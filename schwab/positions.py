"""Positions rolled forward from the newest statement using trade confirmations.

A statement arrives monthly; confirms arrive the day after each fill. So the newest
statement is the anchor - a full, printed, reconciled position list - and every confirm
trade dated after its period end is applied on top.

What this deliberately does not publish: account value, TWR, IRR or any risk figure.
Confirms carry no cash balance, no margin, no dividend, no transfer and no corporate
action, so a rolled-forward account value would be a number with no printed figure
behind it. Those all stay statement-derived.

The hard limit, surfaced rather than buried: Schwab sends no confirmation for an
expiration, and an expiration cannot be told apart from an assignment. An assigned short
put silently becomes 100 long shares plus a debit that never appears in any email. So an
option past its expiry is dropped as assumed_expired_worthless and carries a warning.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from .domain import OPTION_MULTIPLIER, fmt_money, fmt_qty, is_expiry_day, occ_symbol

POSITION_COLUMNS = [
    "key", "symbol", "label", "asset_class", "is_option", "option_type", "strike", "expiry",
    "quantity", "anchor_quantity", "pending_quantity", "confirm_quantity", "cost_basis",
    "anchor_value", "entry_price", "price", "price_as_of", "market_value", "unrealized",
    "unrealized_pct", "source", "status", "note",
]

EXPIRED = "assumed_expired_worthless"
UNCERTAIN = "uncertain"


def infer_expiry(label: str, reference: date):
    """Statement option rows print the expiry as MM/DD with no year.

    The year is the earliest one at or after the statement period end on which options could
    actually expire, since the option was still held on that date. The expiry-day test is
    what makes this safe: taking the plain next occurrence reads a January LEAPS as the
    coming January, which yields a different but entirely valid-looking contract. A date no
    candidate year can justify is returned as the nearest one anyway, so the position stays
    visible and unpriced instead of silently vanishing.
    """
    if not label or reference is None:
        return None
    parts = str(label).split()
    if not parts:
        return None
    tail = parts[-1]
    if "/" not in tail:
        return None
    pieces = tail.split("/")
    if len(pieces) != 2:
        return None
    try:
        month, day = int(pieces[0]), int(pieces[1])
    except ValueError:
        return None
    candidates = []
    for offset in range(0, 4):
        try:
            candidate = date(reference.year + offset, month, day)
        except ValueError:
            continue
        if candidate >= reference:
            candidates.append(candidate)
    for candidate in candidates:
        if is_expiry_day(candidate):
            return candidate
    return candidates[0] if candidates else None


def anchor_positions(record: dict) -> dict:
    """Keyed open positions from one statement's holdings section."""
    period_end = record.get("period_end")
    positions = {}
    for holding in record.get("_holdings") or []:
        symbol = holding.get("symbol")
        if not symbol:
            continue
        is_option = holding.get("asset_class") == "Options" or holding.get("option_type")
        expiry = holding.get("expiry")
        if is_option and expiry is None:
            expiry = infer_expiry(holding.get("label"), period_end)
        if is_option:
            key = occ_symbol(symbol, expiry, holding.get("option_type"), holding.get("strike"))
            if not key:
                # Without a resolvable contract the row cannot be matched to a confirm or
                # quoted; keep it visible under its printed label instead of dropping it.
                key = f"?{holding.get('label') or symbol}"
        else:
            key = str(symbol).strip().upper()
        entry = positions.setdefault(key, {
            "key": key,
            "symbol": str(symbol).strip().upper(),
            "label": holding.get("label") or symbol,
            "asset_class": holding.get("asset_class"),
            "is_option": bool(is_option),
            "option_type": holding.get("option_type"),
            "strike": holding.get("strike"),
            "expiry": expiry,
            "anchor_quantity": 0.0,
            "pending_quantity": 0.0,
            "confirm_quantity": 0.0,
            "cost_basis": 0.0,
            "anchor_value": 0.0,
            "source": "statement",
            "uncertain": False,
        })
        entry["anchor_quantity"] += holding.get("quantity") or 0.0
        if holding.get("cost_basis") is not None:
            entry["cost_basis"] += holding["cost_basis"]
        if holding.get("market_value") is not None:
            entry["anchor_value"] += holding["market_value"]

    # Pending / Open Activity. These trades filled inside the period but settle after it,
    # so the holdings section and the account value both leave them out while the exposure
    # is already real. They are valued at their own trade price: the credit or debit they
    # produced sits outside the account value too, so the two cancel and only price
    # movement after the statement shows up as a change.
    for row in record.get("_pending") or []:
        symbol = row.get("symbol")
        change = signed_quantity(row.get("category") or row.get("action"), row.get("quantity"))
        if not symbol or change is None:
            continue
        is_option = bool(row.get("is_option"))
        if is_option:
            key = occ_symbol(symbol, row.get("expiry"), row.get("option_type"), row.get("strike"))
            if not key:
                key = f"?{row.get('description') or symbol}"
        else:
            key = str(symbol).strip().upper()
        entry = positions.setdefault(key, {
            "key": key,
            "symbol": str(symbol).strip().upper(),
            "label": row.get("description") or key,
            "asset_class": "Options" if is_option else None,
            "is_option": is_option,
            "option_type": row.get("option_type"),
            "strike": row.get("strike"),
            "expiry": row.get("expiry"),
            "anchor_quantity": 0.0,
            "pending_quantity": 0.0,
            "confirm_quantity": 0.0,
            "cost_basis": 0.0,
            "anchor_value": 0.0,
            "source": "statement pending",
            "uncertain": False,
        })
        if entry["source"] == "statement":
            entry["source"] = "statement+pending"
        entry["pending_quantity"] += change
        price = row.get("price")
        if price is not None:
            multiplier = OPTION_MULTIPLIER if is_option else 1
            entry["anchor_value"] += change * float(price) * multiplier
        if entry["anchor_quantity"] == 0.0 and row.get("amount") is not None:
            entry["cost_basis"] += -float(row["amount"])
    return positions


