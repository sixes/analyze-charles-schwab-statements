#!/usr/bin/env python3
"""Streamlit front end for the Schwab statement analyzer.

Upload monthly Charles Schwab brokerage statement PDFs and get the same
performance data, attribution and charts the CLI produces.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import hmac
import io
import os

import pandas as pd
import streamlit as st

import analyze_schwab as A
import store

st.set_page_config(page_title="Schwab Statement Analyzer", page_icon="chart_with_upwards_trend", layout="wide")


def require_password() -> None:
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        st.error("APP_PASSWORD is not set. Start the app with ./run.sh so the login gate has a password.")
        st.stop()
    if st.session_state.get("authenticated"):
        return
    st.title("Charles Schwab Statement Analyzer")
    st.caption("This instance is reachable over the network. Sign in to continue.")
    with st.form("login"):
        supplied = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            if hmac.compare_digest(supplied, expected):
                st.session_state["authenticated"] = True
                st.rerun()
            st.error("Incorrect password.")
    st.stop()


require_password()


@st.cache_data(show_spinner=False)
def parse_upload(data: bytes, name: str) -> dict:
    return A.StatementParser(io.BytesIO(data), name=name).parse()


def money(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"${value:,.0f}"


def cents(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"${value:,.2f}"


def percent(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value * 100:.2f}%"


def read_stored() -> tuple[list, list, str | None]:
    """Stored statements, their inventory, and a message if the database is unreachable."""
    try:
        with store.connect() as conn:
            return store.load_records(conn), store.statement_index(conn), None
    except Exception as error:
        return [], [], str(error)


def write_stored(pairs: list) -> str | None:
    try:
        with store.connect() as conn:
            for digest, record in pairs:
                store.save_record(conn, record, digest)
        return None
    except Exception as error:
        return str(error)


st.title("Charles Schwab Statement Analyzer")
st.caption(
    "Monthly performance measurement and attribution from your statement PDFs. "
    "Files are parsed in memory on this machine and are never uploaded anywhere else."
)

with st.sidebar:
    st.header("Statements")
    uploads = st.file_uploader(
        "Upload monthly statement PDFs",
        type=["pdf"],
        accept_multiple_files=True,
        help="Add one PDF per statement period. More months means more meaningful risk statistics.",
    )

    stored_records, stored_index, db_error = read_stored()
    st.header("Stored history")
    if db_error:
        st.warning(
            "No database connection, so nothing is being stored. Run `./run.sh initdb` "
            "once and restart. Uploads still work in memory."
        )
        st.caption(db_error.strip().splitlines()[0][:200])
        save_to_db = False
        use_history = False
    else:
        if stored_index:
            first = min(row["period_start"] for row in stored_index)
            last = max(row["period_end"] for row in stored_index)
            st.caption(
                f"{len(stored_index)} statement(s) stored, {first} to {last} "
                f"({sum(row['transaction_rows'] for row in stored_index)} transaction rows)."
            )
        else:
            st.caption("No statements stored yet.")
        save_to_db = st.checkbox(
            "Save uploads to the database",
            value=True,
            help="Statements are keyed by file checksum, so re-uploading the same PDF "
            "replaces its rows instead of duplicating them.",
        )
        use_history = st.checkbox(
            "Include stored history",
            value=True,
            help="Analyze every stored statement, not just this upload.",
        )

    st.header("Assumptions")
    risk_free = st.number_input(
        "Risk-free rate (annual)",
        min_value=0.0,
        max_value=0.25,
        value=0.042,
        step=0.001,
        format="%.3f",
        help="Used only for the Sharpe and Sortino ratios.",
    )

if not uploads and not (use_history and stored_records):
    st.info("Upload one or more Schwab statement PDFs in the sidebar to begin.")
    st.markdown(
        """
        **What you get**

        - Time-weighted return per month (Modified Dietz, external flows day-weighted)
        - Money-weighted return (IRR) for your realized dollar experience
        - Risk statistics once there are at least two periods
        - Realized short-term vs long-term vs unrealized gains
        - Asset class reconciliation and position-level exposure
        - Option premium collected per month, filterable by symbol and period
        - A written report plus CSV exports

        **What you don't get**: forecasts, buy/sell recommendations, or tax advice.
        """
    )
    st.stop()

uploaded, failures = [], []
if uploads:
    progress = st.progress(0.0, text="Parsing statements...")
    for index, upload in enumerate(uploads, start=1):
        data = upload.getvalue()
        try:
            uploaded.append((store.statement_digest(data), parse_upload(data, upload.name)))
        except Exception as error:
            failures.append((upload.name, str(error)))
        progress.progress(index / len(uploads), text=f"Parsed {index} of {len(uploads)}")
    progress.empty()

for name, error in failures:
    st.error(f"Could not parse **{name}**: {error}")

if uploaded and save_to_db:
    write_error = write_stored(uploaded)
    if write_error:
        st.error(f"Could not store the upload: {write_error}")
    else:
        st.caption(f"Stored {len(uploaded)} statement(s) in `{store.database_name()}`.")
        stored_records, stored_index, db_error = read_stored()

if use_history and stored_records:
    records = list(stored_records)
    digests = {record.get("_sha256") for record in stored_records}
    periods = {record["period_end"] for record in stored_records}
    for digest, record in uploaded:
        if digest in digests or record["period_end"] in periods:
            continue
        records.append(record)
else:
    records = [record for _, record in uploaded]

records.sort(key=lambda record: record["period_end"])
if not records:
    st.stop()

seen = {}
for record in records:
    key = record["period_end"]
    if key in seen:
        st.warning(
            f"Two statements cover the period ending {key}: "
            f"`{seen[key]}` and `{record['file']}`. Both are included, which will "
            "double-count that month. Remove one for accurate figures."
        )
    seen[key] = record["file"]

frame = A.build_frame(records)
metrics = A.compute_metrics(frame, risk_free)
attribution = A.class_attribution(frame)

holdings = pd.DataFrame(
    [
        dict(holding, month=record["period_end"].strftime("%Y-%m"), period_end=record["period_end"])
        for record in records
        for holding in record["_holdings"]
    ]
)
flows = pd.DataFrame(
    [
        {
            "month": record["period_end"].strftime("%Y-%m"),
            "date": flow["date"],
            "amount": flow["amount"],
            "description": flow["description"],
        }
        for record in records
        for flow in record["_flows"]
    ]
)
settled_transactions = A.transaction_frame(records)

months = metrics["months"]
st.success(
    f"Parsed {len(records)} statement(s) covering {metrics['period_start']} to "
    f"{metrics['period_end']}."
)

top = st.columns(4)
top[0].metric("Cumulative TWR", percent(metrics["cumulative_twr"]))
top[1].metric("Annualized MWR (IRR)", percent(metrics["annualized_mwr"]))
top[2].metric(
    "Ending value",
    money(metrics["ending_value"]),
    delta=money(metrics["total_value_change"]),
)
top[3].metric("Investment gain", money(metrics["investment_gain"]))

second = st.columns(4)
second[0].metric("Net external flows", money(metrics["net_deposits"]))
second[1].metric("Annualized volatility", percent(metrics["volatility_annualized"]))
second[2].metric("Max drawdown", percent(metrics["max_drawdown"]))
second[3].metric("Statement months", str(months))

if not metrics["annualization_valid"]:
    st.warning(
        f"Only {months} statement month(s) parsed. The cumulative return is real, but "
        f"annualizing it ({percent(metrics['annualized_twr'])}) is not meaningful, and "
        "volatility, Sharpe, Sortino and drawdown need a longer record. Upload more months."
    )

charts = dict(A.build_charts(frame, holdings, metrics))

tab_overview, tab_performance, tab_risk, tab_income, tab_premium, tab_positions, tab_data = st.tabs(
    [
        "Overview",
        "Performance",
        "Risk & attribution",
        "Income & gains",
        "Premiums & transactions",
        "Positions",
        "Report & data",
    ]
)

with tab_overview:
    st.subheader("Account value")
    st.pyplot(charts["01_account_value.png"], width="stretch")
    st.subheader("How the value changed")
    st.pyplot(charts["07_value_reconciliation.png"], width="stretch")
    st.caption(
        "Deposits raise account value without being performance. The time-weighted "
        "return strips them out; the money-weighted return keeps their timing."
    )

with tab_performance:
    st.subheader("Monthly time-weighted return")
    st.pyplot(charts["02_monthly_returns.png"], width="stretch")
    if "03_cumulative_and_drawdown.png" in charts:
        st.subheader("Cumulative return and drawdown")
        st.pyplot(charts["03_cumulative_and_drawdown.png"], width="stretch")

    st.subheader("Monthly detail")
    display = pd.DataFrame(
        {
            "Month": frame["month"],
            "Beginning": frame["beginning_value"],
            "Ending": frame["ending_value"],
            "Net flows": frame["net_flow"],
            "Gain": frame["gain"],
            "TWR %": frame["twr"] * 100,
            "Market appr.": frame["market_appreciation"],
            "Income": frame["dividends_interest"],
            "Cumulative TWR %": frame["cumulative_twr"] * 100,
        }
    )
    st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        column_config={
            "Beginning": st.column_config.NumberColumn(format="$%.2f"),
            "Ending": st.column_config.NumberColumn(format="$%.2f"),
            "Net flows": st.column_config.NumberColumn(format="$%.2f"),
            "Gain": st.column_config.NumberColumn(format="$%.2f"),
            "TWR %": st.column_config.NumberColumn(format="%.2f%%"),
            "Market appr.": st.column_config.NumberColumn(format="$%.2f"),
            "Income": st.column_config.NumberColumn(format="$%.2f"),
            "Cumulative TWR %": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    st.caption(
        "TWR is Modified Dietz with each external flow weighted by the days it was "
        "invested. Monthly returns are linked geometrically, never added."
    )

with tab_risk:
    left, right = st.columns(2)
    with left:
        st.subheader("Risk")
        if months >= 2:
            rows = [
                ("Annualized volatility", percent(metrics["volatility_annualized"])),
                ("Annualized downside deviation", percent(metrics["downside_deviation_annualized"])),
                ("Sharpe ratio", A.fmt_ratio(metrics["sharpe"])),
                ("Sortino ratio", A.fmt_ratio(metrics["sortino"])),
                ("Max drawdown", percent(metrics["max_drawdown"])),
                ("Positive months", percent(metrics["hit_rate"])),
            ]
            if metrics["best_month"]:
                rows.append(("Best month", f"{metrics['best_month'][0]}  {percent(metrics['best_month'][1])}"))
                rows.append(("Worst month", f"{metrics['worst_month'][0]}  {percent(metrics['worst_month'][1])}"))
            st.dataframe(
                pd.DataFrame(rows, columns=["Measure", "Value"]),
                hide_index=True,
                width="stretch",
            )
            if months < 12:
                st.caption(
                    f"n = {months} monthly observations. These are descriptive only and "
                    "carry no statistical confidence."
                )
        else:
            st.info(
                "Risk statistics need at least two statement periods. "
                "Upload more monthly PDFs."
            )
    with right:
        st.subheader("Return measures")
        st.dataframe(
            pd.DataFrame(
                [
                    ("Cumulative TWR", percent(metrics["cumulative_twr"])),
                    (
                        "Annualized TWR",
                        percent(metrics["annualized_twr"])
                        + ("" if metrics["annualization_valid"] else "  (not meaningful)"),
                    ),
                    ("Monthly MWR (IRR)", percent(metrics["monthly_mwr"])),
                    ("Annualized MWR (IRR)", percent(metrics["annualized_mwr"])),
                ],
                columns=["Measure", "Value"],
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "TWR judges the investment decisions. MWR judges your realized dollar "
            "outcome including when you added cash. Neither substitutes for the other."
        )

    st.subheader("Asset class reconciliation")
    if attribution.empty:
        st.info("No asset allocation data was parsed from these statements.")
    else:
        st.dataframe(
            attribution.rename(
                columns={
                    "asset_class": "Class",
                    "start_value": "Start",
                    "end_value": "End",
                    "value_change": "Change",
                    "avg_weight": "Avg weight",
                    "end_weight": "End weight",
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Start": st.column_config.NumberColumn(format="$%.0f"),
                "End": st.column_config.NumberColumn(format="$%.0f"),
                "Change": st.column_config.NumberColumn(format="$%.0f"),
                "Avg weight": st.column_config.NumberColumn(format="%.1f%%"),
                "End weight": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )
        st.caption(
            "Statements do not disclose cash flows per asset class, so class change "
            "includes intra-class trading. This is a reconciliation, not a Brinson attribution."
        )
    if "04_asset_allocation.png" in charts:
        st.pyplot(charts["04_asset_allocation.png"], width="stretch")

with tab_income:
    if "05_income.png" in charts:
        st.subheader("Dividends and interest")
        st.pyplot(charts["05_income.png"], width="stretch")
    if "06_realized_unrealized.png" in charts:
        st.subheader("Realized vs unrealized")
        st.pyplot(charts["06_realized_unrealized.png"], width="stretch")
    cells = st.columns(3)
    cells[0].metric("Realized short-term", money(metrics["realized_st"]))
    cells[1].metric("Realized long-term", money(metrics["realized_lt"]))
    cells[2].metric("Unrealized at period end", money(metrics["unrealized_latest"]))
    cells = st.columns(3)
    cells[0].metric("Dividends", money(metrics["dividends_total"]))
    cells[1].metric("Interest", money(metrics["interest_total"]))
    cells[2].metric("Expenses / margin interest", money(metrics["expenses_total"]))
    if metrics["realized_lt"] == 0 and (metrics["realized_st"] or 0) != 0:
        st.caption("All realized gains are short-term, taxed as ordinary income.")

with tab_premium:
    transactions = A.transaction_frame(records, include_pending=True)
    if transactions.empty:
        st.info(
            "No transaction detail was parsed from these statements. Only statements "
            "with a Transaction Details section carry premium data."
        )
    else:
        st.subheader("Filters")
        row_one = st.columns(4)
        available_months = sorted(transactions["month"].dropna().unique(), reverse=True)
        chosen_months = row_one[0].multiselect(
            "Month", available_months, help="Empty means every month."
        )
        dated = transactions["trade_date"].dropna()
        low, high = (dated.min(), dated.max()) if not dated.empty else (None, None)
        chosen_range = row_one[1].date_input(
            "Trade date range",
            value=(low, high) if low is not None else (),
            min_value=low,
            max_value=high,
            help="Ignored when a month is selected.",
        )
        symbols = sorted(transactions["symbol"].dropna().unique())
        chosen_symbols = row_one[2].multiselect("Symbol", symbols)
        chosen_status = row_one[3].radio(
            "Settlement",
            ["Settled only", "Settled + pending", "Pending only"],
            index=0,
            help="Pending rows are unsettled Schwab activity and can still change.",
        )

        row_two = st.columns(4)
        chosen_kind = row_two[0].multiselect("Option type", ["PUT", "CALL"])
        actions = sorted(transactions["action"].dropna().unique())
        chosen_actions = row_two[1].multiselect("Action", actions)
        categories = sorted(transactions["category"].dropna().unique())
        chosen_categories = row_two[2].multiselect("Category", categories)
        options_only = row_two[3].checkbox(
            "Options only",
            value=True,
            help="Premium figures always ignore non-option rows; this also filters the table below.",
        )

        filtered = transactions
        if chosen_months:
            filtered = filtered[filtered["month"].isin(chosen_months)]
        elif isinstance(chosen_range, (list, tuple)) and len(chosen_range) == 2:
            start, end = chosen_range
            filtered = filtered[
                filtered["trade_date"].notna()
                & (filtered["trade_date"] >= start)
                & (filtered["trade_date"] <= end)
            ]
        if chosen_symbols:
            filtered = filtered[filtered["symbol"].isin(chosen_symbols)]
        if chosen_status == "Settled only":
            filtered = filtered[filtered["settled"]]
        elif chosen_status == "Pending only":
            filtered = filtered[~filtered["settled"]]
        if chosen_kind:
            filtered = filtered[filtered["option_type"].isin(chosen_kind)]
        if chosen_actions:
            filtered = filtered[filtered["action"].isin(chosen_actions)]
        if chosen_categories:
            filtered = filtered[filtered["category"].isin(chosen_categories)]
        if options_only:
            filtered = filtered[filtered["is_option"]]

        summary = A.premium_summary(filtered)
        pending_rows = int((~filtered["settled"]).sum())

        st.subheader("Option premium")
        cells = st.columns(4)
        cells[0].metric("Net premium", cents(summary["premium_net"]))
        cells[1].metric("Credits collected", cents(summary["premium_collected"]))
        cells[2].metric("Paid to close", cents(summary["premium_paid_to_close"]))
        cells[3].metric("Contracts sold", f"{summary['contracts_opened']:,.0f}")
        cells = st.columns(4)
        cells[0].metric("Long option purchases", cents(summary["long_option_purchases"]))
        cells[1].metric("Commissions and fees", cents(summary["charges"]))
        cells[2].metric("Contracts closed early", f"{summary['contracts_closed']:,.0f}")
        cells[3].metric("Option rows", f"{summary['trades']:,}")

        st.caption(
            "Amounts are the printed Amount column, already net of commission. Net premium "
            "is credits from short sales plus what was paid to close. Premium is not income: "
            "a short option's credit is a liability until it is closed or expires, and the "
            "realized gain appears only then — see the Income & gains tab for the "
            "statement's authoritative realized figures."
        )
        if pending_rows:
            st.warning(
                f"{pending_rows} of these row(s) are pending, unsettled activity. They are "
                "included in the figures above and may still change."
            )
        st.caption(
            f"Gross notional of the contracts sold, derived by assuming the standard "
            f"100-share multiplier and not printed on the statement: "
            f"{cents(summary['gross_notional'])}."
        )

        monthly = A.premium_by_month(filtered)
        if not monthly.empty:
            st.subheader("Premium by month")
            st.bar_chart(monthly.set_index("month")["premium_net"], height=260)
            st.dataframe(
                monthly.rename(
                    columns={
                        "month": "Month",
                        "premium_collected": "Credits",
                        "premium_paid_to_close": "Paid to close",
                        "premium_net": "Net premium",
                        "long_option_purchases": "Long purchases",
                        "charges": "Fees",
                        "contracts_opened": "Sold",
                        "contracts_closed": "Closed",
                        "trades": "Rows",
                    }
                ),
                hide_index=True,
                width="stretch",
                column_config={
                    "Credits": st.column_config.NumberColumn(format="$%.2f"),
                    "Paid to close": st.column_config.NumberColumn(format="$%.2f"),
                    "Net premium": st.column_config.NumberColumn(format="$%.2f"),
                    "Long purchases": st.column_config.NumberColumn(format="$%.2f"),
                    "Fees": st.column_config.NumberColumn(format="$%.2f"),
                },
            )

        by_symbol = A.premium_by_symbol(filtered)
        if not by_symbol.empty:
            st.subheader("Premium by symbol")
            st.dataframe(
                by_symbol.rename(
                    columns={
                        "symbol": "Symbol",
                        "premium_collected": "Credits",
                        "premium_paid_to_close": "Paid to close",
                        "premium_net": "Net premium",
                        "long_option_purchases": "Long purchases",
                        "charges": "Fees",
                        "contracts_opened": "Sold",
                        "contracts_closed": "Closed",
                        "trades": "Rows",
                    }
                ),
                hide_index=True,
                width="stretch",
                column_config={
                    "Credits": st.column_config.NumberColumn(format="$%.2f"),
                    "Paid to close": st.column_config.NumberColumn(format="$%.2f"),
                    "Net premium": st.column_config.NumberColumn(format="$%.2f"),
                    "Long purchases": st.column_config.NumberColumn(format="$%.2f"),
                    "Fees": st.column_config.NumberColumn(format="$%.2f"),
                },
            )

        st.subheader(f"Transactions ({len(filtered):,} row(s))")
        st.dataframe(
            filtered[
                [
                    "trade_date",
                    "settled",
                    "category",
                    "action",
                    "symbol",
                    "option_type",
                    "strike",
                    "expiry",
                    "quantity",
                    "price",
                    "charges",
                    "amount",
                    "realized",
                    "term",
                    "description",
                ]
            ].rename(
                columns={
                    "trade_date": "Trade date",
                    "settled": "Settled",
                    "category": "Category",
                    "action": "Action",
                    "symbol": "Symbol",
                    "option_type": "Type",
                    "strike": "Strike",
                    "expiry": "Expiry",
                    "quantity": "Qty",
                    "price": "Price",
                    "charges": "Fees",
                    "amount": "Amount",
                    "realized": "Realized",
                    "term": "Term",
                    "description": "Description",
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Strike": st.column_config.NumberColumn(format="$%.2f"),
                "Price": st.column_config.NumberColumn(format="$%.4f"),
                "Fees": st.column_config.NumberColumn(format="$%.2f"),
                "Amount": st.column_config.NumberColumn(format="$%.2f"),
                "Realized": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
        st.download_button(
            "filtered_transactions.csv",
            filtered.to_csv(index=False),
            file_name="filtered_transactions.csv",
        )

with tab_positions:
    if holdings.empty:
        st.info("No individual positions were parsed.")
    else:
        latest_month = holdings["month"].max()
        chosen = st.selectbox(
            "Statement period",
            sorted(holdings["month"].unique(), reverse=True),
            index=0,
        )
        current = holdings[holdings["month"] == chosen]
        st.dataframe(
            current[
                [
                    "asset_class",
                    "symbol",
                    "label",
                    "description",
                    "quantity",
                    "price",
                    "market_value",
                    "cost_basis",
                    "unrealized",
                    "strike",
                    "expiry",
                ]
            ].rename(
                columns={
                    "asset_class": "Class",
                    "symbol": "Symbol",
                    "label": "Position",
                    "description": "Description",
                    "quantity": "Qty",
                    "price": "Price",
                    "market_value": "Market value",
                    "cost_basis": "Cost basis",
                    "unrealized": "Unrealized",
                    "strike": "Strike",
                    "expiry": "Expiry",
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Price": st.column_config.NumberColumn(format="$%.4f"),
                "Market value": st.column_config.NumberColumn(format="$%.2f"),
                "Cost basis": st.column_config.NumberColumn(format="$%.2f"),
                "Unrealized": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
        if chosen == latest_month:
            if "08_holdings_pnl.png" in charts:
                st.pyplot(charts["08_holdings_pnl.png"], width="stretch")
            if "09_position_exposure.png" in charts:
                st.pyplot(charts["09_position_exposure.png"], width="stretch")
        else:
            st.caption("Position charts are drawn for the most recent statement period.")
        st.caption(
            "Negative market value is a short position. Short options carry assignment "
            "risk beyond their market value, and 2x/3x leveraged ETFs compound daily, so "
            "their returns are path-dependent."
        )

with tab_data:
    residual = metrics["max_reconciliation_residual"]
    if residual is None:
        st.warning("Derived gain could not be cross-checked against the statement components.")
    elif residual < 0.02:
        st.success(
            "Derived gain ties to each statement's Market Appreciation + Income + Expenses "
            f"lines (max residual ${residual:,.2f})."
        )
    else:
        st.error(
            f"Derived gain differs from the stated components by up to ${residual:,.2f}. "
            "Investigate before relying on these figures."
        )
    known = int(frame["flow_dates_known"].sum())
    if known < len(frame):
        st.caption(
            f"{len(frame) - known} of {len(frame)} period(s) had no dated cash flow rows; "
            "those use mid-period weighting."
        )

    txn_residuals = [
        record["txn_amount_residual"]
        for record in records
        if record.get("txn_amount_residual") is not None
    ]
    if txn_residuals:
        worst = max(txn_residuals)
        if worst < 0.02:
            st.success(
                f"Transaction detail rows tie to the printed Transactions Summary "
                f"(max residual ${worst:,.2f})."
            )
        else:
            st.error(
                f"Transaction detail rows differ from the printed Transactions Summary by up "
                f"to ${worst:,.2f}. Premium figures may be incomplete."
            )
    pending_residuals = [
        record["pending_residual"]
        for record in records
        if record.get("pending_residual") is not None
    ]
    if pending_residuals and max(pending_residuals) >= 0.02:
        st.warning(
            f"Pending rows differ from the printed Total Pending Transactions by up to "
            f"${max(pending_residuals):,.2f}."
        )

    report = A.build_report(frame, metrics, attribution, risk_free, settled_transactions)
    st.subheader("Written report")
    st.code(report, language="text")

    st.subheader("Downloads")
    export = frame.drop(columns=[c for c in frame.columns if c.startswith("_")])
    files = [
        ("monthly_summary.csv", export.to_csv(index=False)),
        ("asset_class_reconciliation.csv", attribution.to_csv(index=False)),
        ("performance_report.txt", report),
    ]
    if not holdings.empty:
        files.append(("holdings.csv", holdings.to_csv(index=False)))
    if not flows.empty:
        files.append(("cash_flows.csv", flows.to_csv(index=False)))
    if not settled_transactions.empty:
        files.append(("transactions.csv", settled_transactions.to_csv(index=False)))
        files.append(
            ("premium_by_month.csv", A.premium_by_month(settled_transactions).to_csv(index=False))
        )
    for start in range(0, len(files), 4):
        columns = st.columns(4)
        for column, (name, payload) in zip(columns, files[start : start + 4]):
            column.download_button(name, payload, file_name=name, width="stretch")

st.divider()
st.caption(
    "Analysis and education only. Not investment, tax, or legal advice. Returns are "
    "unbenchmarked: no index data is used, so they are not characterized as good or bad "
    "relative to any mandate. Statement figures are not tax documents."
)
