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

import fcntl
import json
import logging
import math
import os
import re
import tempfile
import time
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path

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


class LongbridgeQuota:
    def __init__(self, path=None, clock=time.time, wait=time.sleep):
        self.path = Path(path) if path else Path(__file__).resolve().parent.parent / ".run/longbridge-quota.json"
        self.clock, self.wait = clock, wait

    def reserve(self, contracts=0):
        if not isinstance(contracts, int) or not 0 <= contracts <= 500:
            raise ValueError("contract reservation must be an integer between 0 and 500")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            events = json.loads(self.path.read_text()) if self.path.exists() else []
            if not isinstance(events, list) or any(
                not isinstance(event, list) or len(event) != 2
                or not isinstance(event[0], (int, float)) or not math.isfinite(event[0])
                or not isinstance(event[1], int) or not 0 <= event[1] <= 500
                for event in events
            ):
                raise ValueError("invalid Longbridge quota state; refusing requests")
            while True:
                now = self.clock()
                events = sorted(event for event in events if event[0] > now - 60)
                recent = [stamp for stamp, _ in events if stamp > now - 1]
                wait = max(0, recent[-8] + 1 - now) if len(recent) >= 8 else 0
                used = sum(cost for _, cost in events)
                for stamp, cost in events:
                    if used + contracts <= 500:
                        break
                    used -= cost
                    wait = max(wait, stamp + 60 - now)
                if wait > 0:
                    self.wait(wait + 0.001)
                    continue
                events.append([now, contracts])
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent,
                                                     prefix=".longbridge-quota-", delete=False) as handle:
                        temporary = Path(handle.name)
                        json.dump(events, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.path)
                    directory = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return


def _longbridge_context():
    names = ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN")
    if any(not os.environ.get(name, "").strip() for name in names):
        raise RuntimeError("Longbridge credentials missing: set " + ", ".join(names))
    try:
        from longbridge.openapi import Config, QuoteContext
    except ImportError:
        raise RuntimeError("Longbridge SDK missing; install requirements.txt") from None
    # Explicit values avoid the SDK loading another .env or redirecting credential traffic.
    return QuoteContext(Config(*(os.environ[name] for name in names)))


class OptionMetadataError(ValueError):
    pass


def _longbridge_error(exc):
    if isinstance(exc, OptionMetadataError):
        return f"Longbridge option metadata rejected: {exc}"
    message = str(exc)
    if "401003" in message:
        return "Longbridge access token expired (401003); renew LONGBRIDGE_ACCESS_TOKEN"
    if "301606" in message:
        return "Longbridge rate limit reached (301606); scan deferred until a later pass"
    return (f"Longbridge {type(exc).__name__}: request unavailable; verify credentials, "
            "OPRA US Options Quotes (OpenAPI) permission and API quota")


