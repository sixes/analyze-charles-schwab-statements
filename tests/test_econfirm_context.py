"""Pure, synthetic coverage of eConfirm historical context; no rendering or I/O."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import unittest

import pandas as pd

from schwab.domain import TRADE_COLUMNS, position_key
from schwab.positions import original_trade_context


FLAT = date(2026, 7, 31)
FIRST = date(2026, 8, 3)
SECOND = date(2026, 8, 4)
THIRD = date(2026, 8, 5)
EXPIRY = date(2026, 10, 16)


def trade(name, day=FIRST, **changes):
    row = {
        "body_sha256": f"synthetic-{name}", "seq": 1,
        "trade_date": day, "settle_date": date(2026, 8, 6),
        "symbol": "XYZ", "description": "Synthetic XYZ put",
        "is_option": True, "option_type": "PUT", "strike": Decimal("70.125"),
        "expiry": EXPIRY, "action": "Sale", "intent": "open",
        "quantity": 3.0, "price": 4.25, "principal": 1275.0,
        "commission": 0.65, "industry_fee": 0.0, "net_amount": 1274.35,
        "account_tail": "1111",
    }
    row.update(changes)
    if not row["is_option"]:
        row.update(option_type=None, strike=None, expiry=None)
    row["occ_symbol"] = position_key(
        row["symbol"], row["is_option"], row["expiry"], row["option_type"], row["strike"],
    ) if row["is_option"] else None
    return row


def statement(end=FLAT, quantity=0, pending=(), instrument=None):
    instrument = trade("holding") if instrument is None else instrument
    holding = {field: instrument[field] for field in
               ("symbol", "option_type", "strike", "expiry")}
    holding.update(
        label="XYZ 70.125 P 10/16" if instrument["is_option"] else "XYZ",
        asset_class="Options" if instrument["is_option"] else "Equities",
        quantity=quantity,
    )
    return {"period_end": end, "_holdings": [holding] if quantity != 0 else [],
            "_pending": list(pending), "_transactions": []}


def gap(day, **changes):
    entry = {"body_sha256": "synthetic-gap", "confirm_date": day,
             "status": "partial", "error": "Unparsed trade block", "trade_count": 1}
    entry.update(changes)
    return entry


class OriginalTradeContextTests(unittest.TestCase):
    def resolve(self, current, history=(), records=None, email_index=None):
        results = original_trade_context(
            [statement()] if records is None else records, history, [current], email_index,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(set(results[0]), {"status", "original", "reason"})
        self.assertIsInstance(results[0]["reason"], str)
        self.assertTrue(results[0]["reason"])
        return results[0]

    def assert_unavailable(self, current, history=(), records=None, email_index=None, reason=None):
        result = self.resolve(current, history, records, email_index)
        self.assertEqual(result["status"], "unavailable", result)
        self.assertIsNone(result["original"])
        if reason:
            self.assertIn(reason.lower(), result["reason"].lower())

    def test_short_put_and_long_call_return_actual_full_opening_fill(self):
        for kind, opening, closing in (("PUT", "Sale", "Purchase"),
                                       ("CALL", "Purchase", "Sale")):
            for dataframe in (False, True):
                with self.subTest(kind=kind, dataframe=dataframe):
                    original = trade("original", option_type=kind, action=opening,
                                     net_amount=1274.35 if opening == "Sale" else -1275.65)
                    current = trade("current", SECOND, option_type=kind, action=closing,
                                    intent="close", quantity=1, price=0.5,
                                    net_amount=-50.65 if closing == "Purchase" else 49.35)
                    history = pd.DataFrame([original, current]) if dataframe else [original, current]
                    result = self.resolve(current, history)
                    self.assertEqual(result["status"], "verified")
                    self.assertEqual(result["original"], original)
                    if not dataframe:
                        self.assertIs(result["original"], original)
                    self.assertEqual(result["original"]["quantity"], 3)
                    self.assertEqual(result["original"]["price"], 4.25)
                    self.assertIn("available confirmations", result["reason"])

    def test_verified_opening_from_flat_with_empty_history_shapes(self):
        for history in (None, [], pd.DataFrame(), pd.DataFrame(columns=[*TRADE_COLUMNS, "confirm_date"])):
            with self.subTest(history_type=type(history).__name__):
                result = self.resolve(trade("new"), history)
                self.assertEqual(result, {"status": "none", "original": None, "reason": "No prior position"})

    def test_addition_can_reference_only_prior_opening(self):
        original = trade("original")
        addition = trade("addition", SECOND, quantity=2, price=5, net_amount=999.35)
        result = self.resolve(addition, [original, addition])
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["original"], original)

    def test_partial_closes_do_not_allocate_or_shrink_original(self):
        original = trade("original")
        partial = trade("partial", SECOND, action="Purchase", intent="close", quantity=1)
        current = trade("current", THIRD, action="Purchase", intent="close", quantity=2)
        result = self.resolve(current, [partial, original])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["original"], original)
        self.assertEqual(result["original"]["net_amount"], 1274.35)
        self.assertEqual(result["original"]["quantity"], 3)

    def test_full_close_then_reopen_resets_lifecycle(self):
        original = trade("old")
        close = trade("close", SECOND, action="Purchase", intent="close")
        reopened = trade("reopened", THIRD, price=7, net_amount=2099.35)
        self.assertEqual(self.resolve(reopened, [close, original])["status"], "none")
        current = trade("current", THIRD + timedelta(days=1), action="Purchase", intent="close")
        result = self.resolve(current, [reopened, original, close])
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["original"], reopened)

    def test_multiple_opens_are_not_fifo_or_average_even_after_partial_close(self):
        original = trade("original")
        addition = trade("addition", SECOND)
        partial = trade("partial", THIRD, action="Purchase", intent="close", quantity=3)
        current = trade("current", THIRD + timedelta(days=1), action="Purchase", intent="close")
        self.assert_unavailable(current, [original, addition, partial], reason="Multiple opening")

    def test_full_close_resets_multiple_opening_ambiguity(self):
        original = trade("original")
        addition = trade("addition", SECOND)
        closed = trade("closed", THIRD, action="Purchase", intent="close", quantity=6)
        reopened = trade("reopened", THIRD + timedelta(days=1))
        current = trade("current", THIRD + timedelta(days=2), action="Purchase", intent="close")
        result = self.resolve(current, [original, addition, closed, reopened])
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["original"], reopened)

    def test_shares_use_ticker_identity_with_explicit_intent(self):
        original = trade("shares", is_option=False, action="Purchase", quantity=100)
        current = trade("sale", SECOND, is_option=False, action="Sale", intent="close", quantity=50)
        result = self.resolve(current, [original])
        self.assertEqual(result["status"], "verified")
        self.assertIsNone(result["original"]["expiry"])
        self.assertIs(result["original"], original)
        self.assert_unavailable(dict(current, intent=None), [original], reason="intent")

    def test_batch_uses_fill_dates_not_mailbox_sequence_and_keeps_alignment(self):
        opened = trade("batch-open", seq=90, email_id=900)
        partial = trade("batch-close", SECOND, action="Purchase", intent="close", quantity=1,
                        seq=2, email_id=2)
        current = trade("current", THIRD, action="Purchase", intent="close", quantity=2,
                        seq=1, email_id=1)
        batch = [current, partial, opened]
        for history in ([], pd.DataFrame(batch)):
            with self.subTest(history_type=type(history).__name__):
                result = original_trade_context([statement()], history, batch)
                self.assertEqual([row["status"] for row in result], ["verified", "verified", "none"])
                self.assertEqual([row["original"] for row in result], [opened, opened, None])

    def test_reprocess_excludes_target_by_digest_and_seq_not_economics(self):
        current = trade("current")
        self.assertEqual(self.resolve(current, [dict(current, email_id=17)])["status"], "none")
        for other in (dict(current, body_sha256="synthetic-other"), dict(current, seq=2)):
            with self.subTest(identity=(other["body_sha256"], other["seq"])):
                self.assert_unavailable(current, [current, other], reason="same-instrument")

    def test_identical_historic_fills_remain_distinct(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close", quantity=6)
        for twin in (dict(original, body_sha256="synthetic-twin"), dict(original, seq=2)):
            with self.subTest(identity=(twin["body_sha256"], twin["seq"])):
                self.assert_unavailable(current, [original, twin], reason="same-day")

    def test_same_day_fills_never_get_execution_order_from_ids_or_timestamps(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close", quantity=1, email_id=1)
        addition = trade("addition", datetime(2026, 8, 4, 9, 31), seq=90, email_id=90)
        self.assert_unavailable(current, [original, addition], reason="same-instrument")
        close = trade("close", FIRST, action="Purchase", intent="close", email_id=2)
        self.assert_unavailable(trade("reopen", SECOND), [original, close], reason="same-day")

    def test_later_history_and_snapshots_cannot_change_as_of_context(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        future = trade("future", THIRD, intent=None, account_tail="2222")
        future_statement = statement(THIRD, quantity=-999)
        future_statement["account_tail"] = "2222"
        same_day_statement = statement(SECOND, quantity=0)
        same_day_statement["account_tail"] = "3333"
        result = self.resolve(current, [future, current, original],
                              [future_statement, same_day_statement, statement()],
                              [gap(THIRD + timedelta(days=1))])
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["original"], original)

    def test_current_day_statement_is_not_a_pre_fill_anchor(self):
        self.assert_unavailable(trade("current"), records=[statement(FIRST)], reason="flat statement")

    def test_missing_anchor_or_unexplained_starting_position(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        for records in ([], [statement(quantity=-3)], [{"period_end": FLAT}]):
            with self.subTest(records=records):
                self.assert_unavailable(current, [original], records, reason="flat statement")
        self.assert_unavailable(current, records=[statement()], reason="direction or size")

    def test_pending_only_exposure_prevents_false_flat_anchor(self):
        pending = trade("pending", FLAT)
        self.assert_unavailable(trade("current"), records=[statement(pending=[pending])],
                                reason="flat statement")

    def test_pending_cover_can_prove_flat_and_supersede_old_unknown_history(self):
        end = date(2026, 8, 31)
        cover = trade("pending-cover", end, action="Purchase", intent="close", quantity=9)
        new = trade("new", date(2026, 9, 1))
        records = [statement(quantity=-9), statement(end, quantity=-9, pending=[cover])]
        old_unknown = trade("unknown", intent=None, quantity=9)
        result = self.resolve(new, [old_unknown], records, [gap(FIRST)])
        self.assertEqual(result["status"], "none")

    def test_subsequent_snapshots_reconcile_holdings_plus_pending(self):
        original = trade("original")
        end = date(2026, 8, 31)
        cover = trade("pending-cover", end, action="Purchase", intent="close", quantity=1)
        printed = dict(cover, category="Purchase", action="CoverShort")
        current = trade("current", date(2026, 9, 1), action="Purchase", intent="close", quantity=2)
        result = self.resolve(current, [cover, original],
                              [statement(end, quantity=-3, pending=[printed]), statement()])
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["original"], original)
        self.assert_unavailable(current, [original],
                                [statement(), statement(end, quantity=-3, pending=[printed])],
                                reason="disagrees")

    def test_intervening_disagreement_is_not_repaired_by_later_matching_snapshot(self):
        original = trade("original")
        current = trade("current", date(2026, 10, 1), action="Purchase", intent="close")
        records = [statement(), statement(date(2026, 8, 31), quantity=-2),
                   statement(date(2026, 9, 30), quantity=-3)]
        self.assert_unavailable(current, [original], records, reason="disagrees")

    def test_latest_usable_flat_snapshot_resets_provenance(self):
        end = date(2026, 8, 31)
        original = trade("original", date(2026, 9, 1))
        current = trade("current", date(2026, 9, 2), action="Purchase", intent="close")
        old = trade("old", quantity=50, intent=None)
        result = self.resolve(current, [old, original],
                              [statement(end), statement(quantity=-10)])
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["original"], original)

    def test_conflicting_duplicate_period_snapshots_do_not_prove_flatness(self):
        self.assert_unavailable(trade("current"), records=[statement(), statement(quantity=-3)],
                                reason="flat statement")

    def test_unresolved_holdings_or_pending_are_not_treated_as_flat(self):
        current = trade("current")
        base = statement(quantity=-3)
        bad_holding = dict(base["_holdings"][0], expiry=None, label="unresolved option")
        bad_pending = dict(trade("pending", FLAT), strike=None)
        missing_symbol = dict(bad_holding, symbol=None)
        missing_quantity = dict(base["_holdings"][0], quantity=None)
        cases = [
            dict(statement(), _holdings=[bad_holding]),
            dict(statement(), _holdings=[missing_symbol]),
            dict(statement(), _holdings=[missing_quantity]),
            statement(pending=[bad_pending]),
            statement(pending=[dict(trade("pending", FLAT), quantity=None)]),
            statement(pending=[dict(trade("pending", FLAT), action="Other")]),
        ]
        for records in cases:
            with self.subTest(records=records):
                self.assert_unavailable(current, records=[records], reason="flat statement")

    def test_unresolved_intervening_statement_is_not_silently_skipped(self):
        original = trade("original")
        record = statement(date(2026, 8, 31), quantity=-3)
        record["_holdings"][0].update(strike=None, label="unresolved option")
        current = trade("current", date(2026, 9, 1), action="Purchase", intent="close")
        self.assert_unavailable(current, [original], [statement(), record], reason="Unresolved statement")

    def test_inferred_statement_expiry_matches_existing_anchor_convention(self):
        original = trade("original")
        record = statement(date(2026, 8, 31), quantity=-3)
        record["_holdings"][0]["expiry"] = None
        current = trade("current", date(2026, 9, 1), action="Purchase", intent="close")
        result = self.resolve(current, [original], [statement(), record])
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["original"], original)

    def test_contract_identity_includes_expiry_side_strike_and_share_option_kind(self):
        current = trade("current", SECOND, action="Purchase", intent="close")
        for changes in ({"strike": Decimal("70.126")}, {"option_type": "CALL"},
                        {"expiry": date(2026, 11, 20)}, {"is_option": False}):
            with self.subTest(changes=changes):
                different = trade("different", **changes)
                self.assert_unavailable(current, [different], reason="direction or size")
                # A separate instrument is neither a prior lot nor an executed roll.
                result = self.resolve(dict(current, action="Sale", intent="open"), [different])
                self.assertEqual(result["status"], "none")

    def test_other_contracts_do_not_poison_history_or_flat_statement(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        unrelated = trade("unrelated", None, strike=Decimal("75"), intent=None)
        other = trade("other", symbol="OTHER")
        records = [statement(quantity=-9, instrument=other)]
        result = self.resolve(current, [original, unrelated], records)
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["original"], original)

    def test_undated_or_unresolved_relevant_fill_blocks_context_even_after_flat(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        for unknown in (dict(original, body_sha256="synthetic-unknown", trade_date=None),
                        dict(original, body_sha256="synthetic-unknown", trade_date=pd.NaT),
                        dict(original, body_sha256="synthetic-unknown", strike=None),
                        dict(original, body_sha256="synthetic-unknown", symbol=None)):
            with self.subTest(unknown=unknown):
                self.assert_unavailable(current, [original, unknown])
        unknown = trade("undated", None, confirm_date=date(2027, 1, 1))
        self.assert_unavailable(current, [original, unknown], reason="Undated")

    def test_stored_occ_symbol_cannot_override_disagreeing_contract_fields(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        self.assert_unavailable(current, [dict(original, strike=Decimal("80"))], reason="Unresolved")
        self.assert_unavailable(dict(current, occ_symbol="XYZ261016P00080000"), [original],
                                reason="Instrument identity")

    def test_missing_dates_and_fill_identities_are_unavailable(self):
        current = trade("current")
        for field, value in (("trade_date", None), ("trade_date", "2026-08-03"),
                             ("body_sha256", None), ("seq", None), ("seq", float("nan"))):
            with self.subTest(field=field, value=value):
                self.assert_unavailable(dict(current, **{field: value}))
        original = trade("original")
        close = trade("close", SECOND, action="Purchase", intent="close")
        self.assert_unavailable(close, [dict(original, body_sha256=None)], reason="identity")
        self.assert_unavailable(close, [original], [statement(), statement(None)], reason="Statement date")

    def test_missing_tails_are_allowed_but_known_conflicting_account_scope_is_not(self):
        original = trade("original", account_tail=None)
        current = trade("current", SECOND, action="Purchase", intent="close")
        self.assertEqual(self.resolve(current, [original])["status"], "verified")
        self.assertEqual(self.resolve(dict(current, account_tail=None), [original])["status"], "verified")
        self.assert_unavailable(current, [dict(original, account_tail="2222")], reason="account scope")
        other_account = trade("other-account", symbol="OTHER", account_tail="2222")
        self.assert_unavailable(current, [original, other_account], reason="account scope")
        scoped_statement = dict(statement(), account_tail="2222")
        self.assert_unavailable(current, [original], [scoped_statement], reason="account scope")

    def test_pandas_nulls_and_calendar_dates_are_handled_without_mutation(self):
        original = trade("original", pd.Timestamp(FIRST), account_tail=pd.NA)
        current = trade("current", pd.Timestamp(SECOND), action="Purchase", intent="close")
        frame = pd.DataFrame([original, current])
        before = frame.copy(deep=True)
        result = self.resolve(current, frame)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["original"]["trade_date"], pd.Timestamp(FIRST))
        pd.testing.assert_frame_equal(frame, before)

    def test_known_incomplete_emails_block_relevant_window_including_next_day(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        for entry in (gap(FIRST), gap(SECOND + timedelta(days=1)), gap(None),
                      gap(SECOND, status="failed"), gap(SECOND, status="ok"),
                      gap(SECOND, error=None), gap(None, body_sha256=current["body_sha256"])):
            with self.subTest(entry=entry):
                self.assert_unavailable(current, [original], email_index=[entry], reason="incomplete")
        self.assert_unavailable(trade("new"), email_index=[gap(FIRST)], reason="incomplete")

    def test_target_header_and_historical_email_identities_keep_parse_gaps_relevant(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        late_header = date(2026, 8, 10)
        entries = [gap(late_header, body_sha256=current["body_sha256"])]
        self.assert_unavailable(current, [original], email_index=entries, reason="incomplete")
        entries = [gap(late_header, body_sha256=original["body_sha256"])]
        self.assert_unavailable(current, [original], email_index=entries, reason="incomplete")

    def test_old_or_later_parse_gaps_and_successful_email_index_do_not_block(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        entries = [gap(FLAT), gap(SECOND + timedelta(days=2)),
                   gap(SECOND, status="ok", error=None)]
        self.assertEqual(self.resolve(current, [original], email_index=entries)["status"], "verified")

    def test_bad_actions_intents_and_sizes_fail_for_both_current_and_history(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        invalid = [("intent", None), ("intent", "unknown"), ("action", None),
                   ("action", "SaleInvalid"), ("quantity", None), ("quantity", 0),
                   ("quantity", -1), ("quantity", float("nan")),
                   ("quantity", float("inf")), ("quantity", "invalid")]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                self.assert_unavailable(dict(current, **{field: value}), [original])
                self.assert_unavailable(current, [dict(original, **{field: value})])

    def test_current_cannot_open_against_sign_close_from_flat_or_overshoot(self):
        original = trade("original")
        for current in (trade("wrong-open", SECOND, action="Purchase"),
                        trade("wrong-close", SECOND, action="Sale", intent="close"),
                        trade("overshoot", SECOND, action="Purchase", intent="close", quantity=4)):
            with self.subTest(current=current):
                self.assert_unavailable(current, [original], reason="direction or size")
        self.assert_unavailable(trade("flat-close", action="Purchase", intent="close"),
                                reason="direction or size")

    def test_historical_direction_and_size_inconsistencies_are_not_netted_away(self):
        original = trade("original")
        current = trade("current", THIRD)
        cases = [
            [trade("flat-close", action="Purchase", intent="close")],
            [original, trade("overshoot", SECOND, action="Purchase", intent="close", quantity=4)],
            [original, trade("opposite-open", SECOND, action="Purchase")],
            [original, trade("wrong-close", SECOND, intent="close")],
        ]
        for history in cases:
            with self.subTest(history=history):
                self.assert_unavailable(current, history, reason="direction or size")

    def test_directional_action_aliases_require_consistent_intent(self):
        original = trade("original", action="ShortSale")
        current = trade("current", SECOND, action="CoverShort", intent="close")
        self.assertEqual(self.resolve(current, [original])["status"], "verified")
        self.assert_unavailable(dict(current, intent="open"), [original], reason="intent")
        self.assert_unavailable(current, [dict(original, intent="close")], reason="intent")

    def test_conflicting_copies_of_one_identity_are_not_silently_chosen(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        self.assert_unavailable(current, [original, dict(original, quantity=4)], reason="Conflicting copies")
        self.assertEqual(self.resolve(current, [original, dict(original)])["status"], "verified")

    def test_inputs_are_unchanged_and_empty_current_returns_empty_results(self):
        original = trade("original")
        current = trade("current", SECOND, action="Purchase", intent="close")
        records, history, batch, index = [statement()], [original], [current], [gap(FLAT)]
        before = deepcopy((records, history, batch, index))
        result = original_trade_context(records, history, batch, index)
        self.assertEqual(result[0]["status"], "verified")
        self.assertEqual((records, history, batch, index), before)
        self.assertEqual(original_trade_context(records, history, [], index), [])
