"""Vocabulary shared by parsing, analytics, persistence and the front ends.

Standard library only. store.py and notify.py import this without pulling in
pandas, matplotlib or the PDF stack.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from decimal import Decimal

# Feeds Sharpe and Sortino only. Overridable in the UI and with --rf, and the
# chosen value is stored so it survives a restart.
DEFAULT_RISK_FREE = 0.03

CLASS_COLUMNS = [
    "Cash",
    "Money Market",
    "Equities",
    "ETFs",
    "Mutual Funds",
    "Fixed Income",
    "Options",
    "Other",
]

# One row per Transaction Details / Pending Activity line on a statement.
TXN_COLUMNS = [
    "settled",
    "trade_date",
    "settle_date",
    "category",
    "action",
    "symbol",
    "description",
    "quantity",
    "price",
    "charges",
    "amount",
    "realized",
    "term",
    "is_option",
    "option_type",
    "strike",
    "expiry",
]

TXN_NUMERIC = ("quantity", "price", "charges", "amount", "realized", "strike")

# One row per trade block in a Schwab eConfirms email. Distinct from TXN_COLUMNS:
# a confirm prints commission and industry fee separately and states whether the
# trade opened or closed a position, neither of which a statement row carries.
TRADE_COLUMNS = [
    "seq",
    "trade_date",
    "settle_date",
    "symbol",
    "occ_symbol",
    "description",
    "action",
    "intent",
    "quantity",
    "price",
    "principal",
    "commission",
    "industry_fee",
    "net_amount",
    "is_option",
    "option_type",
    "strike",
    "expiry",
    "cusip",
    "account_tail",
]

TRADE_NUMERIC = (
    "quantity",
    "price",
    "principal",
    "commission",
    "industry_fee",
    "net_amount",
    "strike",
)

PREMIUM_MONTH_COLUMNS = (
    "premium_collected",
    "premium_paid_to_close",
    "premium_net",
    "long_option_purchases",
    "charges",
    "contracts_opened",
    "contracts_closed",
    "trades",
)

# Every exchange-listed option contract covers 100 shares. The statement and the
# confirm both print the per-share price, never the multiplier, so any notional
# figure derived with this is labelled derived wherever it is shown.
OPTION_MULTIPLIER = 100

CREDIT_ACTIONS = ("Sale", "ShortSale", "SellShort")
DEBIT_ACTIONS = ("Purchase", "CoverShort", "BuyToClose", "Buy")

# Gmail resolution and SMTP fail intermittently, and one blip is not news: a single
# failed DNS lookup used to report a whole failed ingestion. Five attempts with a linear
# backoff wait 2+4+6+8 = 20 seconds in total, which has to stay small - cron fires every
# five minutes and run.sh takes a *non-blocking* flock, so a pass that lingers does not
# delay the next tick, it makes that tick skip entirely.
NETWORK_ATTEMPTS = 5
RETRY_BACKOFF = 2.0


def retry(operation, attempts: int = NETWORK_ATTEMPTS, backoff: float = RETRY_BACKOFF,
          on_retry=None):
    """Call `operation()`, retrying failures up to `attempts` times in total.

    Re-raises the last exception, so a caller's own error handling and its message stay
    exactly as they were - the only change is that they are reached once the transient
    explanations are exhausted rather than on the first stumble. `on_retry(attempt, exc)`
    is where the noise goes, so each swallowed attempt is still visible in the log.
    """
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt == attempts:
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            time.sleep(backoff * attempt)


def occ_symbol(symbol: str, expiry: date, option_type: str, strike) -> str | None:
    """OCC contract symbol as Yahoo and the OCC both spell it: SOXL260828P00104000.

    Strike is scaled with Decimal, not float: int(2.5 * 1000) style arithmetic can
    land on a neighbouring strike and produce a valid-looking symbol for the wrong
    contract.
    """
    if not symbol or expiry is None or not option_type or strike is None:
        return None
    letter = str(option_type).strip().upper()[:1]
    if letter not in ("C", "P"):
        return None
    try:
        thousandths = int((Decimal(str(strike)) * 1000).to_integral_value())
    except (ArithmeticError, ValueError):
        return None
    if thousandths <= 0:
        return None
    root = str(symbol).strip().upper().split()[0]
    return f"{root}{expiry.strftime('%y%m%d')}{letter}{thousandths:08d}"


def position_key(symbol: str, is_option: bool, expiry=None, option_type=None, strike=None) -> str | None:
    """Identity used to match a holding to a trade: OCC symbol, or the bare ticker."""
    if is_option:
        return occ_symbol(symbol, expiry, option_type, strike)
    return str(symbol).strip().upper() if symbol else None


def _easter(year: int) -> date:
    """Anonymous Gregorian computus. Needed only to locate Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _friday_holidays(year: int) -> set:
    """Market holidays that can fall on a Friday.

    A holiday on any other weekday cannot shift an expiration, so it is left out. The
    Saturday cases are included because the exchange observes those on the Friday before.
    """
    days = {date(year, 1, 1), _easter(year) - timedelta(days=2)}
    for month, day in ((6, 19), (7, 4), (12, 25)):
        fixed = date(year, month, day)
        days.add(fixed if fixed.weekday() != 5 else fixed - timedelta(days=1))
    return {day for day in days if day.weekday() == 4}


def is_expiry_day(day: date) -> bool:
    """Whether listed options could expire on this date.

    Expirations are Fridays, moving back to Thursday when that Friday is a market holiday.
    Statements print the expiry as MM/DD with no year, so this is what makes the year
    inference safe: without it a January LEAPS reads as the next January, which produces a
    different but entirely valid-looking contract - the same hazard as rounding a strike.
    """
    if day is None:
        return False
    if day.weekday() == 4:
        return day not in _friday_holidays(day.year)
    if day.weekday() == 3:
        return (day + timedelta(days=1)) in _friday_holidays(day.year)
    return False


def _missing(value) -> bool:
    # value != value catches NaN from float, numpy and Decimal without importing them.
    return value is None or value != value


def fmt_money(value):
    return "n/a" if _missing(value) else f"${value:,.2f}"


def fmt_pct(value):
    return "n/a" if _missing(value) else f"{value * 100:.2f}%"


def fmt_ratio(value):
    return "n/a" if _missing(value) else f"{value:.2f}"


def fmt_qty(value):
    return "n/a" if _missing(value) else f"{value:,.0f}"
