import copy
import unittest
from contextlib import ExitStack
from datetime import date
from unittest.mock import patch

from schwab import notify, store
from schwab.confirms import ingest
from tests.test_notify_rolls import RenderedText


def fill(**changes):
    row = dict(seq=1, symbol="ALFA", action="Purchase", intent="close", quantity=1,
               price=2.0, net_amount=-200.68, is_option=True, option_type="PUT",
               strike=50.125, expiry=date(2026, 8, 21), trade_date=date(2026, 8, 10))
    row.update(changes)
    return row


def outcome(trade):
    return dict(status="stored", trades=[trade], trades_stored=1, blocks_failed=0,
                net_amount=trade["net_amount"])


def confirm_body():
    return """Your trade confirmation(s) for 08/11/2026
Account ending: 1234
Symbol:
ALFA 08/21/2026 50.125 P
Security Description: Synthetic put
Action: Purchase
Security No./CUSIP: SYNTHETIC
Trade Date: 08/10/26
Settle Date: 08/11/26
Quantity Price Principal Charge and/or Interest Total Amount
1 $2.00 $200.00
Commission: $0.65
Industry Fee: $0.03
Total: $0.68 $200.68
Additional information for this security:
Bought to close your short option position.
"""


class EConfirmRenderingTests(unittest.TestCase):
    def test_original_current_fields_and_current_only_totals(self):
        original = fill(action="Sale", intent="open", quantity=3, price=5.0,
                        net_amount=1497.96, trade_date=date(2026, 8, 3))
        current = fill(original_context=dict(status="verified", original=original, reason=""))
        data = outcome(current)
        before = copy.deepcopy(data)
        plain, html = notify.body_for(data), notify.html_for(data)
        table = RenderedText(html).tables[0]
        self.assertEqual(table["headers"], list(notify.ECONFIRM_COLUMNS))
        self.assertEqual(len(table["rows"]), 2)
        old, new = table["rows"]
        self.assertEqual(old, ["Original · ALFA $50.125 put", "3", "Sold to open", "5.00",
                               "+$1,497.96", "08-03-26", "08-21-26"])
        self.assertEqual(new, ["Current · ALFA $50.125 put", "1", "Bought to close", "2.00",
                               "-$200.68", "08-10-26", "08-21-26"])
        for row in table["rows"]:
            for cell in row:
                self.assertIn(cell, plain)
        self.assertIn("Cash from these trades: -$200.68 (debit)", plain)
        self.assertEqual(notify.subject_for(data), "Schwab: 1 trade, -$200.68 debit")
        self.assertIn("Totals count Current rows only", plain)
        self.assertIn("full size and cash may differ", plain)
        self.assertIn(f"color:{notify.ROLL_OPEN_COLOR}", html)
        self.assertIn(f"color:{notify.ROLL_CLOSE_COLOR}", html)
        self.assertIn(f"color:{notify.CREDIT_COLOR}", html)
        self.assertIn(f"color:{notify.DEBIT_COLOR}", html)
        self.assertEqual(data, before)

    def test_no_original_and_ambiguous_history_keep_current(self):
        for context, marker, reason in (
            ({"status": "none", "original": None}, "-", "No prior position"),
            ({"status": "unavailable", "original": None, "reason": "Multiple opening fills"},
             "n/a", "Multiple opening fills"),
        ):
            with self.subTest(context=context):
                data = outcome(fill(original_context=context))
                table = RenderedText(notify.html_for(data)).tables[0]
                old, new = table["rows"]
                self.assertIn(reason, old[0])
                self.assertEqual(old[1:], [marker] * 6)
                self.assertEqual(new[5], "08-10-26")
                self.assertIn(reason, notify.body_for(data))

    def test_unknown_intent_shares_and_escaping(self):
        payload = '<img data-probe="unsafe" src=x> & test'
        current = fill(symbol=payload, intent=None, is_option=False, expiry=None, strike=None,
                       original_context=dict(status="unavailable", reason=payload))
        data = outcome(current)
        plain, html = notify.body_for(data), notify.html_for(data)
        rendered = RenderedText(html)
        self.assertNotIn("img", rendered.tags)
        self.assertIn("&lt;img", html)
        self.assertIn(payload, plain)
        self.assertIn(notify.UNKNOWN_INTENT_NOTE, plain)
        self.assertIn(notify.UNKNOWN_INTENT_NOTE, rendered.normalized())
        self.assertEqual(rendered.tables[0]["rows"][1][-1], "-")
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn('name="viewport"', html)

    def test_missing_price_cash_and_date_are_not_zero_or_fabricated(self):
        original = fill(price=float("nan"), net_amount=float("nan"), trade_date=None)
        current = fill(original_context=dict(status="verified", original=original))
        row = RenderedText(notify.html_for(outcome(current))).tables[0]["rows"][0]
        self.assertEqual(row[3:6], ["n/a", "n/a", "n/a"])

    def test_addition_colors_both_open_actions_blue(self):
        original = fill(intent="open", action="Sale")
        current = fill(intent="open", action="Sale", original_context=dict(status="verified", original=original))
        rows, colors = notify._econfirm_rows(current)
        self.assertEqual([row[2] for row in rows], ["Sold to open", "Sold to open"])
        self.assertEqual(colors[0, 2], notify.ROLL_OPEN_COLOR)
        self.assertEqual(colors[1, 2], notify.ROLL_OPEN_COLOR)


class IngestContextTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.body = confirm_body()
        self.message = dict(body=self.body, subject="Schwab eConfirms")
        self.fetch = self.stack.enter_context(patch.object(ingest.mailbox, "fetch", return_value=[self.message]))
        self.connect = self.stack.enter_context(patch.object(store, "connect"))
        self.schema = self.stack.enter_context(patch.object(store, "ensure_schema"))
        self.known = self.stack.enter_context(patch.object(store, "known_bodies", return_value={}))
        self.save = self.stack.enter_context(patch.object(store, "save_confirm", return_value=42))
        self.records = self.stack.enter_context(patch.object(store, "load_records", return_value=[]))
        self.history = self.stack.enter_context(patch.object(store, "load_trades", return_value=[]))
        self.index = self.stack.enter_context(patch.object(store, "trade_index", return_value=[]))
        self.resolve = self.stack.enter_context(patch("schwab.positions.original_trade_context", return_value=[
            dict(status="verified", original=fill(intent="open", action="Sale"), reason="")]))
        self.refresh = self.stack.enter_context(patch.object(ingest, "_refresh_quotes"))
        self.announce = self.stack.enter_context(patch.object(notify, "announce", return_value=True))

    def test_context_without_quotes_and_stable_identity_on_copy(self):
        self.assertEqual(ingest.run(quotes=False), 0)
        self.refresh.assert_not_called()
        current = self.announce.call_args.args[0]["trades"][0]
        self.assertEqual(current["body_sha256"], store.body_digest(self.body))
        self.assertEqual(current["seq"], 1)
        self.assertEqual(current["original_context"]["status"], "verified")
        stored = self.save.call_args.args[2][0]
        self.assertNotIn("body_sha256", stored)
        self.assertNotIn("original_context", stored)
        self.assertEqual(self.announce.call_args.args[0]["net_amount"], -200.68)

    def test_context_error_does_not_claim_successful_saves_failed(self):
        self.records.side_effect = RuntimeError("synthetic context read failure")
        self.assertEqual(ingest.run(quotes=False), 0)
        data = self.announce.call_args.args[0]
        self.assertEqual(data["status"], "stored")
        self.assertEqual(data["trades_stored"], 1)
        self.assertEqual(data["trades"][0]["original_context"]["status"], "unavailable")
        self.assertIn("current fills were stored", data["warnings"][0])
        self.save.assert_called_once()

    def test_quote_refresh_preserves_context_warning(self):
        self.records.side_effect = RuntimeError("synthetic context read failure")
        self.refresh.return_value = (2, 2, 2, ["Synthetic position warning"])
        self.assertEqual(ingest.run(quotes=True), 0)
        data = self.announce.call_args.args[0]
        self.assertIn("current fills were stored", data["warnings"][0])
        self.assertIn("Synthetic position warning", data["warnings"])
        self.assertEqual((data["positions"], data["quotes_ok"], data["quotes_total"]), (2, 2, 2))

    def test_known_email_stays_silent_without_enrichment(self):
        self.known.return_value = {store.body_digest(self.body): 42}
        self.assertEqual(ingest.run(quotes=False), 0)
        data = self.announce.call_args.args[0]
        self.assertIsNone(notify.subject_for(data))
        self.records.assert_not_called()
        self.resolve.assert_not_called()
        self.save.assert_not_called()

    def test_reprocess_passes_same_stable_identity(self):
        self.known.return_value = {store.body_digest(self.body): 42}
        self.assertEqual(ingest.run(quotes=False, reprocess=True), 0)
        self.assertEqual(self.resolve.call_args.args[2][0]["body_sha256"], store.body_digest(self.body))
        self.save.assert_called_once()

    def test_dry_run_neither_stores_enriches_nor_notifies(self):
        self.assertEqual(ingest.run(quotes=False, dry_run=True), 0)
        self.save.assert_not_called()
        self.resolve.assert_not_called()
        self.announce.assert_not_called()

    def test_no_notify_skips_context_but_still_stores(self):
        self.assertEqual(ingest.run(quotes=False, notify_on=False), 0)
        self.save.assert_called_once()
        self.resolve.assert_not_called()
        self.announce.assert_not_called()

    def test_failed_block_is_disclosed_and_blocks_confident_history(self):
        self.message["body"] += "\nSymbol:\nBROKEN\nAction: Sale\n"
        self.assertEqual(ingest.run(quotes=False), 2)
        self.assertIn({"status": "partial", "confirm_date": None}, self.resolve.call_args.args[3])
        data = self.announce.call_args.args[0]
        self.assertEqual(data["blocks_failed"], 1)
        self.assertEqual(data["trades_stored"], 1)


if __name__ == "__main__":
    unittest.main()
