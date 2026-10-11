import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from schwab import rolls
from schwab.positions import opening_trade_dates

TODAY = date(2026, 10, 9)


def quote(kind="PUT", strike="100", days=7, bid="1", ask="1.01", symbol="XYZ"):
    expiry = TODAY + timedelta(days=days)
    return {
        "key": rolls.contract_key(symbol, expiry, kind, strike), "symbol": symbol,
        "provider_symbol": f"{symbol}{expiry:%y%m%d}{kind[0]}{int(Decimal(strike) * 1000)}.US",
        "option_type": kind, "strike": Decimal(strike), "expiry": expiry,
        "bid": Decimal(bid), "ask": Decimal(ask), "multiplier": Decimal(100),
        "contract_size": Decimal(100), "standard": True, "source": "Longbridge OPRA",
        "fetched_at": datetime(2026, 10, 9, 15, tzinfo=timezone.utc),
    }


def position(kind="PUT", strike="100", days=7, symbol="XYZ"):
    contract = quote(kind, strike, days, symbol=symbol)
    return dict(contract, is_option=True, status="open", quantity=Decimal(-2 if kind == "PUT" else 2),
                cost_basis=Decimal(-400 if kind == "PUT" else 400), entry_price=Decimal(2),
                label="Synthetic holding", price=None, unrealized=None)


def screen(row=None, current=None, candidates=None, **kwargs):
    row = row or position()
    current = current or quote(row["option_type"])
    candidates = candidates if candidates is not None else [quote(row["option_type"], days=28, bid="1.10", ask="1.11")]
    return rolls.evaluate([row], {row["key"]: {"current": current, "candidates": candidates}},
                          today=TODAY, **kwargs)


