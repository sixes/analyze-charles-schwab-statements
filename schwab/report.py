"""Plain-text performance report.

Formatting only. Every figure comes from analytics, positions or reconcile - this
module must not compute anything, or the report and the UI could disagree.
"""

from __future__ import annotations

import pandas as pd

from .analytics import premium_summary
from .domain import fmt_money, fmt_pct, fmt_ratio


def build_report(frame: pd.DataFrame, metrics: dict, attribution: pd.DataFrame, risk_free: float,
                 transactions: pd.DataFrame = None, reconciliation=None, positions=None,
                 interim=None):
    months = metrics["months"]
    lines = []
    add = lines.append
    add("=" * 78)
    add("PORTFOLIO PERFORMANCE REVIEW - Charles Schwab Brokerage Account")
    add(f"Period: {metrics['period_start']} to {metrics['period_end']}  ({months} statement month(s))")
    add("=" * 78)

    add("")
    add("1. HEADLINE RESULT")
    add("-" * 78)
    add(f"  Time-weighted return (cumulative) : {fmt_pct(metrics['cumulative_twr'])}")
    if metrics["annualization_valid"]:
        add(f"  Time-weighted return (annualized) : {fmt_pct(metrics['annualized_twr'])}")
    else:
        add(
            f"  Annualized TWR                    : {fmt_pct(metrics['annualized_twr'])}"
            f"  <- NOT MEANINGFUL, only {months} month(s) of data"
        )
    add(f"  Money-weighted return (annualized): {fmt_pct(metrics['annualized_mwr'])}")
    add(f"  Beginning value                   : {fmt_money(metrics['beginning_value'])}")
    add(f"  Ending value                      : {fmt_money(metrics['ending_value'])}")
    add(f"  Net external cash flow            : {fmt_money(metrics['net_deposits'])}")
    add(f"  Total value change                : {fmt_money(metrics['total_value_change'])}")
    add(f"  Investment gain (flow-adjusted)   : {fmt_money(metrics['investment_gain'])}")
    add("")
    add("  Method: TWR uses Modified Dietz with external flows day-weighted from the")
    add("  transaction detail. TWR measures investment decisions; MWR measures the")
    add("  investor's realized dollar experience including flow timing.")

    add("")
    add("2. RISK")
    add("-" * 78)
    if months >= 2:
        add(f"  Annualized volatility             : {fmt_pct(metrics['volatility_annualized'])}")
        add(f"  Annualized downside deviation     : {fmt_pct(metrics['downside_deviation_annualized'])}")
        add(f"  Sharpe ratio                      : {fmt_ratio(metrics['sharpe'])}"
            f"   (rf = {risk_free * 100:.2f}%)")
        add(f"  Sortino ratio                     : {fmt_ratio(metrics['sortino'])}")
        add(f"  Max drawdown (return index)       : {fmt_pct(metrics['max_drawdown'])}")
        add(f"  Positive months                   : {fmt_pct(metrics['hit_rate'])}")
        if metrics["best_month"]:
            add(f"  Best month                        : {metrics['best_month'][0]} "
                f"{fmt_pct(metrics['best_month'][1])}")
            add(f"  Worst month                       : {metrics['worst_month'][0]} "
                f"{fmt_pct(metrics['worst_month'][1])}")
        if months < 12:
            add(f"  CAVEAT: n = {months} monthly observations. These statistics are")
            add("  descriptive only and carry no statistical confidence.")
    else:
        add(f"  Only {months} statement period parsed. Volatility, Sharpe, Sortino and")
        add("  drawdown require at least two periods, and are not meaningful below 12.")
        add("  Add more monthly statement PDFs to build the track record.")

    add("")
    add("3. RETURN DECOMPOSITION")
    add("-" * 78)
    add(f"  Realized short-term gain/(loss)   : {fmt_money(metrics['realized_st'])}")
    add(f"  Realized long-term gain/(loss)    : {fmt_money(metrics['realized_lt'])}")
    add(f"  Unrealized at period end          : {fmt_money(metrics['unrealized_latest'])}")
    add(f"  Dividends received                : {fmt_money(metrics['dividends_total'])}")
    add(f"  Interest received                 : {fmt_money(metrics['interest_total'])}")
    add(f"  Expenses / margin interest        : {fmt_money(metrics['expenses_total'])}")
    if metrics["realized_st"] is not None and metrics["realized_lt"] is not None:
        realized = metrics["realized_st"] + metrics["realized_lt"]
        if abs(realized) > 0 and metrics["realized_lt"] == 0:
            add("  All realized gains are short-term, taxed as ordinary income.")

    add("")
    add("4. OPTION PREMIUM")
    add("-" * 78)
    if transactions is None or transactions.empty:
        add("  No transaction detail parsed.")
    else:
        premium = premium_summary(transactions)
        add(f"  Premium collected (short sales)    : {fmt_money(premium['premium_collected'])}")
        add(f"  Premium paid to close              : {fmt_money(premium['premium_paid_to_close'])}")
        add(f"  Net premium kept                   : {fmt_money(premium['premium_net'])}")
        add(f"  Long option purchases              : {fmt_money(premium['long_option_purchases'])}")
        add(f"  Contracts opened / closed          : {premium['contracts_opened']:,.0f} / {premium['contracts_closed']:,.0f}")
        add(f"  Commissions and fees               : {fmt_money(premium['charges'])}")
        add(f"  Realized on closing trades (ST/LT) : {fmt_money(premium['realized_closed_st'])}"
            f" / {fmt_money(premium['realized_closed_lt'])}")
        add("  Amounts are as printed, net of commission. A short option's credit is a")
        add("  liability until closed or expired, so premium collected is not income.")
        add("  Expirations print no realized gain, so the closing-trade figures above are")
        add("  smaller than section 3's realized total, which is the authoritative one.")

    add("")
    add("5. ASSET CLASS RECONCILIATION")
    add("-" * 78)
    if attribution.empty:
        add("  No asset allocation data parsed.")
    else:
        add(f"  {'Class':<14}{'Start':>14}{'End':>14}{'Change':>14}{'End wt':>10}")
        for _, row in attribution.iterrows():
            change = "n/a" if pd.isna(row["value_change"]) else f"{row['value_change']:,.0f}"
            weight = "n/a" if pd.isna(row["end_weight"]) else f"{row['end_weight'] * 100:.1f}%"
            add(
                f"  {row['asset_class']:<14}{row['start_value']:>14,.0f}"
                f"{row['end_value']:>14,.0f}{change:>14}{weight:>10}"
            )
        add("  Class change includes intra-class trading; statements do not disclose")
        add("  per-class cash flows, so this is a reconciliation, not a Brinson attribution.")

    latest = frame.iloc[-1]
    add("")
    add("6. EXPOSURE AND LEVERAGE AT PERIOD END")
    add("-" * 78)
    add(f"  Total account value               : {fmt_money(latest.get('alloc_total'))}")
    add(f"  Cash and cash investments         : {fmt_money(latest.get('alloc_Cash'))}")
    add(f"  Liabilities (shorts, margin)      : {fmt_money(latest.get('liabilities'))}")
    add(f"  Margin loan balance               : {fmt_money(latest.get('margin_closing'))}")
    add(f"  Securities buying power           : {fmt_money(latest.get('buying_power'))}")
    total = latest.get("alloc_total")
    cash = latest.get("alloc_Cash")
    if total and pd.notna(total) and cash is not None and pd.notna(cash):
        add(f"  Cash weight                       : {fmt_pct(cash / total)}")
    add("")
    add("  Risk notes:")
    add("  - Short option positions create obligations larger than their market value.")
    add("    Their premium is capped while assignment risk is not.")
    add("  - 2x/3x leveraged ETFs are path-dependent: they compound daily and decay in")
    add("    choppy markets. Do not extrapolate their returns linearly.")

    add("")
    add("7. DATA QUALITY")
    add("-" * 78)
    residual = metrics["max_reconciliation_residual"]
    if residual is None:
        add("  Could not cross-check derived gain against statement components.")
    elif residual < 0.02:
        add(f"  Derived gain ties to the statement's Market Appreciation + Income +")
        add(f"  Expenses lines in every period (max residual {fmt_money(residual)}).")
    else:
        add(f"  WARNING: derived gain differs from stated components by up to "
            f"{fmt_money(residual)}. Investigate before relying on these figures.")
    known = int(frame["flow_dates_known"].sum())
    add(f"  Periods with dated cash flows      : {known} of {len(frame)}")
    if known < len(frame):
        add("  Periods without dated flows use mid-period weighting for Modified Dietz.")
    add("  Cost basis may be incomplete per Schwab's own disclosure. Pending")
    add("  transactions are excluded from account value. Not a tax document.")

    if reconciliation is not None:
        summary = reconciliation[1] if isinstance(reconciliation, tuple) else None
        table = reconciliation[0] if isinstance(reconciliation, tuple) else reconciliation
        if summary is None:
            from .reconcile import summarize
            summary = summarize(table)
        add("")
        add("8. STATEMENT VS CONFIRMATION FEED")
        add("-" * 78)
        add("  Trades enter the database from eConfirm email; the statement audits that feed.")
        add(f"  Matched                            : {summary['matched']}")
        add(f"  Amount or price mismatch           : {summary['amount_mismatch']}")
        add(f"  On statement, no confirmation      : {summary['statement_only']}")
        add(f"  Confirmed, not on statement        : {summary['confirm_only']}")
        add(f"  Not confirmable                    : {summary['not_confirmable']}")
        add(f"  Predates the confirmation feed     : {summary['before_confirm_feed']}")
        add("  Dividends, interest, fees, journals and expirations produce no confirmation,")
        add("  so they are excluded from the comparison rather than flagged as missing.")
        if table is not None and not table.empty:
            issues = table[table["status"].isin(
                ["amount_mismatch", "confirm_only", "statement_only"])]
            for _, row in issues.head(20).iterrows():
                add(f"    {str(row['period_end']):<12}{row['status']:<18}"
                    f"{str(row['key'])[:22]:<24}{row['note']}")

    if positions is not None and not positions.empty:
        from .positions import marked_total
        total, complete = marked_total(positions)
        add("")
        add("9. CURRENT POSITIONS (CONFIRM ROLLFORWARD)")
        add("-" * 78)
        add("  Statement holdings rolled forward with confirmed trades and marked at")
        add("  delayed third-party quotes. Positions and marked value only: confirms carry")
        add("  no cash, margin, dividend or corporate-action data, so no account value,")
        add("  time-weighted return or risk figure is derived from them.")
        add(f"  Open positions                     : {len(positions)}")
        add(f"  Marked value                       : "
            f"{fmt_money(total) if complete else 'n/a (incomplete)'}")
        add(f"    {'Position':<24}{'Qty':>9}{'Traded':>10}{'Mark':>10}{'Value':>14}"
            f"{'P/L %':>9}   Status")
        for _, row in positions.iterrows():
            value = "n/a" if pd.isna(row["market_value"]) else fmt_money(row["market_value"])
            traded = "n/a" if pd.isna(row["entry_price"]) else f"{row['entry_price']:,.2f}"
            mark = "n/a" if pd.isna(row["price"]) else f"{row['price']:,.2f}"
            change = "n/a" if pd.isna(row["unrealized_pct"]) else fmt_pct(row["unrealized_pct"])
            add(f"    {str(row['key'])[:22]:<24}{row['quantity']:>9.2f}{traded:>10}{mark:>10}"
                f"{value:>14}{change:>9}   {row['status']}")
        flagged = positions[positions["status"] != "open"]
        if not flagged.empty:
            add("  Flagged:")
            for _, row in flagged.iterrows():
                add(f"    {str(row['key'])[:22]:<24}{row['note']}")

    if interim is not None and interim.get("anchor_date") is not None:
        add("")
        add("10. SINCE THE LAST STATEMENT (CONFIRMATION FEED)")
        add("-" * 78)
        add("  Position-level profit and loss only. This is NOT a time-weighted return and")
        add("  must not be linked with the monthly series above: confirms carry no cash")
        add("  balance, deposit, dividend or corporate-action data, so the denominator a")
        add("  return requires does not exist between statements.")
        add(f"  Window                             : {interim['anchor_date']} to today")
        add(f"  Confirmed trades applied           : {interim['trade_count']}")
        add(f"  Position value at the statement    : "
            f"{fmt_money(interim['anchor_position_value'])}")
        add(f"  Position value now (marked)        : "
            f"{fmt_money(interim['current_position_value'])}")
        add(f"  Change in position value           : {fmt_money(interim['value_change'])}")
        add(f"  Cash from confirmed trades         : {fmt_money(interim['trade_cash'])}")
        add(f"  Profit and loss                    : {fmt_money(interim['pnl'])}")
        add(f"  Against last printed account value : {fmt_pct(interim['pnl_pct'])}")
        for note in interim.get("assumptions") or []:
            add(f"    ! {note}")

    add("")
    add("=" * 78)
    add("Analysis and education only. Not investment, tax, or legal advice.")
    add("Returns are unbenchmarked: no index data is used, so they are not")
    add("characterized as good or bad relative to any mandate.")
    add("=" * 78)
    return "\n".join(lines)