def signed_quantity(action: str, quantity) -> float | None:
    """Quantity delta under the statement's convention, where shorts are negative.

    The delta follows from the action alone: a sale is -qty whether it opens a short or
    closes a long, and a purchase is +qty whether it opens a long or covers a short. The
    open/close intent changes how the cash is attributed, not which way the position
    moves, so a missing intent is flagged on the row instead of discarding a known delta.
    """
    if quantity is None or not action:
        return None
    magnitude = abs(float(quantity))
    verb = str(action).strip().lower()
    if verb.startswith("sale") or verb.startswith("sell"):
        return -magnitude
    if verb.startswith("purchase") or verb.startswith("buy"):
        return magnitude
    return None


def rollforward(records, trades, marks=None, today: date = None) -> pd.DataFrame:
    """Anchor holdings from the newest statement plus every later confirm trade."""
    today = today or date.today()
    marks = marks or {}
    if not records:
        return pd.DataFrame(columns=POSITION_COLUMNS)

    anchor = max(records, key=lambda record: record.get("period_end") or date.min)
    period_end = anchor.get("period_end")
    positions = anchor_positions(anchor)

    if trades is not None and not getattr(trades, "empty", True):
        later = trades
        if period_end is not None and "trade_date" in later:
            later = later[later["trade_date"].notna()]
            later = later[[value > period_end for value in later["trade_date"]]]
        for _, trade in later.iterrows():
            symbol = trade.get("symbol")
            if not symbol:
                continue
            is_option = bool(trade.get("is_option"))
            key = trade.get("occ_symbol") if is_option else str(symbol).strip().upper()
            if not key:
                continue
            entry = positions.setdefault(key, {
                "key": key,
                "symbol": str(symbol).strip().upper(),
                "label": trade.get("description") or key,
                "asset_class": "Options" if is_option else None,
                "is_option": is_option,
                "option_type": trade.get("option_type"),
                "strike": trade.get("strike"),
                "expiry": trade.get("expiry"),
                "anchor_quantity": 0.0,
                "pending_quantity": 0.0,
                "confirm_quantity": 0.0,
                "cost_basis": 0.0,
                "anchor_value": 0.0,
                "source": "confirm",
                "uncertain": False,
            })
            if entry["source"] == "statement":
                entry["source"] = "statement+confirm"
            change = signed_quantity(trade.get("action"), trade.get("quantity"))
            if trade.get("intent") not in ("open", "close"):
                entry["uncertain"] = True
            if change is None:
                entry["uncertain"] = True
                continue
            entry["confirm_quantity"] += change
            net = trade.get("net_amount")
            held = (entry["anchor_quantity"] or 0.0) + (entry["pending_quantity"] or 0.0)
            if net is not None and held == 0.0:
                # A confirm-opened short's basis is the credit received.
                entry["cost_basis"] += -float(net)

    rows = []
    for entry in positions.values():
        quantity = ((entry["anchor_quantity"] or 0.0) + (entry["pending_quantity"] or 0.0)
                    + (entry["confirm_quantity"] or 0.0))
        expiry = entry.get("expiry")
        status = "open"
        note = ""
        if entry["uncertain"]:
            status = UNCERTAIN
            note = "a confirm gave no open/close intent; cost basis may be misattributed"
        if entry["is_option"] and expiry is not None and expiry < today and abs(quantity) > 1e-9:
            status = EXPIRED
            note = ("expired without a confirmation; an assignment is indistinguishable"
                    " from an expiration in email")
        elif abs(quantity) < 1e-9 and status == "open":
            continue

        mark = marks.get(entry["key"]) or {}
        price = mark.get("price")
        as_of = mark.get("as_of")
        if status == EXPIRED:
            market_value = 0.0
            price = 0.0 if price is None else price
        elif price is None:
            market_value = None
        else:
            multiplier = OPTION_MULTIPLIER if entry["is_option"] else 1
            market_value = quantity * float(price) * multiplier

        cost_basis = entry["cost_basis"] or None
        unrealized = None
        if market_value is not None and cost_basis is not None:
            unrealized = market_value - cost_basis

        multiplier = OPTION_MULTIPLIER if entry["is_option"] else 1
        entry_price = None
        if cost_basis is not None and abs(quantity) > 1e-9:
            entry_price = cost_basis / (quantity * multiplier)
        # Against the magnitude of the basis: a short's basis is a credit, so this reads as
        # the share of premium captured rather than as a negative denominator.
        unrealized_pct = None
        if unrealized is not None and cost_basis:
            unrealized_pct = unrealized / abs(cost_basis)

        buckets = [name for name, value in (("statement", entry["anchor_quantity"]),
                                            ("pending", entry["pending_quantity"]),
                                            ("confirm", entry["confirm_quantity"]))
                   if value]
        rows.append({
            "key": entry["key"],
            "symbol": entry["symbol"],
            "label": entry["label"],
            "asset_class": entry["asset_class"],
            "is_option": entry["is_option"],
            "option_type": entry["option_type"],
            "strike": entry["strike"],
            "expiry": expiry,
            "quantity": quantity,
            "anchor_quantity": entry["anchor_quantity"],
            "pending_quantity": entry["pending_quantity"],
            "confirm_quantity": entry["confirm_quantity"],
            "cost_basis": cost_basis,
            "anchor_value": entry["anchor_value"],
            "entry_price": entry_price,
            "price": price,
            "price_as_of": as_of,
            "market_value": market_value,
            "unrealized": unrealized,
            "unrealized_pct": unrealized_pct,
            "source": "+".join(buckets) or entry["source"],
            "status": status,
            "note": note,
        })

    frame = pd.DataFrame(rows, columns=POSITION_COLUMNS)
    if frame.empty:
        return frame
    return frame.sort_values(["is_option", "symbol", "expiry"], na_position="last") \
                .reset_index(drop=True)


