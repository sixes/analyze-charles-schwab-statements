"""Market marks from yfinance.

The only module that talks to the market. Quotes are delayed third-party marks, they
are labelled as such wherever they surface, and they never enter TWR, IRR or any risk
statistic - those must keep tying back to figures printed on a statement.

A symbol that cannot be priced stays None. Never zero, never the last statement price:
a silently short sum understates short-option liability, which is the single error that
would matter most here.

Privacy: this sends ticker and OCC contract symbols to Yahoo, which reveals what is
held. Narrower than uploading a statement, but still a new outbound flow - the CLI and
the app both offer --no-quotes / a toggle to switch it off.
"""

from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

SOURCE = "yfinance"


def _import_yfinance():
    try:
        import yfinance
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(f"yfinance unavailable: {exc}") from exc
    return yfinance


def _from_fast_info(ticker):
    try:
        info = ticker.fast_info
    except Exception:
        return None, None
    for key in ("lastPrice", "last_price", "regularMarketPrice", "previousClose"):
        try:
            value = info[key]
        except Exception:
            continue
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value, date.today()
    return None, None


def _from_history(ticker):
    try:
        frame = ticker.history(period="5d", auto_adjust=False)
    except Exception:
        return None, None
    if frame is None or getattr(frame, "empty", True) or "Close" not in frame:
        return None, None
    closes = frame["Close"].dropna()
    if closes.empty:
        return None, None
    stamp = closes.index[-1]
    as_of = stamp.date() if hasattr(stamp, "date") else None
    try:
        return float(closes.iloc[-1]), as_of
    except (TypeError, ValueError):
        return None, None


def fetch_quotes(symbols) -> dict:
    """{symbol: {price, as_of, source, error}}. Never raises."""
    wanted = [str(symbol).strip().upper() for symbol in symbols if symbol]
    unique = sorted(set(wanted))
    if not unique:
        return {}

    try:
        yfinance = _import_yfinance()
    except RuntimeError as exc:
        return {symbol: {"price": None, "as_of": None, "source": SOURCE, "error": str(exc)}
                for symbol in unique}

    results = {}
    for symbol in unique:
        price = as_of = None
        error = None
        try:
            ticker = yfinance.Ticker(symbol)
            price, as_of = _from_fast_info(ticker)
            if price is None:
                price, as_of = _from_history(ticker)
            if price is None:
                error = "no price returned"
        except Exception as exc:
            error = str(exc)[:200]
        if price is not None and as_of is None:
            as_of = date.today()
        results[symbol] = {"price": price, "as_of": as_of, "source": SOURCE, "error": error}
        if price is None:
            log.info("no quote for %s: %s", symbol, error)
    return results
