"""Performance analytics over parsed statement records.

Kept together deliberately: build_frame calls modified_dietz, and class_attribution
plus the charts depend on the weight_*/twr_index/drawdown columns that only
build_frame synthesizes. Splitting them further would turn an explicit call into an
implicit schema contract.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .domain import (
    CLASS_COLUMNS,
    PREMIUM_MONTH_COLUMNS,
    TXN_COLUMNS,
    TXN_NUMERIC,
)

def modified_dietz(beginning, ending, flows, start: date, end: date):
    """Day-weighted Modified Dietz return; falls back to mid-period weighting."""
    if not beginning or beginning <= 0:
        return None, None
    days = max((end - start).days + 1, 1)
    net = sum(flow["amount"] for flow in flows)
    weighted = 0.0
    for flow in flows:
        elapsed = (flow["date"] - start).days
        weight = (days - min(max(elapsed, 0), days)) / days
        weighted += flow["amount"] * weight
    denominator = beginning + weighted
    if denominator <= 0:
        return None, None
    gain = ending - beginning - net
    return gain / denominator, gain


def irr_monthly(cash_flows, low=-0.9999, high=10.0, tolerance=1e-10):
    """Monthly IRR by bisection. cash_flows[0] is at t=0."""

    def npv(rate):
        return sum(amount / (1 + rate) ** period for period, amount in enumerate(cash_flows))

    if npv(low) * npv(high) > 0:
        return None
    for _ in range(400):
        mid = (low + high) / 2
        value = npv(mid)
        if abs(value) < tolerance:
            return mid
        if npv(low) * value < 0:
            high = mid
        else:
            low = mid
    return (low + high) / 2



def transaction_frame(records: list, include_pending: bool = False) -> pd.DataFrame:
    """Every statement transaction as one row, tagged with its calendar month."""
    rows = []
    for record in records:
        activity = list(record.get("_transactions") or [])
        if include_pending:
            activity += list(record.get("_pending") or [])
        for row in activity:
            rows.append(dict(row, file=record.get("file"), period_end=record.get("period_end")))
    frame = pd.DataFrame(rows, columns=TXN_COLUMNS + ["file", "period_end"])
    if frame.empty:
        frame["month"] = pd.Series(dtype="object")
        return frame
    for column in TXN_NUMERIC:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["is_option"] = frame["is_option"].fillna(False).astype(bool)
    frame["settled"] = frame["settled"].fillna(True).astype(bool)
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["month"] = dates.dt.strftime("%Y-%m")
    return frame.sort_values("trade_date", kind="stable").reset_index(drop=True)


def confirm_transaction_frame(trades: pd.DataFrame) -> pd.DataFrame:
    """Confirm trades expressed in the statement transaction schema.

    Lets premium_summary and friends run over the confirmation feed unchanged, so the
    current month is populated between statements without a second implementation of the
    premium rules. The mapping only has to satisfy what those functions read: a Sale
    category for credits and a CoverShort action for a short being bought back.

    Realized gain stays empty. A confirmation prints no realized column, so the
    statement's Gain or Loss section remains the authoritative realized total.
    """
    columns = TXN_COLUMNS + ["file", "period_end"]
    if trades is None or getattr(trades, "empty", True):
        empty = pd.DataFrame(columns=columns + ["month", "source"])
        return empty

    rows = []
    for _, trade in trades.iterrows():
        action = str(trade.get("action") or "")
        intent = trade.get("intent")
        selling = action.lower().startswith(("sale", "sell"))
        category = "Sale" if selling else "Purchase"
        if selling:
            mapped = "SellToOpen" if intent == "open" else "Sell"
        else:
            mapped = "CoverShort" if intent == "close" else "BuyToOpen"
        commission = trade.get("commission") or 0.0
        fee = trade.get("industry_fee") or 0.0
        rows.append({
            "settled": True,
            "trade_date": trade.get("trade_date"),
            "settle_date": trade.get("settle_date"),
            "category": category,
            "action": mapped,
            "symbol": trade.get("symbol"),
            "description": trade.get("description"),
            "quantity": trade.get("quantity"),
            "price": trade.get("price"),
            "charges": float(commission) + float(fee),
            "amount": trade.get("net_amount"),
            "realized": None,
            "term": None,
            "is_option": bool(trade.get("is_option")),
            "option_type": trade.get("option_type"),
            "strike": trade.get("strike"),
            "expiry": trade.get("expiry"),
            "file": "eConfirm",
            "period_end": None,
        })

    frame = pd.DataFrame(rows, columns=columns)
    for column in TXN_NUMERIC:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["month"] = dates.dt.strftime("%Y-%m")
    frame["source"] = "confirm"
    return frame.sort_values("trade_date", kind="stable").reset_index(drop=True)


def combined_transaction_frame(records: list, trades: pd.DataFrame,
                               include_pending: bool = False) -> pd.DataFrame:
    """Statement rows plus confirm rows for the period no statement covers yet.

    Counting a confirmation alongside the statement that already prints the same trade
    would double that month's premium, so confirmations are taken only from after the
    newest statement's period end - the same window the position rollforward uses.
    """
    statements = transaction_frame(records, include_pending=include_pending)
    statements["source"] = "statement"
    confirms = confirm_transaction_frame(trades)
    if confirms.empty:
        return statements

    period_ends = [record.get("period_end") for record in records
                   if record.get("period_end") is not None]
    if period_ends:
        latest = max(period_ends)
        confirms = confirms[[value is not None and value > latest
                             for value in confirms["trade_date"]]]
    if confirms.empty:
        return statements

    combined = pd.concat([statements, confirms], ignore_index=True)
    return combined.sort_values("trade_date", kind="stable").reset_index(drop=True)


def premium_summary(frame: pd.DataFrame) -> dict:
    """Option premium from the printed Amount column.

    Amounts are net of commission, which is how Schwab prints them. Premium is
    not income: a short option's credit is a liability until the position is
    closed or expires.
    """
    options = frame[frame["is_option"]] if not frame.empty else frame
    if options.empty:
        opened = closed = bought = options
    else:
        opened = options[options["category"] == "Sale"]
        closed = options[options["action"] == "CoverShort"]
        bought = options[(options["category"] == "Purchase") & (options["action"] != "CoverShort")]
    collected = float(opened["amount"].sum())
    paid = float(closed["amount"].sum())
    realized = options["realized"] if not options.empty else pd.Series(dtype=float)
    term = options["term"] if not options.empty else pd.Series(dtype=object)
    return {
        "premium_collected": collected,
        "premium_paid_to_close": paid,
        "premium_net": collected + paid,
        "long_option_purchases": float(bought["amount"].sum()),
        "charges": float(options["charges"].sum()) if not options.empty else 0.0,
        "contracts_opened": float(opened["quantity"].abs().sum()) if not opened.empty else 0.0,
        "contracts_closed": float(closed["quantity"].abs().sum()) if not closed.empty else 0.0,
        # Derived, not printed: the 100-share multiplier is assumed.
        "gross_notional": float((opened["quantity"].abs() * opened["price"] * 100).sum()) if not opened.empty else 0.0,
        # Only closing trades print a realized gain. Expirations realize the
        # premium too, so these are smaller than the statement's Realized Gain
        # section, which is the authoritative figure for the period.
        "realized_closed_st": float(realized[term == "ST"].sum()),
        "realized_closed_lt": float(realized[term == "LT"].sum()),
        "trades": int(len(options)),
    }


def premium_by_month(frame: pd.DataFrame) -> pd.DataFrame:
    """premium_summary() split by calendar month of the trade date."""
    if frame.empty:
        return pd.DataFrame(columns=["month", *PREMIUM_MONTH_COLUMNS])
    rows = []
    for month, group in frame.groupby("month", dropna=True):
        summary = premium_summary(group)
        rows.append({"month": month, **{key: summary[key] for key in PREMIUM_MONTH_COLUMNS}})
    return pd.DataFrame(rows).sort_values("month").reset_index(drop=True)


def premium_by_symbol(frame: pd.DataFrame) -> pd.DataFrame:
    """premium_summary() split by underlying symbol, largest net premium first."""
    if frame.empty:
        return pd.DataFrame(columns=["symbol", *PREMIUM_MONTH_COLUMNS])
    rows = []
    for symbol, group in frame.groupby("symbol", dropna=True):
        summary = premium_summary(group)
        if not summary["trades"]:
            continue
        rows.append({"symbol": symbol, **{key: summary[key] for key in PREMIUM_MONTH_COLUMNS}})
    if not rows:
        return pd.DataFrame(columns=["symbol", *PREMIUM_MONTH_COLUMNS])
    return (
        pd.DataFrame(rows)
        .sort_values("premium_net", ascending=False)
        .reset_index(drop=True)
    )


def build_frame(records: list) -> pd.DataFrame:
    rows = []
    for record in records:
        row = {key: value for key, value in record.items() if not key.startswith("_")}
        flows = record["_flows"]
        beginning = row.get("beginning_value")
        ending = row.get("ending_value")

        stated_net = (row.get("deposits") or 0.0) + (row.get("withdrawals") or 0.0)
        parsed_net = sum(flow["amount"] for flow in flows)
        # Trust the Account Summary totals; use dated rows only for weighting.
        if flows and abs(parsed_net) > 0.005 and abs(parsed_net - stated_net) > 0.02:
            scale = stated_net / parsed_net
            flows = [dict(flow, amount=flow["amount"] * scale) for flow in flows]
        elif abs(stated_net) > 0.005 and not flows:
            midpoint = row["period_start"] + (row["period_end"] - row["period_start"]) / 2
            flows = [{"date": midpoint, "amount": stated_net, "description": "assumed mid-period"}]

        row["net_flow"] = stated_net
        row["flow_dates_known"] = bool(record["_flows"])
        dietz, gain = modified_dietz(
            beginning, ending, flows, row["period_start"], row["period_end"]
        )
        row["twr"] = dietz
        row["gain"] = gain
        row["simple_return"] = (
            (ending - beginning) / beginning if beginning and beginning > 0 else None
        )
        row["value_change"] = (
            ending - beginning if ending is not None and beginning is not None else None
        )

        # Reconcile derived gain against the statement's own components.
        components = [
            row.get("market_appreciation"),
            row.get("dividends_interest"),
            row.get("expenses"),
        ]
        if gain is not None and all(value is not None for value in components):
            row["reconciliation_residual"] = gain - sum(components)
        else:
            row["reconciliation_residual"] = None

        realized = row.get("st_net")
        long_term = row.get("lt_net")
        if realized is not None or long_term is not None:
            row["realized_net"] = (realized or 0.0) + (long_term or 0.0)
        else:
            row["realized_net"] = None
        rows.append(row)

    frame = pd.DataFrame(rows).sort_values("period_end").reset_index(drop=True)
    frame["month"] = pd.to_datetime(frame["period_end"]).dt.strftime("%Y-%m")

    # Records reloaded from Postgres arrive as Decimal, and a column that is
    # null for every statement lands as object dtype. Both break the arithmetic
    # below, so normalize regardless of where the records came from.
    for column in frame.columns:
        if column in ("file", "month", "period_start", "period_end", "flow_dates_known"):
            continue
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    returns = frame["twr"].fillna(0.0)
    frame["twr_index"] = 100.0 * (1.0 + returns).cumprod()
    frame["cumulative_twr"] = frame["twr_index"] / 100.0 - 1.0
    peak = frame["twr_index"].cummax().clip(lower=100.0)
    frame["drawdown"] = frame["twr_index"] / peak - 1.0

    for name in CLASS_COLUMNS:
        column = f"alloc_{name}"
        total = frame["alloc_total"].replace(0, np.nan)
        frame[f"weight_{name}"] = frame[column] / total
    return frame


def compute_metrics(frame: pd.DataFrame, risk_free: float) -> dict:
    returns = frame["twr"].dropna()
    months = len(returns)
    beginning = frame["beginning_value"].iloc[0]
    ending = frame["ending_value"].iloc[-1]
    cumulative = float((1.0 + returns).prod() - 1.0) if months else None

    metrics = {
        "months": int(months),
        "period_start": str(frame["period_start"].iloc[0]),
        "period_end": str(frame["period_end"].iloc[-1]),
        "beginning_value": float(beginning) if pd.notna(beginning) else None,
        "ending_value": float(ending) if pd.notna(ending) else None,
        "net_deposits": float(frame["net_flow"].sum()),
        "total_value_change": float(frame["value_change"].sum()),
        "investment_gain": float(frame["gain"].dropna().sum()),
        "cumulative_twr": cumulative,
        "annualized_twr": None,
        "annualization_valid": months >= 12,
        "volatility_annualized": None,
        "downside_deviation_annualized": None,
        "sharpe": None,
        "sortino": None,
        "max_drawdown": float(frame["drawdown"].min()) if months else None,
        "best_month": None,
        "worst_month": None,
        "hit_rate": None,
        "monthly_mwr": None,
        "annualized_mwr": None,
        "income_total": float(frame.get("income_total", pd.Series(dtype=float)).sum(min_count=1))
        if "income_total" in frame
        else None,
        "dividends_total": float(frame.get("dividends", pd.Series(dtype=float)).sum(min_count=1))
        if "dividends" in frame
        else None,
        "interest_total": float(frame.get("interest", pd.Series(dtype=float)).sum(min_count=1))
        if "interest" in frame
        else None,
        "expenses_total": float(frame["expenses"].sum(min_count=1))
        if "expenses" in frame
        else None,
        "realized_st": float(frame["st_net"].sum(min_count=1)) if "st_net" in frame else None,
        "realized_lt": float(frame["lt_net"].sum(min_count=1)) if "lt_net" in frame else None,
        "unrealized_latest": float(frame["unrealized"].iloc[-1])
        if "unrealized" in frame and pd.notna(frame["unrealized"].iloc[-1])
        else None,
        "max_reconciliation_residual": float(
            frame["reconciliation_residual"].abs().max()
        )
        if frame["reconciliation_residual"].notna().any()
        else None,
    }

    if cumulative is not None and months:
        metrics["annualized_twr"] = float((1.0 + cumulative) ** (12.0 / months) - 1.0)
        best = returns.idxmax()
        worst = returns.idxmin()
        metrics["best_month"] = [frame["month"].iloc[best], float(returns.loc[best])]
        metrics["worst_month"] = [frame["month"].iloc[worst], float(returns.loc[worst])]
        metrics["hit_rate"] = float((returns > 0).mean())

    if months >= 2:
        volatility = float(returns.std(ddof=1) * np.sqrt(12))
        metrics["volatility_annualized"] = volatility
        downside = returns[returns < 0]
        if len(downside) >= 2:
            deviation = float(downside.std(ddof=1) * np.sqrt(12))
            metrics["downside_deviation_annualized"] = deviation
            if deviation > 0 and metrics["annualized_twr"] is not None:
                metrics["sortino"] = (metrics["annualized_twr"] - risk_free) / deviation
        if volatility > 0 and metrics["annualized_twr"] is not None:
            metrics["sharpe"] = (metrics["annualized_twr"] - risk_free) / volatility

    flows = [-float(beginning)] if pd.notna(beginning) else [0.0]
    for _, row in frame.iterrows():
        flows.append(-float(row["net_flow"] or 0.0))
    flows[-1] += float(ending) if pd.notna(ending) else 0.0
    rate = irr_monthly(flows)
    if rate is not None:
        metrics["monthly_mwr"] = float(rate)
        metrics["annualized_mwr"] = float((1.0 + rate) ** 12 - 1.0)
    return metrics


def class_attribution(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-class value change and average weight.

    Statements do not disclose per-class cash flows, so this reconciles value
    change by class (which includes intra-class trading) rather than claiming a
    clean Brinson attribution.
    """
    rows = []
    for name in CLASS_COLUMNS:
        column = f"alloc_{name}"
        series = frame[column].dropna()
        if series.empty:
            continue
        change = float(series.iloc[-1] - series.iloc[0]) if len(series) > 1 else np.nan
        rows.append(
            {
                "asset_class": name,
                "start_value": float(series.iloc[0]),
                "end_value": float(series.iloc[-1]),
                "value_change": change,
                "avg_weight": float(frame[f"weight_{name}"].dropna().mean())
                if frame[f"weight_{name}"].notna().any()
                else np.nan,
                "end_weight": float(frame[f"weight_{name}"].dropna().iloc[-1])
                if frame[f"weight_{name}"].notna().any()
                else np.nan,
            }
        )
    return pd.DataFrame(rows)
