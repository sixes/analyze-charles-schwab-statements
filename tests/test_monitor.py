import io
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from schwab import cli, monitor, notify, quotes, rolls, store
from tests.test_rolls import TODAY, position, quote


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.conn.read_only = False
        self.conn.__enter__.return_value = self.conn
        self.frame = pd.DataFrame([dict(position(), price=4)])
        self.row = position()
        self.market = {self.row["key"]: {"current": quote(), "candidates": [
            quote(days=28, bid="1.10", ask="1.11")]}}
        self.mocks = {}
        targets = {
            "store.connect": {"return_value": self.conn},
            "store.ensure_schema": {}, "store.load_records": {"return_value": [{}]},
            "store.load_trades": {"return_value": pd.DataFrame()},
            "store.save_quotes": {}, "store.clear_rebased_alerts": {},
            "store.claim_alerts": {"side_effect": lambda conn, alerts: alerts},
            "store.drop_alerts": {}, "store.cleanup_roll_alerts": {},
            "store.roll_alert_states": {"return_value": ["would send"]},
            "store.claim_roll_alerts": {"side_effect": lambda conn, alerts: [dict(a, claim_id=17) for a in alerts]},
            "store.drop_roll_alerts": {},
            "quotes.fetch_quotes": {"return_value": {self.row["key"]: {"price": 4}}},
            "quotes.fetch_roll_market": {"return_value": (self.market, [])},
            "positions.rollforward": {"side_effect": lambda *args, **kwargs: self.frame},
            "notify.announce_alerts": {"return_value": True},
            "notify.send": {"return_value": True},
            "rolls.market_date": {"return_value": TODAY},
        }
        for target, options in targets.items():
            patcher = patch("schwab." + target, **options)
            self.mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)

    def run_monitor(self, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = monitor.run(**kwargs)
        return result, out.getvalue(), err.getvalue()

    def test_golden_path_both_pipelines_and_complete_email(self):
        result, out, err = self.run_monitor()
        self.assertEqual(result, 0, err)
        self.mocks["notify.announce_alerts"].assert_called_once()
        subject, text, html = self.mocks["notify.send"].call_args.args
        self.assertIn("roll candidate", subject)
        self.assertIn("$15.40", text)
        self.assertIn("$15.40", html)
        self.assertIn("Longbridge OPRA", text)
        self.assertIn("28 DTE (+21 days)", out)
        self.mocks["store.cleanup_roll_alerts"].assert_called_once()
        self.assertFalse(self.conn.read_only)

    def test_confirmed_original_dates_reach_market_screen_and_email(self):
        opened = TODAY - timedelta(days=8)
        self.mocks["store.load_records"].return_value = [{
            "period_end": TODAY - timedelta(days=10), "_holdings": [], "_pending": [],
        }]
        self.mocks["store.load_trades"].return_value = pd.DataFrame([{
            "occ_symbol": self.row["key"], "trade_date": opened, "quantity": 2,
            "action": "Sale", "intent": "open",
        }])
        result, _, err = self.run_monitor()
        self.assertEqual(result, 0, err)
        requested = self.mocks["quotes.fetch_roll_market"].call_args.args[0][0]
        self.assertEqual(requested["original_trade_dates"], (opened,))
        _, plain, html = self.mocks["notify.send"].call_args.args
        self.assertIn(opened.strftime("%m-%d-%y"), plain)
        self.assertIn(opened.strftime("%m-%d-%y"), html)

    def test_no_band_does_not_skip_rolls(self):
        self.frame.loc[0, "price"] = 2
        result, _, _ = self.run_monitor()
        self.assertEqual(result, 0)
        self.mocks["store.claim_alerts"].assert_not_called()
        self.mocks["notify.send"].assert_called_once()

    def test_already_reported_bands_do_not_skip_rolls(self):
        self.mocks["store.claim_alerts"].side_effect = None
        self.mocks["store.claim_alerts"].return_value = []
        result, _, _ = self.run_monitor()
        self.assertEqual(result, 0)
        self.mocks["notify.announce_alerts"].assert_not_called()
        self.mocks["notify.send"].assert_called_once()

    def test_all_last_price_marks_failed_does_not_skip_depth_screen(self):
        self.frame.loc[0, "price"] = None
        self.mocks["quotes.fetch_quotes"].return_value = {self.row["key"]: {"price": None}}
        result, _, _ = self.run_monitor()
        self.assertEqual(result, 0)
        self.mocks["notify.send"].assert_called_once()

    def test_failed_or_raising_band_email_releases_bands_and_still_sends_roll(self):
        for failure in (False, RuntimeError("smtp failure")):
            with self.subTest(failure=failure):
                self.mocks["notify.announce_alerts"].side_effect = failure if isinstance(failure, Exception) else None
                self.mocks["notify.announce_alerts"].return_value = False
                self.mocks["notify.send"].reset_mock()
                self.mocks["store.drop_alerts"].reset_mock()
                result, _, _ = self.run_monitor()
                self.assertEqual(result, 1)
                self.mocks["store.drop_alerts"].assert_called_once()
                self.mocks["notify.send"].assert_called_once()

    def test_roll_false_or_exception_releases_exact_claim(self):
        for failure in (False, RuntimeError("smtp failure")):
            with self.subTest(failure=failure):
                self.mocks["notify.send"].side_effect = failure if isinstance(failure, Exception) else None
                self.mocks["notify.send"].return_value = False
                self.mocks["store.drop_roll_alerts"].reset_mock()
                result, _, _ = self.run_monitor()
                self.assertEqual(result, 1)
                released = self.mocks["store.drop_roll_alerts"].call_args.args[1]
                self.assertEqual([a["claim_id"] for a in released], [17])
                self.assertTrue(self.mocks["notify.announce_alerts"].called)

    def test_dry_run_read_only_before_loading_and_no_state_or_email(self):
        def read_records(conn):
            self.assertTrue(conn.read_only)
            return [{}]
        self.mocks["store.load_records"].side_effect = read_records
        result, out, _ = self.run_monitor(dry_run=True)
        self.assertEqual(result, 0)
        self.assertIn("would send", out)
        self.assertIn("local quota counters", out)
        for target in ("store.ensure_schema", "store.save_quotes", "store.clear_rebased_alerts",
                       "store.claim_alerts", "store.claim_roll_alerts", "store.cleanup_roll_alerts",
                       "store.drop_alerts", "store.drop_roll_alerts", "notify.send", "notify.announce_alerts"):
            self.mocks[target].assert_not_called()
        self.mocks["quotes.fetch_roll_market"].assert_called_once()

    def test_no_notify_claims_both_without_email(self):
        result, out, _ = self.run_monitor(notify=False)
        self.assertEqual(result, 0)
        self.assertIn("rolls recorded", out)
        self.mocks["store.claim_alerts"].assert_called_once()
        self.mocks["store.claim_roll_alerts"].assert_called_once()
        self.mocks["notify.send"].assert_not_called()
        self.mocks["notify.announce_alerts"].assert_not_called()

    def test_deduped_rolls_stay_silent(self):
        self.mocks["store.claim_roll_alerts"].side_effect = None
        self.mocks["store.claim_roll_alerts"].return_value = []
        result, out, _ = self.run_monitor()
        self.assertEqual(result, 0)
        self.assertIn("cooldown", out)
        self.mocks["notify.send"].assert_not_called()

    def test_no_rolls_keeps_price_monitor_only(self):
        result, _, _ = self.run_monitor(rolls_on=False)
        self.assertEqual(result, 0)
        self.mocks["quotes.fetch_roll_market"].assert_not_called()
        self.mocks["store.cleanup_roll_alerts"].assert_not_called()
        self.mocks["notify.announce_alerts"].assert_called_once()

    def test_roll_failure_cannot_mask_existing_band_delivery(self):
        self.mocks["quotes.fetch_roll_market"].side_effect = RuntimeError("provider unavailable")
        result, _, _ = self.run_monitor()
        self.assertEqual(result, 1)
        self.mocks["notify.announce_alerts"].assert_called_once()
        self.mocks["store.claim_roll_alerts"].assert_not_called()

    def test_invalid_arguments_stop_before_database(self):
        result, _, _ = self.run_monitor(roll_fee="NaN")
        self.assertEqual(result, 1)
        self.mocks["store.connect"].assert_not_called()

    def test_missing_statements_do_not_fetch_market(self):
        self.mocks["store.load_records"].return_value = []
        result, _, _ = self.run_monitor(dry_run=True)
        self.assertEqual(result, 1)
        self.mocks["quotes.fetch_quotes"].assert_not_called()
        self.mocks["quotes.fetch_roll_market"].assert_not_called()


class MonitorCliTests(unittest.TestCase):
    def test_options_are_forwarded(self):
        with patch.object(monitor, "run", return_value=0) as run:
            result = cli.main(["monitor", "--dry-run", "--no-notify", "--no-rolls",
                               "--roll-fee-per-leg", "1.25", "--put-min-credit", "10"])
        self.assertEqual(result, 0)
        run.assert_called_once_with(dry_run=True, notify=False, rolls_on=False,
                                    roll_fee=Decimal("1.25"), put_min_credit=Decimal("10"))

    def test_invalid_option_values_rejected_before_monitor(self):
        with patch.object(monitor, "run") as run, redirect_stderr(io.StringIO()):
            for flag, value in (("--roll-fee-per-leg", "nan"), ("--roll-fee-per-leg", "-1"),
                                ("--put-min-credit", "0"), ("--put-min-credit", "inf")):
                with self.subTest(flag=flag, value=value), self.assertRaises(SystemExit) as caught:
                    cli.main(["monitor", flag, value])
                self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()


class SyntheticEndToEndTests(unittest.TestCase):
    def test_statement_to_chain_to_depth_to_roll_email_and_repeat_silence(self):
        from tests.test_quotes import Clock, Context

        old = quote()
        candidate = quote(days=28, bid="1.10", ask="1.11")
        ctx = Context([old, candidate])
        record = {"period_end": TODAY - timedelta(days=9), "_holdings": [{
            "symbol": "XYZ", "asset_class": "Options", "option_type": "PUT",
            "strike": 100.0, "expiry": old["expiry"], "label": "XYZ 100 P",
            "quantity": -2.0, "cost_basis": -400.0, "market_value": -300.0,
        }], "_pending": []}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        claimed = set()

        def claim(conn, alerts):
            fresh = []
            for alert in alerts:
                identity = (alert["key"], alert["candidate_key"])
                if identity not in claimed:
                    fresh.append(dict(alert, claim_id=1))
                    claimed.add(identity)
            return fresh

        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            clock = Clock()
            limiter = quotes.LongbridgeQuota(Path(temp) / "quota.json", clock.time, clock.wait)
            stack.enter_context(patch.object(quotes, "_longbridge_context", return_value=ctx))
            stack.enter_context(patch.object(quotes, "LongbridgeQuota", return_value=limiter))
            stack.enter_context(patch.object(quotes, "fetch_quotes", return_value={old["key"]: {"price": 2}}))
            stack.enter_context(patch.object(rolls, "market_date", return_value=TODAY))
            stack.enter_context(patch.object(store, "connect", return_value=conn))
            stack.enter_context(patch.object(store, "load_records", return_value=[record]))
            stack.enter_context(patch.object(store, "load_trades", return_value=pd.DataFrame()))
            for name in ("ensure_schema", "save_quotes", "clear_rebased_alerts", "cleanup_roll_alerts"):
                stack.enter_context(patch.object(store, name))
            stack.enter_context(patch.object(store, "claim_roll_alerts", side_effect=claim))
            send = stack.enter_context(patch.object(notify, "send", return_value=True))
            stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(monitor.run(), 0)
            self.assertEqual(monitor.run(), 0)
            send.assert_called_once()
            subject, plain, html = send.call_args.args
            self.assertIn("roll candidate", subject)
            for fact in ("Original · XYZ $100.00 put", "New · XYZ $100.00 put",
                         old["expiry"].strftime("%m-%d-%y"), candidate["expiry"].strftime("%m-%d-%y"),
                         "$15.40", "21 days", "50%", "70 calendar DTE"):
                self.assertIn(fact, plain)
                self.assertIn(fact, html)
            self.assertEqual(sum(call[0] == "depth" for call in ctx.calls), 4)


if __name__ == "__main__":
    unittest.main()
