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

from datetime import date, datetime, timedelta
from math import isfinite

import pandas as pd

from .domain import OPTION_MULTIPLIER, fmt_money, fmt_qty, is_expiry_day, occ_symbol, position_key

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


def opening_trade_dates(records, trades, book) -> dict[str, tuple[date, ...] | None]:
    """Infer dates only from confirmations reconciled to statement exposure, without lot allocation."""
    def calendar_date(value):
        if not isinstance(value, date) or pd.isna(value):
            return None
        return value.date() if isinstance(value, datetime) else value

    def finite_quantity(value):
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if isfinite(value) else None

    keys = {row["key"] for row in book}
    if not keys:
        return {}
    snapshots = []
    for record in records or []:
        end = calendar_date(record.get("period_end"))
        if end is not None:
            anchored = anchor_positions(record)
            quantities = {
                key: entry["anchor_quantity"] + entry["pending_quantity"]
                for key, entry in anchored.items()
            }
            snapshots.append((end, quantities))
    snapshots.sort(key=lambda snapshot: snapshot[0])

    by_key = {key: [] for key in keys}
    rows = trades.to_dict("records") if isinstance(trades, pd.DataFrame) else (trades or [])
    for trade in rows:
        key = trade.get("occ_symbol")
        if key in by_key:
            by_key[key].append(trade)

    def reconstruct(key, current):
        current = finite_quantity(current)
        if current is None or abs(current) < 1e-9:
            return None
        flat_date = next((end for end, quantities in reversed(snapshots)
                          if abs(quantities.get(key, 0.0)) < 1e-9), None)
        if flat_date is None:
            # Even an explicit opening cannot prove there was no earlier exposure.
            return None

        # End-of-day snapshots follow same-day confirmations in their supplied order.
        events = [(end, True, quantities.get(key, 0.0))
                  for end, quantities in snapshots if end > flat_date]
        for trade in by_key[key]:
            traded = calendar_date(trade.get("trade_date"))
            if traded is None:
                # An undated row cannot safely be placed on either side of the anchor.
                return None
            if traded > flat_date:
                events.append((traded, False, trade))
        events.sort(key=lambda event: (event[0], event[1]))

        quantity = 0.0
        dates = set()
        for traded, is_snapshot, item in events:
            if is_snapshot:
                expected = finite_quantity(item)
                if expected is None or abs(quantity - expected) > 1e-9:
                    return None
                continue
            intent = item.get("intent")
            if not isinstance(intent, str) or intent not in ("open", "close"):
                return None
            try:
                change = signed_quantity(item.get("action"), item.get("quantity"))
            except (TypeError, ValueError, OverflowError):
                return None
            if change is None or not isfinite(change) or abs(change) < 1e-9:
                return None
            if intent == "open":
                if quantity * change < 0:
                    return None
                if dates is not None:
                    dates.add(traded)
            else:
                if quantity * change >= 0 or abs(change) > abs(quantity) + 1e-9:
                    return None
                if dates is not None and len(dates) > 1:
                    dates = None
            quantity += change
            if abs(quantity) < 1e-9:
                quantity = 0.0
                dates = set()

        if abs(quantity - current) > 1e-9 or not dates:
            return None
        return tuple(sorted(dates))

    return {row["key"]: reconstruct(row["key"], row.get("quantity")) for row in book}