def quote_symbols(frame: pd.DataFrame) -> list[str]:
    """Symbols worth quoting: open rows with a resolvable market identifier."""
    if frame is None or frame.empty:
        return []
    live = frame[frame["status"] != EXPIRED]
    keys = [key for key in live["key"] if key and not str(key).startswith("?")]
    return sorted(set(keys))


def marked_total(frame: pd.DataFrame):
    """(total, complete). Incomplete when any open leg has no mark.

    An incomplete sum is reported as such rather than added up, because a missing short
    option leg makes the total look better than the account is.
    """
    if frame is None or frame.empty:
        return 0.0, True
    live = frame[frame["status"] != EXPIRED]
    complete = bool(live["market_value"].notna().all())
    total = float(live["market_value"].dropna().sum())
    return total, complete


def interim_performance(records, trades, marks=None, frame=None, today: date = None) -> dict:
    """Position-level profit and loss between the newest statement and today.

    This is deliberately not a return and must never be linked into the statement TWR
    series: confirmations carry no cash balance, deposit, dividend or corporate-action
    data, so the denominator a return needs does not exist between statements. What can be
    measured is the change in the value of the positions themselves plus the cash the
    confirmations printed, which is what this reports.
    """
    empty = {
        "anchor_date": None, "anchor_account_value": None, "anchor_position_value": None,
        "current_position_value": None, "value_change": None, "trade_cash": 0.0,
        "pnl": None, "pnl_pct": None, "trade_count": 0, "complete": False, "assumptions": [],
    }
    if not records:
        return empty

    today = today or date.today()
    anchor = max(records, key=lambda record: record.get("period_end") or date.min)
    period_end = anchor.get("period_end")
    if frame is None:
        frame = rollforward(records, trades, marks=marks, today=today)

    # Summed from the anchor holdings, not the frame: a position a confirmation closed out
    # entirely is dropped from the frame, and its opening value has to survive that.
    anchor_value = sum(entry["anchor_value"] for entry in anchor_positions(anchor).values())

    trade_cash = 0.0
    trade_count = 0
    if trades is not None and not getattr(trades, "empty", True):
        later = trades[trades["trade_date"].notna()]
        if period_end is not None:
            later = later[[value > period_end for value in later["trade_date"]]]
        trade_count = len(later)
        trade_cash = float(later["net_amount"].dropna().sum())

    current_value, complete = marked_total(frame)
    assumptions = []
    if not frame.empty:
        expired = frame[frame["status"] == EXPIRED]
        if not expired.empty:
            assumptions.append(
                f"{len(expired)} option(s) past expiry are valued at zero; an assignment is "
                "indistinguishable from an expiration in the confirmation feed"
            )
        uncertain = frame[frame["status"] == UNCERTAIN]
        if not uncertain.empty:
            assumptions.append(
                f"{len(uncertain)} position(s) came from a confirmation with no open/close "
                "intent, so their cost basis may be misattributed"
            )
    if not complete:
        assumptions.append(
            "at least one open leg has no market mark, so the profit figure reads n/a rather "
            "than a total that silently omits a short option liability"
        )

    account_value = anchor.get("ending_value")
    value_change = (current_value - anchor_value) if complete else None
    pnl = (value_change + trade_cash) if value_change is not None else None
    pnl_pct = None
    if pnl is not None and account_value:
        pnl_pct = pnl / float(account_value)

    return {
        "anchor_date": period_end,
        "anchor_account_value": account_value,
        "anchor_position_value": anchor_value,
        "current_position_value": current_value if complete else None,
        "value_change": value_change,
        "trade_cash": trade_cash,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "trade_count": trade_count,
        "complete": complete,
        "assumptions": assumptions,
    }


