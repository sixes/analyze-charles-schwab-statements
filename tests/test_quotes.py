import json
import os
import tempfile
import unittest
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from schwab import quotes, rolls
from tests.test_rolls import TODAY, position, quote


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def time(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def reserve_in_process(path):
    quotes.LongbridgeQuota(path).reserve(1)


class Context:
    def __init__(self, contracts):
        self.contracts = {item["provider_symbol"]: item for item in contracts}
        self.calls = []
        self.fail_depth = set()
        self.throttle = False
        self.metadata_overrides = {}

    def option_chain_info_by_date(self, symbol, expiry):
        self.calls.append(("chain", symbol, expiry))
        grouped = {}
        for item in self.contracts.values():
            if item["symbol"] + ".US" != symbol or item["expiry"] != expiry:
                continue
            info = grouped.setdefault(item["strike"], SimpleNamespace(
                price=item["strike"], standard=item["standard"], call_symbol="", put_symbol=""))
            setattr(info, "put_symbol" if item["option_type"] == "PUT" else "call_symbol",
                    item["provider_symbol"])
        return list(grouped.values())

    def option_chain_expiry_date_list(self, symbol):
        self.calls.append(("expiry", symbol))
        return sorted({item["expiry"] for item in self.contracts.values()
                       if item["symbol"] + ".US" == symbol})

    def option_quote(self, symbols):
        self.calls.append(("metadata", *symbols))
        result = []
        for symbol in symbols:
            item = self.contracts[symbol]
            metadata = dict(
                symbol=symbol, underlying_symbol=item["symbol"] + ".US", expiry_date=item["expiry"],
                strike_price=item["strike"], direction="OptionDirection." + item["option_type"].title(),
                contract_multiplier=item["multiplier"], contract_size=item["contract_size"],
                trade_status="TradeStatus.Normal", last_done=99999, open_interest=0, timestamp=None)
            metadata.update(self.metadata_overrides.get(symbol, {}))
            result.append(SimpleNamespace(**metadata))
        return result

    def depth(self, symbol):
        self.calls.append(("depth", symbol))
        if self.throttle:
            raise RuntimeError("301606 rate limited")
        if symbol in self.fail_depth:
            raise RuntimeError("synthetic credentials must not reach diagnostics")
        item = self.contracts[symbol]
        return SimpleNamespace(bids=[SimpleNamespace(position=1, price=item["bid"])],
                               asks=[SimpleNamespace(position=1, price=item["ask"])])


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "quota.json"
        self.clock = Clock()
        self.quota = quotes.LongbridgeQuota(self.path, self.clock.time, self.clock.wait)

    def test_weighted_rolling_window_persists_between_instances(self):
        self.quota.reserve(499)
        another = quotes.LongbridgeQuota(self.path, self.clock.time, self.clock.wait)
        another.reserve(1)
        self.assertFalse(self.clock.waits)
        another.reserve(1)
        self.assertGreaterEqual(self.clock.now, 1060)
        self.assertEqual(sum(cost for _, cost in json.loads(self.path.read_text())), 1)
        self.assertNotIn("symbol", self.path.read_text())

    def test_independent_request_rate_includes_chain_calls(self):
        for _ in range(8):
            self.quota.reserve(0)
        self.assertFalse(self.clock.waits)
        self.quota.reserve(0)
        self.assertGreaterEqual(self.clock.now, 1001)
        self.assertLess(self.clock.now, 1002)

    def test_sliding_not_fixed_minute_and_batch_cost(self):
        self.quota.reserve(300)
        self.clock.now += 30
        self.quota.reserve(200)
        self.quota.reserve(300)
        self.assertGreaterEqual(self.clock.now, 1060)
        self.assertLess(self.clock.now, 1090)
        self.assertEqual(sum(cost for _, cost in json.loads(self.path.read_text())), 500)

    def test_malformed_state_and_invalid_cost_fail_closed(self):
        for content in ('', '{}', 'broken', '[[1000, -1]]', '[[NaN, 1]]'):
            self.path.write_text(content)
            with self.assertRaises(ValueError):
                self.quota.reserve(1)
            self.assertEqual(self.path.read_text(), content)
        for count in (-1, 501, 1.5):
            with self.assertRaises(ValueError):
                self.quota.reserve(count)

    def test_cross_process_reservations_not_lost(self):
        with ProcessPoolExecutor(max_workers=3) as pool:
            list(pool.map(reserve_in_process, [str(self.path)] * 6))
        events = json.loads(self.path.read_text())
        self.assertEqual(len(events), 6)
        self.assertEqual(sum(cost for _, cost in events), 6)

    def test_interrupted_write_preserves_previous_quota(self):
        self.quota.reserve(499)
        previous = self.path.read_text()
        with patch.object(quotes.json, "dump", side_effect=OSError("interrupted write")):
            with self.assertRaises(OSError):
                self.quota.reserve(1)
        self.assertEqual(self.path.read_text(), previous)
        self.assertFalse(list(self.path.parent.glob(".longbridge-quota-*")))
        self.quota.reserve(500)
        self.assertGreaterEqual(self.clock.now, 1060)
        self.assertEqual(sum(cost for _, cost in json.loads(self.path.read_text())), 500)

    def test_failed_atomic_replace_preserves_previous_quota(self):
        self.quota.reserve(499)
        previous = self.path.read_text()
        with patch.object(quotes.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.quota.reserve(1)
        self.assertEqual(self.path.read_text(), previous)
        self.assertFalse(list(self.path.parent.glob(".longbridge-quota-*")))


class LongbridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        clock = Clock()
        self.path = Path(self.temp.name) / "quota.json"
        self.limiter = quotes.LongbridgeQuota(self.path, clock.time, clock.wait)

    def fetch(self, rows, contracts, **kwargs):
        context = Context(contracts)
        for key, value in kwargs.items():
            setattr(context, key, value)
        market, diagnostics = quotes.fetch_roll_market(rows, TODAY, context, self.limiter)
        return context, market, diagnostics

    def test_sdk_interfaces_and_version_match_adapter(self):
        import inspect
        from importlib.metadata import version
        from longbridge.openapi import Config, QuoteContext, StrikePriceInfo, OptionQuote, SecurityDepth
        self.assertEqual(version("longbridge"), "0.2.74")
        self.assertIn("app_key", inspect.signature(Config).parameters)
        self.assertIn("expiry_date", inspect.signature(QuoteContext.option_chain_info_by_date).parameters)
        for name in ("price", "standard", "call_symbol", "put_symbol"):
            self.assertTrue(hasattr(StrikePriceInfo, name))
        self.assertTrue(hasattr(OptionQuote, "contract_multiplier"))
        self.assertTrue(hasattr(SecurityDepth, "bids"))
        self.assertTrue(hasattr(SecurityDepth, "asks"))

    def test_depth_not_last_trade_and_no_oi_time_filter(self):
        row = position()
        old, new = quote(), quote(days=28, bid="1.10", ask="1.11")
        context, market, _ = self.fetch([row], [old, new])
        current = market[row["key"]]["current"]
        self.assertEqual(current["bid"], old["bid"])
        self.assertNotEqual(current["bid"], 99999)
        self.assertIsNotNone(current["fetched_at"].tzinfo)
        alerts, _ = rolls.evaluate([row], market, today=TODAY)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(sum(cost for _, cost in json.loads(self.path.read_text())), 4)

    def test_unknown_direction_uses_verified_chain_and_symbol_for_both_sides(self):
        for kind in ("PUT", "CALL"):
            with self.subTest(kind=kind):
                row = position(kind, strike="100.125")
                old = quote(kind, strike="100.125", bid="2", ask="2.01")
                new = quote(kind, strike="100.125", days=28,
                            bid="2.10" if kind == "PUT" else "1.90",
                            ask="2.11" if kind == "PUT" else "1.91")
                overrides = {item["provider_symbol"]: {"direction": "OptionDirection.Unknown"}
                             for item in (old, new)}
                context, market, _ = self.fetch([row], [old, new], metadata_overrides=overrides)
                self.assertIsNone(rolls.quote_error(market[row["key"]]["current"]))
                self.assertEqual(sum(call[0] == "depth" for call in context.calls), 2)
                alerts, _ = rolls.evaluate([row], market, today=TODAY)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0]["option_type"], kind)

    def test_contradictory_or_missing_direction_still_rejected(self):
        for kind, direction in (("PUT", "OptionDirection.Call"), ("CALL", "OptionDirection.Put"),
                                ("PUT", None), ("CALL", "")):
            with self.subTest(kind=kind, direction=direction):
                old = quote(kind)
                context, market, _ = self.fetch(
                    [position(kind)], [old],
                    metadata_overrides={old["provider_symbol"]: {"direction": direction}})
                error = market[old["key"]]["current"]["error"]
                self.assertIn("metadata rejected: option direction", error)
                self.assertNotIn("credentials", error)
                self.assertFalse(any(call[0] == "depth" for call in context.calls))

    def test_unknown_direction_cannot_hide_inconsistent_provider_symbol(self):
        for symbol in ("ABC261016P100000.US", "XYZ261017P100000.US", "XYZ261016C100000.US",
                       "XYZ261016P100001.US", "XYZ261016P100000.HK", "malformed"):
            with self.subTest(symbol=symbol):
                old = dict(quote(), provider_symbol=symbol)
                context, market, _ = self.fetch(
                    [position()], [old],
                    metadata_overrides={symbol: {"direction": "OptionDirection.Unknown"}})
                error = market[old["key"]]["current"]["error"]
                self.assertIn("metadata rejected: contract symbol", error)
                self.assertNotIn("credentials", error)
                self.assertFalse(any(call[0] == "depth" for call in context.calls))

    def test_unknown_direction_retains_all_other_metadata_checks(self):
        cases = (("symbol", "wrong-symbol", "no contract metadata"),
                 ("underlying_symbol", "ABC.US", "underlying"),
                 ("expiry_date", TODAY, "expiry"), ("strike_price", "100.001", "strike"),
                 ("contract_multiplier", 150, "multiplier"),
                 ("contract_size", 150, "contract size"),
                 ("trade_status", "TradeStatus.Suspend", "trading status"))
        for field, value, reason in cases:
            with self.subTest(field=field):
                old = quote()
                overrides = {old["provider_symbol"]: {"direction": "OptionDirection.Unknown",
                                                       field: value}}
                context, market, _ = self.fetch([position()], [old], metadata_overrides=overrides)
                error = market[old["key"]]["current"]["error"]
                self.assertIn("metadata rejected: " + reason, error)
                self.assertNotIn("credentials", error)
                self.assertNotIn(str(value), error)
                self.assertFalse(any(call[0] == "depth" for call in context.calls))

    def test_provider_value_error_is_sanitized_not_labeled_local_metadata(self):
        with patch.object(Context, "option_quote", side_effect=ValueError("sensitive-provider-value")):
            context, market, _ = self.fetch([position()], [quote()])
        error = market[position()["key"]]["current"]["error"]
        self.assertIn("request unavailable", error)
        self.assertNotIn("metadata rejected", error)
        self.assertNotIn("sensitive-provider-value", error)
        self.assertFalse(any(call[0] == "depth" for call in context.calls))

    def test_shared_expiry_chain_and_contract_cache(self):
        rows = [position(), position("CALL")]
        contracts = [quote(), quote("CALL"), quote(days=28), quote("CALL", days=28)]
        context, _, _ = self.fetch(rows, contracts)
        counts = Counter(context.calls)
        self.assertEqual(counts[("expiry", "XYZ.US")], 1)
        self.assertEqual(counts[("chain", "XYZ.US", TODAY + timedelta(days=7))], 1)
        self.assertEqual(counts[("chain", "XYZ.US", TODAY + timedelta(days=28))], 1)
        self.assertTrue(all(count == 1 for call, count in counts.items() if call[0] == "metadata"))

    def test_partial_failure_preserves_other_candidates_and_counts_failure(self):
        row = position()
        good, bad = quote(days=28), quote(days=35)
        _, market, diagnostics = self.fetch([row], [quote(), good, bad],
                                            fail_depth={bad["provider_symbol"]})
        candidates = market[row["key"]]["candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertNotIn("error", candidates[0])
        self.assertIn("error", candidates[1])
        self.assertNotIn("synthetic credentials", str(market) + str(diagnostics))
        self.assertEqual(sum(cost for _, cost in json.loads(self.path.read_text())), 6)

    def test_adjusted_and_unverified_multiplier_are_rejected(self):
        for field, value in (("standard", False), ("multiplier", 150)):
            old = dict(quote(), **{field: value})
            _, market, _ = self.fetch([position()], [old, quote(days=28)])
            self.assertIsNotNone(rolls.quote_error(market[position()["key"]]["current"]))

    def test_missing_credentials_never_opens_context_or_quota_file(self):
        with patch.dict(os.environ, {}, clear=True):
            market, diagnostics = quotes.fetch_roll_market([position()], TODAY, limiter=self.limiter)
        self.assertIn("credentials missing", diagnostics[0])
        self.assertFalse(self.path.exists())
        self.assertIsNone(market[position()["key"]]["current"])

    def test_expired_token_is_actionable_without_leaking_provider_message(self):
        with patch.object(quotes, "_longbridge_context", side_effect=RuntimeError(
                "(401003) token expired sensitive-token-value")):
            market, diagnostics = quotes.fetch_roll_market([position()], TODAY, limiter=self.limiter)
        self.assertIn("access token expired (401003)", diagnostics[0])
        self.assertIn("LONGBRIDGE_ACCESS_TOKEN", diagnostics[0])
        self.assertNotIn("sensitive-token-value", str(diagnostics))
        self.assertFalse(self.path.exists())
        self.assertIsNone(market[position()["key"]]["current"])

    def test_expiry_cap_and_first_four_expiries(self):
        contracts = [quote()] + [quote(days=days) for days in (14, 21, 28, 35, 42, 70, 71)]
        context, _, _ = self.fetch([position()], contracts)
        requested = [call[2] for call in context.calls if call[0] == "chain"]
        self.assertEqual(requested, [TODAY + timedelta(days=n) for n in (7, 14, 21, 28, 35)])

    def test_quota_bounded_scan_caches_and_round_robins_holdings(self):
        rows = [position(strike="500"), position(strike="500", symbol="ABC")]
        contracts = [quote(strike="500"), quote(strike="500", symbol="ABC")]
        contracts += [quote(strike=str(strike), days=28, symbol=symbol)
                      for symbol in ("XYZ", "ABC") for strike in range(1, 501)]
        context, market, diagnostics = self.fetch(rows, contracts)
        calls = [call for call in context.calls if call[0] in ("metadata", "depth")]
        self.assertEqual(len(calls), 500)
        self.assertTrue(any("partial" in line for line in diagnostics))
        counts = [len(market[row["key"]]["candidates"]) for row in rows]
        self.assertEqual(counts, [124, 124])
        self.assertEqual([call[1] for call in calls[:4:2]],
                         [contracts[0]["provider_symbol"], contracts[1]["provider_symbol"]])

    def test_throttle_defers_remaining_market_calls(self):
        context, market, _ = self.fetch([position(), position(symbol="ABC")],
                                        [quote(), quote(days=28), quote(symbol="ABC")], throttle=True)
        self.assertEqual(sum(call[0] == "depth" for call in context.calls), 1)
        self.assertEqual(sum(cost for _, cost in json.loads(self.path.read_text())), 2)
        self.assertFalse(market[position()["key"]]["candidates"])


if __name__ == "__main__":
    unittest.main()
