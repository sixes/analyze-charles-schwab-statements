"""Statement rows checked against the trade-confirmation feed.

Trades now enter the database from eConfirm email, which means the statement's role
changes: it becomes the audit. It is the printed, reconciled record, so any trade it
shows that no confirm produced is either a missing email or a gap in ingestion, and any
confirm the statement does not show is a parser miss on one side or the other.

Filter before comparing. A statement's Transaction Details section carries dividends,
interest, NRA tax withholding, fees, journals and Other Activity expirations, none of
which generate a trade confirmation. Without partitioning those out first, every
dividend would be reported as a missing confirm and the signal would be lost in noise.
"""

from __future__ import annotations

import pandas as pd

from .domain import fmt_money, occ_symbol

CONFIRMABLE_CATEGORIES = {"sale", "purchase"}

PRICE_TOLERANCE = 0.005
AMOUNT_TOLERANCE = 0.02

RECONCILE_COLUMNS = [
    "period_end", "status", "key", "trade_date", "side", "quantity",
    "statement_price", "confirm_price", "statement_amount", "confirm_amount",
    "difference", "category", "description", "note",
]

MATCHED = "matched"
AMOUNT_MISMATCH = "amount_mismatch"
CONFIRM_ONLY = "confirm_only"
STATEMENT_ONLY = "statement_only"
NOT_CONFIRMABLE = "not_confirmable"
BEFORE_FEED = "before_confirm_feed"

STATUSES = (MATCHED, AMOUNT_MISMATCH, CONFIRM_ONLY, STATEMENT_ONLY, NOT_CONFIRMABLE, BEFORE_FEED)


def side_of(text) -> str | None:
    if not text:
        return None
    lowered = str(text).strip().lower()
    if lowered.startswith("sale") or lowered.startswith("sell"):
        return "sell"
    if lowered.startswith("purchase") or lowered.startswith("buy"):
        return "buy"
    return None


def statement_key(row: dict, period_end):
    symbol = row.get("symbol")
    if not symbol:
        return None
    if row.get("is_option"):
        expiry = row.get("expiry")
        key = occ_symbol(symbol, expiry, row.get("option_type"), row.get("strike"))
        return key or f"?{symbol}"
    return str(symbol).strip().upper()


def _confirmable(row: dict) -> bool:
    category = str(row.get("category") or "").strip().lower()
    if category not in CONFIRMABLE_CATEGORIES:
        return False
    return row.get("quantity") is not None and row.get("price") is not None


def feed_start(trades: pd.DataFrame):
    """Trade date of the earliest confirmation held.

    Statements older than this cannot be audited: the confirm feed simply did not exist
    yet. Reporting their trades as missing confirmations would raise 60-odd alarms on
    every run and bury a real gap when one appears.
    """
    if trades is None or getattr(trades, "empty", True) or "trade_date" not in trades:
        return None
    dates = [value for value in trades["trade_date"] if value is not None and value == value]
    return min(dates) if dates else None