def warnings(frame: pd.DataFrame) -> list[str]:
    out = []
    if frame is None or frame.empty:
        return out
    expired = frame[frame["status"] == EXPIRED]
    for _, row in expired.iterrows():
        out.append(f"{row['key']}: {row['note']}")
    uncertain = frame[frame["status"] == UNCERTAIN]
    for _, row in uncertain.iterrows():
        out.append(f"{row['key']}: {row['note']}")
    unpriced = frame[(frame["status"] != EXPIRED) & (frame["market_value"].isna())]
    if not unpriced.empty:
        out.append(f"{len(unpriced)} position(s) have no market mark; "
                   "marked totals read n/a (incomplete)")
    return out


def render(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty:
        return "No open positions."
    lines = [
        f"{'Position':<24}{'Qty':>10}{'Mark':>12}{'Value':>16}{'Unrealized':>14}  Status",
        "-" * 92,
    ]
    for _, row in frame.iterrows():
        value = "n/a" if row["market_value"] is None or pd.isna(row["market_value"]) \
            else fmt_money(row["market_value"])
        unrealized = "n/a" if row["unrealized"] is None or pd.isna(row["unrealized"]) \
            else fmt_money(row["unrealized"])
        mark = "n/a" if row["price"] is None or pd.isna(row["price"]) else fmt_money(row["price"])
        lines.append(
            f"{str(row['key'])[:23]:<24}{fmt_qty(row['quantity']):>10}{mark:>12}"
            f"{value:>16}{unrealized:>14}  {row['status']}"
        )
    total, complete = marked_total(frame)
    lines.append("-" * 92)
    lines.append(f"{'Marked value':<24}{'':>10}{'':>12}"
                 f"{fmt_money(total) if complete else 'n/a (incomplete)':>16}")
    lines.append("Marks are delayed third-party quotes, not statement figures.")
    return "\n".join(lines)