def original_trade_context(records, history, current_trades, email_index=None) -> list[dict]:
    """Reconstruct one historical opening fill per Current row without allocating lots."""
    epsilon = 1e-9

    def text(value):
        return value.strip() if isinstance(value, str) and value.strip() else None

    def calendar_date(value):
        if not isinstance(value, date) or pd.isna(value):
            return None
        return value.date() if isinstance(value, datetime) else value

    def number(value):
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if isfinite(value) else None

    def identity(row):
        digest, seq = text(row.get("body_sha256")), number(row.get("seq"))
        if not digest or seq is None or seq < 0 or not seq.is_integer():
            return None
        return digest, int(seq)

    def instrument(row, end=None, holding=False):
        symbol = text(row.get("symbol"))
        if not symbol:
            return None
        if holding:
            kind = text(row.get("asset_class"))
            option = kind == "Options" or bool(text(row.get("option_type")))
            if not kind and not option:
                return None
        else:
            flag = row.get("is_option")
            if flag is None or pd.isna(flag) or flag not in (True, False):
                return None
            option = bool(flag)
            if not option and text(row.get("option_type")):
                return None
        expiry = calendar_date(row.get("expiry"))
        if holding and option and row.get("expiry") is None:
            expiry = infer_expiry(row.get("label"), end)
            if expiry is None or not is_expiry_day(expiry):
                return None
        if option and (expiry is None or number(row.get("strike")) is None):
            return None
        key = position_key(symbol, option, expiry, text(row.get("option_type")),
                           row.get("strike"))
        printed = text(row.get("occ_symbol"))
        return None if printed and printed != key else key

    def relevant(row, key, symbol, row_key):
        # An unresolved row for this root (or with no root) might be this contract.
        root = text(row.get("symbol"))
        return (row_key == key or (row_key is None and
                (not root or root.upper() == symbol or text(row.get("occ_symbol")) == key)))

    def delta(action, size):
        size = number(size)
        verb = (text(action) or "").lower()
        if size is None or size <= epsilon:
            return None
        if verb in ("sale", "sell", "shortsale", "sellshort"):
            return signed_quantity("Sale", size)
        if verb in ("purchase", "buy", "covershort", "buytoclose"):
            return signed_quantity("Purchase", size)
        return None

    def fill_change(row):
        intent = text(row.get("intent"))
        if intent not in ("open", "close"):
            return None, "Missing or invalid open/close intent"
        verb = (text(row.get("action")) or "").lower()
        if ((verb in ("shortsale", "sellshort") and intent != "open") or
                (verb in ("covershort", "buytoclose") and intent != "close")):
            return None, "Action conflicts with open/close intent"
        change = delta(row.get("action"), row.get("quantity"))
        return change, "Invalid action or quantity" if change is None else ""

    def compatible(quantity, change, intent):
        if intent == "open":
            return quantity * change >= 0
        return quantity * change < 0 and abs(change) <= abs(quantity) + epsilon

    def snapshot(record, key, symbol, end):
        # Unresolved holdings or skipped pending rows cannot establish flatness.
        selected = {"period_end": end, "_holdings": [], "_pending": []}
        for section in ("_holdings", "_pending"):
            rows = record.get(section)
            if not isinstance(rows, (list, tuple)):
                return None
            for row in rows:
                holding = section == "_holdings"
                row_key = instrument(row, end, holding)
                if not relevant(row, key, symbol, row_key):
                    continue
                quantity = number(row.get("quantity"))
                if row_key is None or quantity is None:
                    return None
                # Missing cost/value must not affect quantity-only reconciliation.
                clean = {field: row.get(field) for field in
                         ("symbol", "label", "asset_class", "is_option", "option_type",
                          "strike", "expiry")}
                clean["quantity"] = quantity
                if holding:
                    if clean["expiry"] is None and row.get("asset_class") == "Options":
                        clean["expiry"] = infer_expiry(row.get("label"), end)
                else:
                    change = delta(row.get("category") or row.get("action"), quantity)
                    if change is None:
                        return None
                    clean["action"] = "Sale" if change < 0 else "Purchase"
                selected[section].append(clean)
        entry = anchor_positions(selected).get(key)
        return number(entry["anchor_quantity"] + entry["pending_quantity"]) if entry else 0.0

    def result(status, reason, original=None):
        return {"status": status, "original": original, "reason": reason}

    historical = history.to_dict("records") if isinstance(history, pd.DataFrame) else list(history or [])
    current = list(current_trades or [])
    rows = historical + current
    indexed_emails = list(email_index or [])
    dated_records = [(calendar_date(record.get("period_end")), record)
                     for record in records or []]

    def resolve(target):
        day = calendar_date(target.get("trade_date"))
        target_id = identity(target)
        key = instrument(target)
        if day is None:
            return result("unavailable", "Trade date unavailable")
        if target_id is None:
            return result("unavailable", "Fill identity unavailable")
        if key is None:
            return result("unavailable", "Instrument identity unavailable")
        change, problem = fill_change(target)
        if problem:
            return result("unavailable", problem)
        symbol = text(target.get("symbol")).upper()
        if any(end is None for end, _ in dated_records):
            return result("unavailable", "Statement date unavailable")
        earlier = [(end, record) for end, record in dated_records if end < day]

        # Conflicting known tails make account-less statements ambiguous across tickers.
        scoped = [row for row in rows if calendar_date(row.get("trade_date")) is None
                  or calendar_date(row.get("trade_date")) <= day]
        for _, record in earlier:
            scoped.append(record)
            scoped.extend(record.get("_holdings") or [])
            scoped.extend(record.get("_pending") or [])
        tails = {text(row.get("account_tail")) for row in scoped}
        tails.discard(None)
        if len(tails) > 1:
            return result("unavailable", "Conflicting account scope")

        snapshots = {}
        for end, record in earlier:
            quantity = snapshot(record, key, symbol, end)
            if end in snapshots and (quantity is None or snapshots[end] is None or
                                     abs(snapshots[end] - quantity) > epsilon):
                snapshots[end] = None
            else:
                snapshots[end] = quantity
        flat = max((end for end, quantity in snapshots.items()
                    if quantity is not None and abs(quantity) < epsilon), default=None)
        if flat is None:
            return result("unavailable", "No earlier flat statement")

        fills = {}
        for row in rows:
            row_id = identity(row)
            if row_id == target_id:
                continue
            traded = calendar_date(row.get("trade_date"))
            if traded is not None and (traded <= flat or traded > day):
                continue
            row_key = instrument(row)
            if not relevant(row, key, symbol, row_key):
                continue
            if traded is None:
                return result("unavailable", "Undated relevant fill")
            if row_key is None:
                return result("unavailable", "Unresolved instrument in history")
            if row_id is None:
                return result("unavailable", "Historical fill identity unavailable")
            if traded == day:
                return result("unavailable", "Other same-instrument fills on trade date")
            if row_id in fills:
                # Stored and notification copies share identity, not permission to disagree.
                previous = fills[row_id]
                fields = ("action", "intent", "account_tail")
                amounts = ("quantity", "price", "net_amount")
                if (calendar_date(previous.get("trade_date")) != traded or
                        any(text(previous.get(field)) != text(row.get(field)) for field in fields) or
                        any(number(previous.get(field)) != number(row.get(field)) for field in amounts)):
                    return result("unavailable", "Conflicting copies of historical fill")
            else:
                fills[row_id] = row

        # Unidentified failed blocks may hide same-day fills reported in next-day eConfirms.
        horizon = day + timedelta(days=1)
        target_headers = [calendar_date(target.get("confirm_date"))]
        target_headers.extend(calendar_date(email.get("confirm_date")) for email in indexed_emails
                              if text(email.get("body_sha256")) == target_id[0])
        horizon = max([horizon] + [value for value in target_headers if value is not None])
        digests = {row_id[0] for row_id in fills} | {target_id[0]}
        for email in indexed_emails:
            if text(email.get("status")) == "ok" and not text(email.get("error")):
                continue
            confirmed = calendar_date(email.get("confirm_date"))
            if (text(email.get("body_sha256")) in digests or confirmed is None or
                    flat < confirmed <= horizon):
                return result("unavailable", "Known incomplete confirmation history")

        events = [(end, True, quantity) for end, quantity in snapshots.items() if end > flat]
        counts = {}
        for row in fills.values():
            traded = calendar_date(row.get("trade_date"))
            counts[traded] = counts.get(traded, 0) + 1
            if counts[traded] > 1:
                return result("unavailable", "Ambiguous same-day historical fills")
            events.append((traded, False, row))
        events.sort(key=lambda event: (event[0], event[1]))
        quantity, openings = 0.0, []
        for _, is_snapshot, item in events:
            if is_snapshot:
                if item is None:
                    return result("unavailable", "Unresolved statement exposure")
                if abs(quantity - item) > epsilon:
                    return result("unavailable", "Statement quantity disagrees with history")
                continue
            step, problem = fill_change(item)
            if problem:
                return result("unavailable", problem)
            if not compatible(quantity, step, text(item.get("intent"))):
                return result("unavailable", "History conflicts with position direction or size")
            if text(item.get("intent")) == "open":
                openings.append(item)
            quantity += step
            if not isfinite(quantity):
                return result("unavailable", "Invalid cumulative quantity")
            if abs(quantity) < epsilon:
                quantity, openings = 0.0, []

        if not compatible(quantity, change, text(target.get("intent"))):
            return result("unavailable", "Current conflicts with position direction or size")
        if not quantity:
            return result("none", "No prior position")
        if len(openings) != 1:
            return result("unavailable", "Multiple opening fills in active position")
        return result("verified", "Reconstructed from available confirmations", openings[0])

    return [resolve(target) for target in current]


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