def reconcile(record: dict, trades: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """One statement period against the confirm trades that fall inside it."""
    period_end = record.get("period_end")
    period_start = record.get("period_start")
    start = feed_start(trades)
    covered = start is not None and period_end is not None and period_end >= start

    rows = []
    statement_rows = []
    # A trade that fills in the last days of the period settles after the statement
    # closes, so it prints only under Pending / Open Activity. Those rows are real
    # trades and Schwab confirms them; leaving them out reports each as confirm_only.
    source_rows = [row for row in (record.get("_transactions") or []) if row.get("settled", True)]
    source_rows += list(record.get("_pending") or [])
    for row in source_rows:
        if _confirmable(row):
            statement_rows.append(row)
            continue
        rows.append({
            "period_end": period_end,
            "status": NOT_CONFIRMABLE,
            "key": row.get("symbol"),
            "trade_date": row.get("trade_date"),
            "side": None,
            "quantity": row.get("quantity"),
            "statement_price": row.get("price"),
            "confirm_price": None,
            "statement_amount": row.get("amount"),
            "confirm_amount": None,
            "difference": None,
            "category": row.get("category"),
            "description": row.get("description"),
            "note": "no trade confirmation is issued for this activity",
        })

    confirm_rows = []
    if trades is not None and not getattr(trades, "empty", True):
        window = trades[trades["trade_date"].notna()]
        if period_start is not None:
            window = window[[value >= period_start for value in window["trade_date"]]]
        if period_end is not None:
            window = window[[value <= period_end for value in window["trade_date"]]]
        confirm_rows = window.to_dict("records")

    unmatched = list(range(len(confirm_rows)))
    duplicate_keys = set()

    for row in statement_rows:
        key = statement_key(row, period_end)
        side = side_of(row.get("category")) or side_of(row.get("action"))
        quantity = abs(float(row.get("quantity") or 0.0))
        price = row.get("price")

        candidates = []
        for index in unmatched:
            confirm = confirm_rows[index]
            confirm_key = confirm.get("occ_symbol") if confirm.get("is_option") \
                else str(confirm.get("symbol") or "").strip().upper()
            if key != confirm_key:
                continue
            if side != side_of(confirm.get("action")):
                continue
            if row.get("trade_date") is not None and confirm.get("trade_date") is not None \
                    and row["trade_date"] != confirm["trade_date"]:
                continue
            if abs(abs(float(confirm.get("quantity") or 0.0)) - quantity) > 1e-6:
                continue
            candidates.append(index)

        if not candidates:
            trade_date = row.get("trade_date")
            before_feed = start is None or (trade_date is not None and trade_date < start)
            rows.append({
                "period_end": period_end,
                "status": BEFORE_FEED if before_feed else STATEMENT_ONLY,
                "key": key,
                "trade_date": trade_date,
                "side": side,
                "quantity": row.get("quantity"),
                "statement_price": price,
                "confirm_price": None,
                "statement_amount": row.get("amount"),
                "confirm_amount": None,
                "difference": None,
                "category": row.get("category"),
                "description": row.get("description"),
                "note": "predates the confirmation feed; not auditable" if before_feed
                        else "no confirmation ingested for this trade",
            })
            continue

        if len(candidates) > 1:
            duplicate_keys.add(key)
        index = candidates[0]
        unmatched.remove(index)
        confirm = confirm_rows[index]

        note = ""
        status = MATCHED
        confirm_price = confirm.get("price")
        confirm_amount = confirm.get("net_amount")
        statement_amount = row.get("amount")
        difference = None
        if statement_amount is not None and confirm_amount is not None:
            difference = float(statement_amount) - float(confirm_amount)
            if abs(difference) > AMOUNT_TOLERANCE:
                status = AMOUNT_MISMATCH
                note = "amounts differ by more than the fee-rounding tolerance"
        if price is not None and confirm_price is not None \
                and abs(float(price) - float(confirm_price)) > PRICE_TOLERANCE:
            status = AMOUNT_MISMATCH
            note = "prices differ"
        if key in duplicate_keys:
            note = (note + "; " if note else "") + \
                "same contract traded more than once at this price; pairing is unordered"
        if not row.get("settled", True):
            note = (note + "; " if note else "") + \
                "statement prints this under Pending / Open Activity; unsettled at period end"

        rows.append({
            "period_end": period_end,
            "status": status,
            "key": key,
            "trade_date": row.get("trade_date"),
            "side": side,
            "quantity": row.get("quantity"),
            "statement_price": price,
            "confirm_price": confirm_price,
            "statement_amount": statement_amount,
            "confirm_amount": confirm_amount,
            "difference": difference,
            "category": row.get("category"),
            "description": row.get("description"),
            "note": note,
        })

    for index in unmatched:
        confirm = confirm_rows[index]
        key = confirm.get("occ_symbol") if confirm.get("is_option") \
            else str(confirm.get("symbol") or "").strip().upper()
        rows.append({
            "period_end": period_end,
            "status": CONFIRM_ONLY,
            "key": key,
            "trade_date": confirm.get("trade_date"),
            "side": side_of(confirm.get("action")),
            "quantity": confirm.get("quantity"),
            "statement_price": None,
            "confirm_price": confirm.get("price"),
            "statement_amount": None,
            "confirm_amount": confirm.get("net_amount"),
            "difference": None,
            "category": confirm.get("action"),
            "description": confirm.get("description"),
            "note": "confirmed but not found on the statement; check the statement parser",
        })

    frame = pd.DataFrame(rows, columns=RECONCILE_COLUMNS)
    return frame, summarize(frame)


def summarize(frame: pd.DataFrame) -> dict:
    counts = {status: 0 for status in STATUSES}
    if frame is not None and not frame.empty:
        for status, count in frame["status"].value_counts().items():
            counts[status] = int(count)
    counts["confirmable"] = (counts[MATCHED] + counts[AMOUNT_MISMATCH]
                             + counts[CONFIRM_ONLY] + counts[STATEMENT_ONLY])
    counts["discrepancies"] = counts[AMOUNT_MISMATCH] + counts[CONFIRM_ONLY] \
        + counts[STATEMENT_ONLY]
    return counts


def reconcile_all(records, trades: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    rows = []
    for record in sorted(records, key=lambda item: item.get("period_end") or ""):
        frame, _ = reconcile(record, trades)
        if not frame.empty:
            rows.extend(frame.to_dict("records"))
    combined = pd.DataFrame(rows, columns=RECONCILE_COLUMNS)
    return combined, summarize(combined)


def render(frame: pd.DataFrame, summary: dict) -> str:
    lines = ["Statement vs confirmation feed", "-" * 92]
    lines.append(f"  matched            {summary[MATCHED]}")
    lines.append(f"  amount mismatch    {summary[AMOUNT_MISMATCH]}")
    lines.append(f"  statement only     {summary[STATEMENT_ONLY]}  (confirmation never ingested)")
    lines.append(f"  confirm only       {summary[CONFIRM_ONLY]}  (statement parser may have missed it)")
    lines.append(f"  not confirmable    {summary[NOT_CONFIRMABLE]}  (dividends, interest, fees, expirations)")
    lines.append(f"  before feed        {summary[BEFORE_FEED]}  (traded before the first confirmation)")

    issues = frame[frame["status"].isin([AMOUNT_MISMATCH, CONFIRM_ONLY, STATEMENT_ONLY])] \
        if frame is not None and not frame.empty else None
    if issues is not None and not issues.empty:
        lines.append("")
        lines.append(f"{'Period':<12}{'Status':<18}{'Key':<24}{'Date':<12}{'Diff':>12}  Note")
        for _, row in issues.iterrows():
            difference = "" if row["difference"] is None or pd.isna(row["difference"]) \
                else fmt_money(row["difference"])
            lines.append(
                f"{str(row['period_end']):<12}{row['status']:<18}{str(row['key'])[:23]:<24}"
                f"{str(row['trade_date']):<12}{difference:>12}  {row['note']}"
            )
    else:
        lines.append("")
        lines.append("No discrepancies.")
    return "\n".join(lines)
