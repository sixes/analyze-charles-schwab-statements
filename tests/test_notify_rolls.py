import copy
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from html import escape
from html.parser import HTMLParser
from unittest.mock import patch

from schwab import notify, rolls


class RenderedText(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.text, self.tags, self.tables = [], [], []
        self.in_style = False
        self.table = self.row = self.cell = None
        self.header = False
        self.feed(html)

    def handle_data(self, data):
        if self.in_style:
            return
        self.text.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "style":
            self.in_style = True
        elif tag == "table":
            self.table = {"class": dict(attrs).get("class"), "headers": [], "rows": []}
            self.tables.append(self.table)
        elif tag == "tr":
            self.row, self.header = [], False
        elif tag in ("td", "th"):
            self.cell = []
            self.header = tag == "th"

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        elif tag in ("td", "th"):
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr":
            if self.header:
                self.table["headers"] = self.row
            else:
                self.table["rows"].append(self.row)
        elif tag == "table":
            self.table = None

    def normalized(self):
        return " ".join(" ".join(self.text).replace(";", " ").split())


def quote(kind, strike, expiry, bid, ask, fetched_at):
    key = rolls.contract_key("XYZ", expiry, kind, strike)
    return {
        "key": key,
        "provider_symbol": f"XYZ{expiry:%y%m%d}{kind[0]}{int(Decimal(str(strike)) * 1000)}.US",
        "symbol": "XYZ", "option_type": kind, "strike": Decimal(str(strike)), "expiry": expiry,
        "bid": Decimal(bid), "ask": Decimal(ask), "standard": True,
        "multiplier": 100, "contract_size": 100, "source": "Longbridge OPRA depth",
        "fetched_at": fetched_at,
    }


def evaluated_alert(kind="PUT", zero_credit=False):
    today = date(2026, 8, 3)
    close_time = datetime(2026, 8, 3, 14, 0, 1, tzinfo=timezone.utc)
    open_time = datetime(2026, 8, 3, 14, 0, 9, tzinfo=timezone.utc)
    if kind == "PUT":
        bid, ask, new_bid, new_ask = "4.00", "4.20", "4.80", "5.00"
        quantity, basis = -3, -1500
    else:
        bid, ask, new_bid, new_ask = "8.20", "8.40", "7.80", "8.00"
        quantity, basis = 2, 1700
        if zero_credit:
            bid = "8.01"
    current = quote(kind, 70, date(2026, 8, 21), bid, ask, close_time)
    candidate = quote(kind, 65, date(2026, 9, 18), new_bid, new_ask, open_time)
    row = {
        "key": current["key"], "symbol": "XYZ", "option_type": kind,
        "quantity": quantity, "cost_basis": basis, "strike": Decimal(70),
        "expiry": current["expiry"], "is_option": True, "status": "open",
        "original_trade_dates": (date(2026, 8, 3),),
    }
    positions, skipped = rolls.eligible_positions([row], today=today)
    if skipped:
        raise AssertionError(skipped)
    alerts, skipped = rolls.evaluate(
        positions, {current["key"]: {"current": current, "candidates": [candidate]}},
        fee=Decimal("0.50") if zero_credit else Decimal("0.65"), today=today,
    )
    if skipped or len(alerts) != 1:
        raise AssertionError((alerts, skipped))
    return alerts[0]


class RollNotificationTests(unittest.TestCase):
    def assert_formats(self, alerts, facts=(), diagnostics=None):
        plain = notify.roll_body(alerts, diagnostics)
        html = notify.roll_html(alerts, diagnostics)
        rendered = RenderedText(html)
        text_lines = [line for line in plain.splitlines() if not line or not set(line) <= set("-+")]
        normalized = " ".join(" ".join(text_lines).replace("|", " ").replace(";", " ").split())
        self.assertEqual(normalized, rendered.normalized())
        for fact in facts:
            with self.subTest(fact=fact):
                self.assertIn(fact, plain)
                self.assertIn(" ".join(fact.replace(";", " ").split()), rendered.normalized())
        return plain, html

    def comparison(self, alert):
        parsed = RenderedText(notify.roll_html([alert]))
        return next(table for table in parsed.tables if table["class"] == "roll-comparison")

    def test_two_rows_seven_columns_and_numeric_costs(self):
        for kind, size, cost, entry in (("PUT", "3", "5.00", "4.80"),
                                       ("CALL", "2", "8.50", "8.00")):
            with self.subTest(kind=kind):
                table = self.comparison(evaluated_alert(kind))
                self.assertEqual(table["headers"], ["Contract", "Size", "Close / Open", "Cost ($/share)",
                                                    "Current ($/share)", "Trade date", "Expiry"])
                old, new = table["rows"]
                self.assertEqual(len(old), 7)
                self.assertEqual(len(new), 7)
                self.assertTrue(old[0].startswith("Original · XYZ $70.00"))
                self.assertTrue(new[0].startswith("New · XYZ $65.00"))
                self.assertEqual([old[1], new[1]], [size, size])
                self.assertEqual([old[3], new[3]], [cost, entry])
                for row in (old, new):
                    self.assertRegex(row[3], r"^\d[\d,]*\.\d{2}$")
                self.assertEqual(old[5:], ["08-03-26", "08-21-26"])
                self.assertEqual(new[5:], ["-", "09-18-26"])

    def test_put_economics_and_provenance_in_both_formats(self):
        alert = evaluated_alert()
        self.assert_formats([alert], [
            "Buy to close", "Sell to open", "Gap +$0.60/share", "Net credit $176.10", "+28 days",
            "Close debit $1,260.00", "open credit $1,440.00", "fees $3.90",
            "net credit $58.70/contract", "46 DTE", "Gap before fees", "net after fees",
            "Bid $4.00 / Ask $4.20", "Bid $4.80 / Ask $5.00", "4.88% / 4.08%",
            "08-03-26 14:00:01 UTC", "08-03-26 14:00:09 UTC", "Longbridge OPRA depth",
            "$0.65/contract/leg", "Short puts: longer assignment/downside exposure.",
        ])
        old, new = self.comparison(alert)["rows"]
        self.assertEqual(old[2], "Buy to close ask")
        self.assertEqual(new[2], "Sell to open bid")
        self.assertTrue(old[4].startswith("4.20 "))
        self.assertTrue(new[4].startswith("4.80 "))

    def test_call_economics_and_unusual_match_warning(self):
        alert = evaluated_alert("CALL")
        self.assert_formats([alert], [
            "Sell to close", "Buy to open", "Gap +$0.20/share", "Net credit $37.40",
            "Close credit $1,640.00", "open debit $1,600.00", "fees $2.60",
            "net credit $18.70/contract", "No-cash long calls: unusual quotes; verify both legs.",
        ])
        old, new = self.comparison(alert)["rows"]
        self.assertEqual(old[2], "Sell to close bid")
        self.assertEqual(new[2], "Buy to open ask")

    def test_colors_highlight_actions_and_cash_without_replacing_labels(self):
        html = notify.roll_html([evaluated_alert()])
        self.assertIn(f"color:{notify.ROLL_CLOSE_COLOR}", html)
        self.assertIn(f"color:{notify.ROLL_OPEN_COLOR}", html)
        self.assertIn(f"color:{notify.CREDIT_COLOR}", html)
        self.assertIn("<strong>Buy to close</strong>", html)
        self.assertIn("<strong>Sell to open</strong>", html)
        self.assertIn("<strong>4.20</strong>", html)
        self.assertIn("Net credit $176.10", html)
        self.assertIn('data-label="Size"', html)

    def test_limits_diagnostics_and_multiple_candidates_are_not_repeated(self):
        diagnostics = ["ABC: no depth", "Scan truncated at quota; coverage is partial", "ABC: no depth"]
        plain, html = self.assert_formats([evaluated_alert(), evaluated_alert("CALL")], [
            "2 roll candidates · manual review", "70 calendar DTE", "spread ≤50% of midpoint",
            "No open-interest or last-trade filter", "first 4 eligible expiries",
            "≤500 weighted attempts/pass and rolling minute", "metadata, depth, retries",
            "Not exhaustive", "no exchange quote timestamp", "100-share multiplier and size",
            "Verify deliverables, full-size fills and buying power manually",
            "Net credit is not profit or realized P&L", "no orders placed", "Coverage / skipped",
        ], diagnostics)
        self.assertEqual(plain.count("ABC: no depth"), 1)
        self.assertEqual(plain.count("Cost/share: original"), 1)
        self.assertEqual(sum(t["class"] == "roll-comparison" for t in RenderedText(html).tables), 2)
        self.assertNotIn("Position and term", plain)
        self.assertNotIn("Conservative close price used", plain)

    def test_all_displayed_external_text_is_escaped(self):
        payload = '<img src=x onerror="alert(1)"> & <script>bad()</script>'
        base = evaluated_alert()
        paths = [("symbol",), ("close_action",), ("open_action",),
                 *((leg, field) for leg in ("current", "candidate")
                   for field in ("symbol", "option_type", "expiry", "source", "fetched_at"))]
        for path in paths:
            with self.subTest(path=path):
                alert = copy.deepcopy(base)
                target = alert
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = payload
                plain, html = self.assert_formats([alert])
                self.assertIn(payload, plain)
                self.assertIn(escape(payload), html)
                self.assertNotIn(payload, html)
                self.assertNotIn("img", RenderedText(html).tags)
                self.assertNotIn("script", RenderedText(html).tags)
        plain, html = self.assert_formats([base], diagnostics=[payload])
        self.assertIn(payload, plain)
        self.assertIn(escape(payload), html)
        alert = copy.deepcopy(base)
        alert["original_trade_dates"] = (payload,)
        self.assertIn(escape(payload), notify.roll_html([alert]))

    def test_fractional_strike_identity_and_mobile_document_metadata(self):
        alert = evaluated_alert()
        alert["current"]["strike"] = Decimal("70.125")
        alert["candidate"]["strike"] = Decimal("65.375")
        plain, html = self.assert_formats([alert], ["$70.125 put", "$65.375 put"])
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', html)
        self.assertNotIn("$70.12 put", plain)
        self.assertNotIn("$65.38 put", plain)

    def test_unknown_and_multiple_original_dates_not_replaced_with_other_dates(self):
        alert = evaluated_alert()
        alert.pop("original_trade_dates")
        self.assertEqual(self.comparison(alert)["rows"][0][5], "n/a")
        alert["original_trade_dates"] = (date(2026, 8, 3), date(2026, 8, 4))
        self.assertEqual(self.comparison(alert)["rows"][0][5], "08-03-26, 08-04-26")
        self.assertEqual(self.comparison(alert)["rows"][1][5], "-")

    def test_missing_retrieval_time_is_not_filled_with_last_trade_time(self):
        alert = evaluated_alert()
        for leg in ("current", "candidate"):
            alert[leg].pop("fetched_at")
            alert[leg]["last_trade_time"] = "1900-01-01"
        plain, html = self.assert_formats([alert], ["Original: n/a", "New: n/a"])
        self.assertNotIn("1900-01-01", plain + html)

    def test_subjects_are_short_and_not_profit_claims(self):
        put, call = evaluated_alert(), evaluated_alert("CALL")
        self.assertEqual(notify.roll_subject([put]), "Schwab: 1 roll candidate to review")
        self.assertEqual(notify.roll_subject([put, call]), "Schwab: 2 roll candidates to review")

    def test_empty_alerts_do_not_send_even_with_diagnostics(self):
        with patch.object(notify, "send") as send:
            for diagnostics in (None, ["no valid quotes"]):
                self.assertIsNone(notify.roll_subject([]))
                self.assertEqual(notify.roll_body([], diagnostics), "")
                self.assertEqual(notify.roll_html([], diagnostics), "")
                self.assertIs(notify.announce_rolls([], diagnostics), False)
            send.assert_not_called()

    def test_announce_uses_existing_transport_and_propagates_result(self):
        alerts = [evaluated_alert(), evaluated_alert("CALL")]
        diagnostics = ["ABC: no valid quotes"]
        for result in (True, False):
            with self.subTest(result=result), patch.object(notify, "send", return_value=result) as send:
                self.assertIs(notify.announce_rolls(alerts, diagnostics), result)
                send.assert_called_once_with(notify.roll_subject(alerts), notify.roll_body(alerts, diagnostics),
                                            notify.roll_html(alerts, diagnostics))

    def test_zero_credit_call_is_still_a_candidate(self):
        alert = evaluated_alert("CALL", zero_credit=True)
        self.assert_formats([alert], ["Gap +$0.01/share", "Net credit $0.00", "fees $2.00",
                                     "net credit $0.00/contract", "$0.50/contract/leg"])
        with patch.object(notify, "send", return_value=True) as send:
            self.assertTrue(notify.announce_rolls([alert]))
            send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