def fetch_roll_market(positions, today=None, context=None, limiter=None):
    from . import rolls

    today = today or rolls.market_date()
    market = {row["key"]: {"current": None, "candidates": []} for row in positions}
    diagnostics = []
    if not positions:
        return market, diagnostics
    if context is None:
        try:
            context = _longbridge_context()
        except RuntimeError as exc:
            diagnostics.append(str(exc) if str(exc).startswith(("Longbridge credentials missing",
                                                                  "Longbridge SDK missing"))
                               else _longbridge_error(exc))
            return market, diagnostics
        except Exception as exc:
            return market, [_longbridge_error(exc)]
    limiter = limiter or LongbridgeQuota()
    used = 0
    throttled = False
    chains, expiries, quotes = {}, {}, {}

    def request(method, *args, contracts=0):
        nonlocal used, throttled
        if throttled or used + contracts > 500:
            raise RuntimeError("Longbridge scan deferred by quota")
        limiter.reserve(contracts)
        used += contracts
        try:
            return method(*args)
        except Exception as exc:
            if "301606" in str(exc):
                throttled = True
            raise

    def chain(symbol, expiry):
        pair = (symbol, expiry)
        if pair not in chains:
            try:
                chains[pair] = request(context.option_chain_info_by_date, symbol + ".US", expiry)
            except Exception as exc:
                diagnostics.append(f"{symbol} {expiry}: {_longbridge_error(exc)}")
                chains[pair] = []
        return chains[pair]

    def contracts(row, expiry):
        found = []
        for item in chain(row["symbol"], expiry):
            strike = rolls.number(item.price)
            if not item.standard or strike is None or strike > row["strike"]:
                continue
            provider_symbol = item.put_symbol if row["option_type"] == "PUT" else item.call_symbol
            key = rolls.contract_key(row["symbol"], expiry, row["option_type"], strike)
            if not provider_symbol or not key:
                continue
            found.append({"provider_symbol": provider_symbol, "key": key,
                          "symbol": row["symbol"], "expiry": expiry, "strike": strike,
                          "option_type": row["option_type"], "standard": True})
        return found

    def quote(descriptor):
        symbol = descriptor["provider_symbol"]
        if symbol not in quotes:
            result = dict(descriptor)
            try:
                metadata = request(context.option_quote, [symbol], contracts=1)
                item = next((item for item in metadata if item.symbol == symbol), None)
                if item is None:
                    raise OptionMetadataError("no contract metadata returned")
                identity = re.fullmatch(
                    re.escape(descriptor["symbol"]) + r"(\d{6})([CP])(\d+)\.US", symbol)
                if (identity is None or identity[1] != descriptor["expiry"].strftime("%y%m%d")
                        or identity[2] != descriptor["option_type"][0]
                        or int(identity[3]) != descriptor["strike"] * 1000):
                    raise OptionMetadataError("contract symbol disagrees with the standard chain")
                # This SDK decodes direction as Unknown; the chain and encoded symbol still identify it.
                direction = str(item.direction).rsplit(".", 1)[-1].upper()
                if direction not in ("UNKNOWN", descriptor["option_type"]):
                    raise OptionMetadataError("option direction disagrees with the standard chain")
                checks = (
                    ("underlying", item.underlying_symbol, descriptor["symbol"] + ".US"),
                    ("expiry", item.expiry_date, descriptor["expiry"]),
                    ("strike", rolls.number(item.strike_price), descriptor["strike"]),
                    ("multiplier", rolls.number(item.contract_multiplier), 100),
                    ("contract size", rolls.number(item.contract_size), 100),
                    ("trading status", str(item.trade_status).rsplit(".", 1)[-1], "Normal"),
                )
                for field, actual, expected in checks:
                    if actual != expected:
                        raise OptionMetadataError(f"{field} disagrees with the standard active contract")
                result.update(multiplier=item.contract_multiplier, contract_size=item.contract_size)
                depth = request(context.depth, symbol, contracts=1)
                bids = sorted(depth.bids, key=lambda item: item.position)
                asks = sorted(depth.asks, key=lambda item: item.position)
                result.update(bid=bids[0].price if bids else None,
                              ask=asks[0].price if asks else None,
                              fetched_at=datetime.now(timezone.utc), source="Longbridge OPRA")
            except Exception as exc:
                result["error"] = _longbridge_error(exc)
            quotes[symbol] = result
        return quotes[symbol]

    queues = []
    for row in positions:
        if throttled:
            diagnostics.append("Longbridge throttled the scan; remaining coverage deferred")
            return market, diagnostics
        current = next((item for item in contracts(row, row["expiry"])
                        if item["key"] == row["key"]), None)
        if current is None:
            diagnostics.append(f"{row['key']}: standard closing contract absent from Longbridge chain")
            continue
        if current["provider_symbol"] not in quotes and used + 2 > 500:
            diagnostics.append(f"{row['key']}: closing quote deferred by per-pass contract budget")
            continue
        market[row["key"]]["current"] = quote(current)
        if rolls.quote_error(market[row["key"]]["current"]):
            continue
        symbol = row["symbol"]
        if symbol not in expiries:
            try:
                expiries[symbol] = request(context.option_chain_expiry_date_list, symbol + ".US")
            except Exception as exc:
                diagnostics.append(f"{symbol}: {_longbridge_error(exc)}")
                expiries[symbol] = []
        candidates = []
        for expiry in rolls.candidate_expiries(expiries[symbol], row["expiry"], today):
            candidates.extend(contracts(row, expiry))
        candidates.sort(key=lambda item: (row["strike"] - item["strike"], item["expiry"], item["key"]))
        queues.append((row["key"], deque(candidates)))

    while any(queue for _, queue in queues):
        for key, queue in queues:
            if throttled:
                diagnostics.append("Longbridge throttled the scan; remaining coverage deferred")
                return market, diagnostics
            if not queue:
                continue
            descriptor = queue.popleft()
            if descriptor["provider_symbol"] not in quotes and used + 2 > 500:
                diagnostics.append("Longbridge per-pass quota reached; candidate coverage is partial, "
                                   "not an exhaustive chain search")
                return market, diagnostics
            market[key]["candidates"].append(quote(descriptor))
    diagnostics.append(f"Longbridge used {used}/500 contract request attempts; searched at most "
                       "four later expiries per holding within 70 DTE")
    return market, diagnostics