class RollScreenTests(unittest.TestCase):
    def test_put_cash_and_quantity_not_profit(self):
        alerts, _ = screen()
        alert = alerts[0]
        self.assertEqual(alert["close_cash_per_contract"], Decimal("-101"))
        self.assertEqual(alert["open_cash_per_contract"], Decimal("110"))
        self.assertEqual(alert["net_credit_per_contract"], Decimal("7.70"))
        self.assertEqual(alert["net_credit_total"], Decimal("15.40"))
        self.assertEqual(alert["fees_total"], Decimal("2.60"))
        self.assertEqual(alert["quantity"], -2)
        self.assertNotIn("profit", alert)

    def test_exact_put_credit_boundary(self):
        for bid, expected in (("1.073", 1), ("1.0729", 0)):
            with self.subTest(bid=bid):
                alerts, _ = screen(candidates=[quote(days=28, bid=bid, ask="1.08")])
                self.assertEqual(len(alerts), expected)

    def test_call_no_cash_boundary_and_signs(self):
        row = position("CALL")
        for new_ask, expected in (("1.987", 1), ("1.9871", 0)):
            alerts, _ = screen(row, quote("CALL", bid="2", ask="2.01"),
                               [quote("CALL", days=28, bid="1.98", ask=new_ask)])
            self.assertEqual(len(alerts), expected)
            if alerts:
                self.assertEqual(alerts[0]["net_credit_total"], 0)
                self.assertEqual(alerts[0]["close_action"], "Sell to close")
                self.assertEqual(alerts[0]["open_action"], "Buy to open")
                self.assertGreater(alerts[0]["close_cash_per_contract"], 0)
                self.assertLess(alerts[0]["open_cash_per_contract"], 0)

    def test_expiry_and_strike_boundaries(self):
        for days, strike, expected in ((70, "100", 1), (71, "100", 0), (7, "100", 0),
                                        (6, "100", 0), (28, "101", 0), (28, "99", 1)):
            with self.subTest(days=days, strike=strike):
                alerts, _ = screen(candidates=[quote(days=days, strike=strike, bid="1.10", ask="1.11")])
                self.assertEqual(len(alerts), expected)

    def test_spread_exact_half_midpoint(self):
        self.assertIsNone(rolls.quote_error(quote(bid="3", ask="5")))
        self.assertIn("50%", rolls.quote_error(quote(bid="3", ask="5.001")))
        alerts, _ = screen(current=quote(bid="3", ask="5.001"))
        self.assertFalse(alerts)

    def test_invalid_quotes_fail_closed_on_both_legs(self):
        invalid = [("bid", "NaN"), ("ask", "Infinity"), ("bid", 0), ("ask", None),
                   ("bid", -1), ("bid", 10), ("multiplier", 10), ("contract_size", 150),
                   ("standard", False), ("key", "mismatched")]
        for field, value in invalid:
            for leg in ("current", "candidate"):
                with self.subTest(field=field, value=value, leg=leg):
                    current = quote()
                    candidate = quote(days=28, bid="1.10", ask="1.11")
                    (current if leg == "current" else candidate)[field] = value
                    self.assertFalse(screen(current=current, candidates=[candidate])[0])

    def test_open_interest_and_last_trade_do_not_filter(self):
        current = quote()
        new = quote(days=28, bid="1.10", ask="1.11")
        for data in (current, new):
            data.update(open_interest=0, last_trade_date=None)
        self.assertEqual(len(screen(current=current, candidates=[new])[0]), 1)
        new["last_trade_date"] = date(2000, 1, 1)
        current["open_interest"] = None
        self.assertEqual(len(screen(current=current, candidates=[new])[0]), 1)

    def test_ranking_lowest_strike_then_shortest_term_then_credit(self):
        candidates = [quote(strike="99", days=42, bid="2", ask="2.01"),
                      quote(strike="100", days=14, bid="5", ask="5.01"),
                      quote(strike="99", days=28, bid="1.10", ask="1.11")]
        alerts, _ = screen(candidates=candidates)
        self.assertEqual(alerts[0]["candidate_key"], candidates[2]["key"])

    def test_other_underlying_rejected(self):
        candidate = quote(symbol="ABC", days=28, bid="1.10", ask="1.11")
        self.assertFalse(screen(candidates=[candidate])[0])

    def test_eligibility_and_expiry_list(self):
        rows = [position(), position("CALL"), dict(position(), is_option=False)]
        eligible, skipped = rolls.eligible_positions(rows, TODAY)
        self.assertEqual(len(eligible), 2)
        self.assertFalse(skipped)
        changes = [("status", "uncertain"), ("status", "assumed_expired_worthless"),
                   ("quantity", "NaN"), ("quantity", 0), ("quantity", "1.5"),
                   ("quantity", 1), ("cost_basis", None), ("cost_basis", 0),
                   ("expiry", TODAY), ("expiry", TODAY + timedelta(days=70)),
                   ("key", "unresolved"), ("strike", "100.0005")]
        for key, value in changes:
            eligible, skipped = rolls.eligible_positions([dict(position(), **{key: value})], TODAY)
            self.assertFalse(eligible, (key, value))
            self.assertTrue(skipped)
        dates = [TODAY + timedelta(days=days) for days in (0, 7, 14, 21, 28, 35, 42, 70, 71)]
        self.assertEqual(rolls.candidate_expiries(dates, TODAY + timedelta(days=7), TODAY), dates[2:6])

    def test_invalid_fee_and_credit_rejected(self):
        for value in ("NaN", "Infinity", "-1", None, "garbage"):
            with self.assertRaises(ValueError):
                rolls.parameters(fee=value)
            with self.assertRaises(ValueError):
                rolls.parameters(put_min_credit=value)
        with self.assertRaises(ValueError):
            rolls.parameters(put_min_credit=0)
        self.assertEqual(rolls.parameters(fee=0)[0], 0)


