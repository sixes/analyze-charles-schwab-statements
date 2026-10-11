"""Pure defensive option-roll screening; cash estimates are not investment returns."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .domain import OPTION_MULTIPLIER, occ_symbol

MAX_DTE = 70
MAX_EXPIRIES = 4
MAX_SPREAD = Decimal("0.50")
DEFAULT_FEE = Decimal("0.65")
DEFAULT_PUT_CREDIT = Decimal("5.00")


def market_date() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def number(value) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def parameters(fee=DEFAULT_FEE, put_min_credit=DEFAULT_PUT_CREDIT):
    fee, credit = number(fee), number(put_min_credit)
    if fee is None or fee < 0:
        raise ValueError("roll fee per leg must be finite and nonnegative")
    if credit is None or credit <= 0:
        raise ValueError("put minimum credit must be finite and strictly positive")
    return fee, credit


def contract_key(symbol, expiry, option_type, strike):
    strike = number(strike)
    if (not isinstance(expiry, date) or not symbol or option_type not in ("PUT", "CALL")
            or strike is None or strike <= 0
            or strike * 1000 != (strike * 1000).to_integral_value()):
        return None
    return occ_symbol(symbol, expiry, option_type, strike)


def eligible_positions(frame, today=None):
    today = today or market_date()
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else (frame or [])
    eligible, skipped = [], []
    for row in rows:
        if not row.get("is_option"):
            continue
        key = row.get("key") or "unresolved option"
        quantity, basis, strike = (number(row.get(field))
                                   for field in ("quantity", "cost_basis", "strike"))
        expiry, kind = row.get("expiry"), row.get("option_type")
        reason = None
        if row.get("status") != "open":
            reason = f"position status {row.get('status')}; confirm the holding first"
        elif quantity is None or not quantity or quantity != quantity.to_integral_value():
            reason = "missing, zero or fractional contract quantity"
        elif not ((kind == "PUT" and quantity < 0) or (kind == "CALL" and quantity > 0)):
            reason = "only short puts and long calls are screened"
        elif basis is None or basis == 0:
            reason = "no reliable recorded basis"
        elif not isinstance(expiry, date) or expiry <= today:
            reason = "missing expiry, expired or expiring today"
        elif expiry >= today + timedelta(days=MAX_DTE):
            reason = "no later expiry within 70 DTE"
        elif contract_key(row.get("symbol"), expiry, kind, strike) != key:
            reason = "unresolved or inconsistent standard contract identity"
        if reason:
            skipped.append(f"{key}: {reason}")
        else:
            eligible.append(dict(row, quantity=quantity, cost_basis=basis, strike=strike))
    return eligible, skipped


def candidate_expiries(expiries, current_expiry, today):
    return sorted({expiry for expiry in expiries
                   if isinstance(expiry, date)
                   and current_expiry < expiry <= today + timedelta(days=MAX_DTE)})[:MAX_EXPIRIES]


def quote_error(quote):
    if not quote:
        return "no Longbridge depth quote"
    if quote.get("error"):
        return str(quote["error"])
    if (quote.get("standard") is not True
            or number(quote.get("multiplier")) != OPTION_MULTIPLIER
            or number(quote.get("contract_size")) != OPTION_MULTIPLIER):
        return "nonstandard or unverified 100-share contract"
    expected = contract_key(quote.get("symbol"), quote.get("expiry"),
                            quote.get("option_type"), quote.get("strike"))
    if expected is None or expected != quote.get("key"):
        return "inconsistent contract metadata"
    bid, ask = number(quote.get("bid")), number(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
        return "missing, nonpositive, nonfinite or crossed bid/ask"
    if (ask - bid) / ((ask + bid) / 2) > MAX_SPREAD:
        return "bid/ask spread exceeds 50% of midpoint"
    return None


def evaluate(positions, market, fee=DEFAULT_FEE, put_min_credit=DEFAULT_PUT_CREDIT,
             today=None):
    fee, put_min_credit = parameters(fee, put_min_credit)
    today = today or market_date()
    alerts, skipped = [], []
    for row in positions:
        key = row["key"]
        book = market.get(key, {})
        current = book.get("current")
        reason = quote_error(current)
        if reason:
            skipped.append(f"{key}: closing leg {reason}")
            continue
        if current["key"] != key:
            skipped.append(f"{key}: closing leg does not match the holding")
            continue
        current_bid, current_ask = number(current["bid"]), number(current["ask"])
        choices, rejected = [], set()
        for candidate in book.get("candidates", []):
            reason = quote_error(candidate)
            if reason:
                rejected.add(reason)
                continue
            if (candidate["symbol"] != row["symbol"]
                    or candidate["option_type"] != row["option_type"]
                    or not row["expiry"] < candidate["expiry"] <= today + timedelta(days=MAX_DTE)
                    or number(candidate["strike"]) > row["strike"]):
                rejected.add("candidate outside underlying, type, strike or expiry policy")
                continue
            bid, ask = number(candidate["bid"]), number(candidate["ask"])
            put = row["option_type"] == "PUT"
            close_price, open_price = (current_ask, bid) if put else (current_bid, ask)
            close_cash = (-1 if put else 1) * close_price * OPTION_MULTIPLIER
            open_cash = (1 if put else -1) * open_price * OPTION_MULTIPLIER
            credit = close_cash + open_cash - 2 * fee
            if credit < (put_min_credit if put else Decimal(0)):
                continue
            quantity = row["quantity"]
            count = abs(quantity)
            choices.append({
                "key": key, "candidate_key": candidate["key"], "symbol": row["symbol"],
                "option_type": row["option_type"], "quantity": quantity,
                "reference_basis": row["cost_basis"], "current": current,
                "original_trade_dates": row.get("original_trade_dates"),
                "candidate": candidate, "close_action": "Buy to close" if put else "Sell to close",
                "open_action": "Sell to open" if put else "Buy to open",
                "close_price": close_price, "open_price": open_price,
                "close_cash_per_contract": close_cash, "open_cash_per_contract": open_cash,
                "fee_per_leg": fee, "fees_per_contract": 2 * fee, "fees_total": 2 * fee * count,
                "net_credit_per_contract": credit, "net_credit_total": credit * count,
                "added_days": (candidate["expiry"] - row["expiry"]).days,
                "dte": (candidate["expiry"] - today).days,
                "current_spread": (current_ask - current_bid) / ((current_ask + current_bid) / 2),
                "candidate_spread": (ask - bid) / ((ask + bid) / 2),
            })
        if choices:
            choices.sort(key=lambda item: (number(item["candidate"]["strike"]),
                                           item["added_days"], -item["net_credit_per_contract"],
                                           item["candidate_key"]))
            alerts.append(choices[0])
        else:
            skipped.append(f"{key}: no qualifying candidate within the evaluated 70-DTE screen")
        if rejected:
            skipped.append(f"{key}: excluded candidate quotes: {'; '.join(sorted(rejected))}")
    return alerts, skipped