class OpeningTradeDatesTests(unittest.TestCase):
    def setUp(self):
        self.row = position()
        self.flat_day = date(2026, 7, 31)
        self.first = date(2026, 8, 3)
        self.second = date(2026, 8, 4)

    def statement(self, end=None, quantity=0, pending=(), row=None):
        row = self.row if row is None else row
        holding = {field: row[field] for field in
                   ("symbol", "label", "option_type", "strike", "expiry")}
        holding.update(asset_class="Options", quantity=float(quantity))
        return {"period_end": end or self.flat_day,
                "_holdings": [holding] if quantity else [],
                "_pending": list(pending), "_transactions": []}

    def trade(self, day, quantity=2, action="Sale", intent="open", row=None):
        row = self.row if row is None else row
        return {"occ_symbol": row["key"], "symbol": row["symbol"], "is_option": True,
                "option_type": row["option_type"], "strike": row["strike"],
                "expiry": row["expiry"], "trade_date": day, "quantity": quantity,
                "action": action, "intent": intent,
                "settle_date": date(2026, 10, 1), "net_amount": 123.45}

    def test_single_opening_date_for_short_put_and_long_call(self):
        for kind, action in (("PUT", "Sale"), ("CALL", "Purchase")):
            for dataframe in (False, True):
                with self.subTest(kind=kind, dataframe=dataframe):
                    row = position(kind)
                    trades = [self.trade(self.first, action=action, row=row)]
                    if dataframe:
                        trades = pd.DataFrame(trades)
                    result = opening_trade_dates([self.statement()], trades, [row])
                    self.assertEqual(result, {row["key"]: (self.first,)})
                    self.assertIs(type(result[row["key"]][0]), date)

    def test_multiple_dates_are_sorted_and_unique(self):
        trades = [self.trade(self.second, 1), self.trade(self.first, 1),
                  self.trade(self.first, 1)]
        row = dict(self.row, quantity=Decimal(-3))
        self.assertEqual(opening_trade_dates([self.statement()], trades, [row]),
                         {row["key"]: (self.first, self.second)})

    def test_partial_close_retains_a_single_distinct_opening_date(self):
        for kind, opening, closing in (("PUT", "Sale", "Purchase"),
                                        ("CALL", "Purchase", "Sale")):
            with self.subTest(kind=kind):
                row = dict(position(kind), quantity=Decimal(-1 if kind == "PUT" else 1))
                trades = [self.trade(self.first, 2, opening, row=row),
                          self.trade(self.first, 1, opening, row=row),
                          self.trade(self.second, 2, closing, "close", row=row)]
                self.assertEqual(opening_trade_dates([self.statement()], trades, [row]),
                                 {row["key"]: (self.first,)})

    def test_multidate_partial_close_stays_unknown_after_another_open(self):
        trades = [self.trade(self.first, 2), self.trade(self.second, 1),
                  self.trade(date(2026, 8, 5), 1, "Purchase", "close")]
        self.assertEqual(opening_trade_dates([self.statement()], trades, [self.row]),
                         {self.row["key"]: None})
        trades.append(self.trade(date(2026, 8, 6), 1))
        row = dict(self.row, quantity=Decimal(-3))
        self.assertEqual(opening_trade_dates([self.statement()], trades, [row]),
                         {row["key"]: None})

    def test_full_close_reopen_discards_prior_dates(self):
        reopened = date(2026, 8, 6)
        trades = [self.trade(self.first),
                  self.trade(self.second, 2, "Purchase", "close"),
                  self.trade(reopened)]
        # Reconstruction uses trade dates, not list order or settlement dates.
        self.assertEqual(opening_trade_dates([self.statement()], trades[::-1], [self.row]),
                         {self.row["key"]: (reopened,)})

    def test_full_close_resolves_multidate_partial_close_ambiguity(self):
        reopened = date(2026, 8, 7)
        trades = [self.trade(self.first, 2), self.trade(self.second, 1),
                  self.trade(date(2026, 8, 5), 1, "Purchase", "close"),
                  self.trade(date(2026, 8, 6), 2, "Purchase", "close"),
                  self.trade(reopened)]
        self.assertEqual(opening_trade_dates([self.statement()], trades, [self.row]),
                         {self.row["key"]: (reopened,)})

    def test_pending_exposure_is_checked_on_the_statement_trade_date(self):
        end = date(2026, 8, 31)
        for action, intent, expected in (("Sale", "open", -3),
                                          ("Purchase", "close", -1)):
            with self.subTest(intent=intent):
                pending = self.trade(end, 1, action, intent)
                # Statement dates can be synthetic and must never supply the answer.
                printed = dict(pending, trade_date=date(2000, 1, 1), category=action)
                records = [self.statement(), self.statement(end, -2, [printed]),
                           self.statement(date(2026, 9, 30), expected)]
                row = dict(self.row, quantity=Decimal(expected))
                dates = (self.first, end) if intent == "open" else (self.first,)
                self.assertEqual(opening_trade_dates(records[::-1],
                                 [pending, self.trade(self.first)], [row]),
                                 {row["key"]: dates})

    def test_latest_net_flat_snapshot_supersedes_unknown_prior_history(self):
        end = date(2026, 8, 31)
        pending = self.trade(end, 9, "Purchase", "close")
        records = [self.statement(date(2026, 6, 30)),
                   self.statement(quantity=-9), self.statement(end, -9, [pending])]
        reopened = date(2026, 9, 3)
        # The old missing intent and missing confirmations precede proven flatness.
        old = self.trade(self.first, 7, intent=None)
        self.assertEqual(opening_trade_dates(records, [old, self.trade(reopened)], [self.row]),
                         {self.row["key"]: (reopened,)})

    def test_pending_only_position_is_not_a_flat_anchor(self):
        pending = self.trade(self.flat_day)
        records = [self.statement(pending=[pending])]
        self.assertEqual(opening_trade_dates(records, [pending], [self.row]),
                         {self.row["key"]: None})

    def test_unknown_starting_holding_and_absent_records(self):
        held = self.statement(date(2026, 8, 31), -2)
        held["_transactions"] = [self.trade(self.first)]
        for records in ([], [held]):
            with self.subTest(records=bool(records)):
                self.assertEqual(opening_trade_dates(records, [self.trade(self.first)],
                                                    [self.row]),
                                 {self.row["key"]: None})

    def test_missing_dates_or_intent_are_not_inferred(self):
        for field, value in (("trade_date", None), ("trade_date", pd.NaT),
                             ("intent", None), ("intent", pd.NA), ("intent", "")):
            with self.subTest(field=field, value=value):
                trade = dict(self.trade(self.first), **{field: value})
                record = self.statement()
                record["_transactions"] = [self.trade(self.first)]
                row = dict(self.row, price_as_of=datetime(2026, 10, 9, tzinfo=timezone.utc))
                self.assertEqual(opening_trade_dates([record], [trade], [row]),
                                 {row["key"]: None})
        for field in ("trade_date", "intent"):
            trade = self.trade(self.first)
            del trade[field]
            self.assertEqual(opening_trade_dates([self.statement()], [trade], [self.row]),
                             {self.row["key"]: None})
        trades = [self.trade(self.first, 3), self.trade(self.second, 1, "Purchase", None)]
        self.assertEqual(opening_trade_dates([self.statement()], trades, [self.row]),
                         {self.row["key"]: None})

    def test_undated_confirmation_cannot_be_assigned_before_latest_flat(self):
        records = [self.statement(), self.statement(date(2026, 8, 31))]
        trades = [self.trade(None), self.trade(date(2026, 9, 3))]
        self.assertEqual(opening_trade_dates(records, trades, [self.row]),
                         {self.row["key"]: None})

    def test_trade_date_timestamp_is_returned_as_a_calendar_date(self):
        trades = pd.DataFrame([self.trade(pd.Timestamp(self.first))])
        result = opening_trade_dates([self.statement()], trades, [self.row])
        self.assertEqual(result, {self.row["key"]: (self.first,)})
        self.assertIs(type(result[self.row["key"]][0]), date)

    def test_intervening_and_current_quantity_mismatches_fail_closed(self):
        trades = [self.trade(self.first)]
        records = [self.statement(), self.statement(date(2026, 8, 31), -1),
                   self.statement(date(2026, 9, 30), -2)]
        self.assertEqual(opening_trade_dates(records, trades, [self.row]),
                         {self.row["key"]: None})
        for quantity in (-1, -3, 0):
            row = dict(self.row, quantity=Decimal(quantity))
            self.assertEqual(opening_trade_dates([self.statement()], trades, [row]),
                             {row["key"]: None})

    def test_pending_quantity_mismatch_is_not_hidden_by_matching_holdings(self):
        end = date(2026, 8, 31)
        pending = self.trade(end, 1)
        records = [self.statement(), self.statement(end, -2, [pending])]
        self.assertEqual(opening_trade_dates(records, [self.trade(self.first)], [self.row]),
                         {self.row["key"]: None})

    def test_impossible_side_and_intent_progressions_fail_closed(self):
        cases = [
            [self.trade(self.first, intent="close")],
            [self.trade(self.first, 3), self.trade(self.second, 1, "Purchase", "open")],
            [self.trade(self.first, 1), self.trade(self.second, 1, "Sale", "close")],
            [self.trade(self.first, 1, "Purchase"), self.trade(self.second, 3)],
            [self.trade(self.first, 1), self.trade(self.second, 3, "Purchase", "close"),
             self.trade(date(2026, 8, 5), 4)],
        ]
        # Each sequence nets to the current -2, but violates explicit opening/closing intent.
        for trades in cases:
            with self.subTest(trades=trades):
                self.assertEqual(opening_trade_dates([self.statement()], trades, [self.row]),
                                 {self.row["key"]: None})
        for quantity in (None, 0, float("nan"), float("inf"), "invalid"):
            trades = [self.trade(self.first, quantity)]
            self.assertEqual(opening_trade_dates([self.statement()], trades, [self.row]),
                             {self.row["key"]: None})

    def test_occ_keys_keep_dates_and_unknowns_separate(self):
        book = [self.row, position(strike="99"), position(days=28),
                position("CALL"), position(symbol="ABC")]
        trades, expected = [], {}
        for index, row in enumerate(book):
            day = self.first + timedelta(days=index)
            action = "Purchase" if row["option_type"] == "CALL" else "Sale"
            trades.append(self.trade(day, action=action, row=row))
            expected[row["key"]] = (day,)
        trades[-1]["intent"] = None
        expected[book[-1]["key"]] = None
        self.assertEqual(opening_trade_dates([self.statement()], trades, book), expected)

    def test_latest_flat_anchor_is_selected_per_contract(self):
        other = position(strike="99")
        end = date(2026, 8, 31)
        later = date(2026, 9, 3)
        records = [self.statement(), self.statement(end, -2)]
        trades = [self.trade(self.first), self.trade(self.first, 7, intent=None, row=other),
                  self.trade(later, row=other)]
        self.assertEqual(opening_trade_dates(records, trades, [self.row, other]),
                         {self.row["key"]: (self.first,), other["key"]: (later,)})

    def test_absent_trade_history_and_empty_book(self):
        for trades in (None, [], pd.DataFrame()):
            self.assertEqual(opening_trade_dates([self.statement()], trades, [self.row]),
                             {self.row["key"]: None})
        self.assertEqual(opening_trade_dates([self.statement()], [self.trade(None)], []), {})

    def test_inputs_are_not_mutated(self):
        end = date(2026, 8, 31)
        pending = self.trade(end, 1, "Purchase", "close")
        records = [self.statement(), self.statement(end, -3, [pending])]
        records[-1]["_transactions"] = [self.trade(date(1999, 1, 1))]
        trades = [pending, self.trade(self.first, 3)]
        book = [dict(self.row, original_trade_dates=(date(1998, 1, 1),))]
        saved_records, saved_trades, saved_book = deepcopy((records, trades, book))
        for dataframe in (False, True):
            source = pd.DataFrame(trades) if dataframe else trades
            saved_source = deepcopy(source)
            self.assertEqual(opening_trade_dates(records, source, book),
                             {self.row["key"]: (self.first,)})
            self.assertEqual(records, saved_records)
            self.assertEqual(trades, saved_trades)
            self.assertEqual(book, saved_book)
            if dataframe:
                pd.testing.assert_frame_equal(source, saved_source)


if __name__ == "__main__":
    unittest.main()
