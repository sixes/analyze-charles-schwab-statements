"""Parse a Charles Schwab monthly brokerage statement PDF into a record dict.

Statements now serve as the audit trail: trades arrive from eConfirm emails, and a
statement is what proves the confirm feed was complete and correctly parsed. So this
module must stay faithful to what is printed and return None for anything absent -
a missing figure and a zero figure mean different things.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pdfplumber

from .domain import CLASS_COLUMNS
from .text import MONTH_NAMES, compact, first_num, nums, stamp_date, to_num

logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

PERIOD_RE = re.compile(rf"({'|'.join(MONTH_NAMES)})(\d{{1,2}})-(\d{{1,2}}),(\d{{4}})")
PERIOD_SPAN_RE = re.compile(
    rf"({'|'.join(MONTH_NAMES)})(\d{{1,2}})-({'|'.join(MONTH_NAMES)})(\d{{1,2}}),(\d{{4}})"
)
FILE_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

HOLDING_RE = re.compile(
    r"^(?P<sym>[A-Z][A-Z0-9.\-/]{0,14})\s+"
    r"(?P<desc>\S.*?)\s+"
    r"(?P<qty>\(?[\d,]+\.\d{4}\)?S?)\s+"
    r"(?P<price>[\d,]+\.\d{2,5})\s+"
    r"(?P<mv>\(?\$?[\d,]+\.\d{2}\)?)"
    r"(?:\s+(?P<cb>\(?\$?[\d,]+\.\d{2}\)?)\s+(?P<ug>\(?\$?[\d,]+\.\d{2}\)?))?"
)

# -- transaction detail ----------------------------------------------------
# Every cash-affecting row starts with one of these category words. Rows whose
# category is Other carry no Amount column, only a quantity.
TXN_CATEGORIES = (
    "Sale",
    "Purchase",
    "Dividend",
    "Interest",
    "Other",
    "Journal",
    "Transfer",
    "Deposit",
    "Withdrawal",
    "Fee",
    "Tax",
    "Reinvestment",
    "Exchange",
    "Reorganization",
    "Adjustment",
    "Margin",
    "Check",
    "Wire",
    "Misc",
)

# Stricter than TOKEN_NUM_RE: a trailing letter would let strike tokens such as
# 450.00P into the value tail, and a leading $ would let description fragments
# such as $106 in.
TXN_VALUE_RE = re.compile(r"^\(?[\d,]+\.\d{2,5}\)?$")

# Realized gain carries its term as a suffix: 280.67(ST), 2,198.62,(ST),
# (188.66)(LT). to_num() cannot be used on these - it reads the term's opening
# parenthesis as a negative sign.
TXN_REALIZED_RE = re.compile(r"^\(?[\d,]+\.\d{2}\)?,?\((ST|LT)\)$")

# Pending rows put the settle date between the charges and amount columns, so
# the value tail cannot simply be scanned from the right.
PENDING_ROW_RE = re.compile(
    r"^(?:Pending\s+)?(?:(?P<date>\d{2}/\d{2})\s+)?"
    r"(?P<action>[A-Z][A-Za-z]+)\s+"
    r"(?P<sym>[A-Z][A-Z0-9./\-]{0,20})\s+"
    r"(?P<desc>\S+)\s+"
    r"(?P<qty>\(?[\d,]+\.\d{2,5}\)?)\s+"
    r"(?P<price>[\d,]+\.\d{2,5})\s+"
    r"(?P<settle>\d{2}/\d{2})\s+"
    r"(?P<amount>\(?[\d,]+\.\d{2}\)?)$"
)

PENDING_END_RE = re.compile(
    r"^(?:TotalPendingTransactions|OpenOrders|EndnotesForYourAccount)"
)

TXN_END_MARKERS = (
    "Pending/OpenActivity",
    "PendingTransactions",
    "TotalPendingTransactions",
    "OpenOrders",
    "EndnotesForYourAccount",
    "TermsandConditions",
    "GENERALINFORMATION",
    "AccountSummary",
    "Positions-",
)

# Page furniture that appears between transaction rows on continuation pages.
TXN_SKIP_RE = re.compile(
    r"^(?:\(continued\)|Symbol/|DateCategory|Date\s|Price/Rate|\d+of\d+$"
    r"|AccountNumber|StatementPeriod|.*Accountof$)"
)

# Extraction glues the ticker to whatever follows it: WMT08/21/2026PUTWALMARTINC,
# USD08/21/2026, RAM07/17/2026.
SYMBOL_SPLIT_RE = re.compile(r"^(?P<sym>[A-Z]{1,6})(?P<tail>(?:\d{2}/\d{2}/\d{4}|PUT|CALL).*)$")

POSITION_SECTIONS = [
    ("Positions-Equities", "Equities"),
    ("Positions-ExchangeTradedFunds", "ETFs"),
    ("Positions-Options", "Options"),
    ("Positions-MutualFunds", "Mutual Funds"),
    ("Positions-FixedIncome", "Fixed Income"),
    ("Positions-Other", "Other"),
]

SECTION_BREAKERS = (
    "Positions-Summary",
    "Transactions-Summary",
    "TransactionDetails",
    "PendingTransactions",
    "AssetAllocation",
    "AccountSummary",
    "GENERALINFORMATION",
    "OptionCustomers",
    "EstimatedAnnualIncome",
)

ALLOC_LABELS = [
    ("CashandCashInvestments", "Cash"),
    ("MoneyMarketFunds", "Money Market"),
    ("Equities", "Equities"),
    ("ExchangeTradedFunds", "ETFs"),
    ("MutualFunds", "Mutual Funds"),
    ("FixedIncome", "Fixed Income"),
    ("Options", "Options"),
    ("OtherAssets", "Other"),
]


class StatementParser:
    def __init__(self, source, verbose: bool = False, name: str = None):
        self.verbose = verbose
        if isinstance(source, (str, Path)):
            self.name = Path(source).name
            handle = str(source)
        else:
            self.name = name or Path(getattr(source, "name", "uploaded.pdf")).name
            handle = source
        with pdfplumber.open(handle) as pdf:
            self.pages = [(page.extract_text() or "").split("\n") for page in pdf.pages]
        self.lines = [line for page in self.pages for line in page]

    # -- helpers ----------------------------------------------------------
    def labeled_row(self, label: str, count: int, lines=None, exclude: str = None):
        """Value(s) from the last `count` numbers on the first line carrying `label`."""
        pattern = re.compile(label if exclude is None else f"{label}(?!{exclude})")
        for line in lines if lines is not None else self.lines:
            if pattern.search(compact(line)):
                values = nums(line)
                if len(values) >= count:
                    return values[-count:]
        return None

    def page_with(self, marker: str):
        for page in self.pages:
            for line in page:
                if compact(line).startswith(marker):
                    return page
        return None

    def row_after(self, marker: str, count: int):
        """First line after a section header that carries at least `count` numbers."""
        for page in self.pages:
            for index, line in enumerate(page):
                if not compact(line).startswith(marker):
                    continue
                for follower in page[index + 1 : index + 12]:
                    values = nums(follower)
                    if len(values) >= count:
                        return values
        return None

    # -- period -----------------------------------------------------------
    def parse_period(self):
        text = compact("\n".join(self.pages[0]))
        span = PERIOD_SPAN_RE.search(text)
        if span:
            m1, d1, m2, d2, year = span.groups()
            start = date(int(year), MONTH_NAMES.index(m1) + 1, int(d1))
            end = date(int(year), MONTH_NAMES.index(m2) + 1, int(d2))
            if end < start:  # period straddles a year boundary
                start = date(int(year) - 1, start.month, start.day)
            return start, end
        match = PERIOD_RE.search(text)
        if match:
            name, d1, d2, year = match.groups()
            month = MONTH_NAMES.index(name) + 1
            return date(int(year), month, int(d1)), date(int(year), month, int(d2))
        match = FILE_DATE_RE.search(self.name)
        if match:
            year, month, day = (int(g) for g in match.groups())
            return date(year, month, 1), date(year, month, day)
        raise ValueError(f"cannot determine statement period for {self.name}")

    # -- account summary --------------------------------------------------
    def parse_account_summary(self):
        page = self.pages[0]
        summary = {}
        fields = [
            ("beginning_value", "BeginningAccountValue", "asof"),
            ("deposits", "Deposits", None),
            ("withdrawals", "Withdrawals", None),
            ("dividends_interest", "DividendsandInterest", None),
            ("market_appreciation", "MarketAppreciation", None),
            ("expenses", "Expenses", None),
            ("ending_value", "EndingAccountValue", "asof"),
        ]
        for key, label, exclude in fields:
            row = self.labeled_row(label, 2, lines=page, exclude=exclude)
            if row:
                summary[key], summary[f"{key}_ytd"] = row

        if "ending_value" not in summary or "beginning_value" not in summary:
            fallback = re.search(
                r"EndingAccountValueasof\d\d/\d\d\s*BeginningAccountValueasof\d\d/\d\d",
                compact("\n".join(page)),
            )
            if fallback:
                for index, line in enumerate(page):
                    if "EndingAccountValueasof" in compact(line):
                        values = nums(page[index + 1]) if index + 1 < len(page) else []
                        if len(values) >= 2:
                            summary.setdefault("ending_value", values[0])
                            summary.setdefault("beginning_value", values[1])
                        break

        # Withdrawals and expenses reduce value; normalize the sign regardless of
        # whether Schwab printed them bare or in parentheses.
        for key in ("withdrawals", "withdrawals_ytd", "expenses", "expenses_ytd"):
            if summary.get(key) is not None:
                summary[key] = -abs(summary[key])
        return summary

    # -- asset allocation -------------------------------------------------
    def parse_allocation(self):
        page = self.page_with("AssetAllocation")
        if page is None:
            return {}
        allocation = {}
        for line in page:
            text = compact(line)
            for label, name in ALLOC_LABELS:
                if text.startswith(label) and name not in allocation:
                    value = first_num(line)
                    if value is not None:
                        allocation[name] = value
                    break
            if re.match(r"^Total[\$\d(]", text) and "total" not in allocation:
                value = first_num(line)
                if value is not None:
                    allocation["total"] = value
            if text.startswith("Liabilities") and "liabilities" not in allocation:
                value = first_num(line)
                if value is not None:
                    allocation["liabilities"] = value
        return allocation

    # -- gain / loss ------------------------------------------------------
    def parse_gain_loss(self):
        page = self.page_with("GainorLoss") or self.page_with("Gainor(Loss)")
        if page is None:
            return {}
        result = {}
        for line in page:
            text = compact(line)
            values = nums(line)
            if text.startswith("This") and len(values) >= 6 and "st_gain" not in result:
                (
                    result["st_gain"],
                    result["st_loss"],
                    result["st_net"],
                    result["lt_gain"],
                    result["lt_loss"],
                    result["lt_net"],
                ) = values[:6]
            elif text.startswith("YTD") and len(values) >= 2 and "st_net_ytd" not in result:
                result["st_net_ytd"], result["lt_net_ytd"] = values[:2]
            elif text.startswith("Unrealized") and values and "unrealized" not in result:
                result["unrealized"] = values[0]
        return result

    # -- income -----------------------------------------------------------
    def parse_income(self):
        page = self.page_with("IncomeSummary") or self.page_with("AssetAllocation")
        if page is None:
            return {}
        result = {}
        pairs = [
            ("interest", r"^(SchwabOne|BankSweep|Interest\b)"),
            ("dividends", r"^CashDividends"),
            ("income_total", r"^TotalIncome"),
        ]
        for line in page:
            text = compact(line)
            values = nums(line)
            if len(values) < 4:
                continue
            for key, pattern in pairs:
                if key not in result and re.search(pattern, text):
                    exempt, taxable, exempt_ytd, taxable_ytd = values[:4]
                    result[key] = exempt + taxable
                    result[f"{key}_ytd"] = exempt_ytd + taxable_ytd
                    break
        return result

    # -- positions reconciliation ----------------------------------------
    def parse_positions_summary(self):
        values = self.row_after("Positions-Summary", 6)
        if not values:
            return {}
        keys = [
            "pos_beginning",
            "pos_transfers",
            "pos_div_reinvested",
            "pos_cash_activity",
            "pos_market_change",
            "pos_ending",
            "cost_basis",
            "unrealized_gain",
        ]
        return dict(zip(keys, values))

    # -- transactions summary ---------------------------------------------
    def parse_transactions_summary(self):
        values = self.row_after("Transactions-Summary", 8)
        if not values:
            return {}
        keys = [
            "cash_beginning",
            "txn_deposits",
            "txn_withdrawals",
            "purchases",
            "sales",
            "txn_dividends_interest",
            "txn_expenses",
            "cash_ending",
        ]
        return dict(zip(keys, values[:8]))

    # -- margin -----------------------------------------------------------
    def parse_margin(self):
        values = self.row_after("MarginLoanInformation", 4)
        if not values:
            return {}
        keys = ["margin_opening", "margin_closing", "funds_available", "buying_power"]
        return dict(zip(keys, values[:4]))

    # -- holdings ---------------------------------------------------------
    def parse_holdings(self):
        holdings = []
        totals = {}
        cash = {}
        section = None
        unparsed = []
        for page in self.pages:
            for index, line in enumerate(page):
                text = compact(line)

                matched_header = False
                for marker, name in POSITION_SECTIONS:
                    if text.startswith(marker):
                        section, matched_header = name, True
                        break
                if matched_header:
                    continue
                if any(text.startswith(breaker) for breaker in SECTION_BREAKERS):
                    section = None
                    continue

                if text.startswith("TotalCashandCashInvestments"):
                    values = nums(line)
                    if len(values) >= 2:
                        cash = {"cash_beginning": values[0], "cash_ending": values[1]}
                    continue
                if re.match(r"^Total[A-Z]", text):
                    values = nums(line)
                    name = None
                    for marker, candidate in POSITION_SECTIONS:
                        if text.startswith("Total" + marker.split("-")[1]):
                            name = candidate
                            break
                    if name and len(values) >= 3:
                        totals[name] = {
                            "market_value": values[0],
                            "cost_basis": values[1],
                            "unrealized": values[2],
                        }
                    section = None
                    continue

                if section is None:
                    continue
                match = HOLDING_RE.match(line.strip())
                if match:
                    symbol = match.group("sym")
                    description = match.group("desc").rstrip(",")
                    holding = {
                        "asset_class": section,
                        "symbol": symbol,
                        "description": description,
                        "quantity": to_num(match.group("qty")),
                        "price": to_num(match.group("price")),
                        "market_value": to_num(match.group("mv")),
                        "cost_basis": to_num(match.group("cb")) if match.group("cb") else None,
                        "unrealized": to_num(match.group("ug")) if match.group("ug") else None,
                        "option_type": None,
                        "strike": None,
                        "expiry": None,
                    }
                    if section == "Options":
                        contract = compact(description).upper()
                        holding["option_type"] = (
                            "CALL" if contract.startswith("CALL") else "PUT" if contract.startswith("PUT") else None
                        )
                        lookahead = compact(" ".join(page[index + 1 : index + 5]))
                        expiry = re.search(r"EXP(\d{2}/\d{2}/\d{2})", lookahead)
                        strike = re.search(r"\$([\d,]+(?:\.\d+)?)", lookahead)
                        if expiry:
                            holding["expiry"] = expiry.group(1)
                        if strike:
                            holding["strike"] = to_num(
                                strike.group(1) if "." in strike.group(1) else strike.group(1) + ".00"
                            )
                    holding["label"] = self.holding_label(holding)
                    holdings.append(holding)
                elif self.verbose and nums(line):
                    unparsed.append(line.strip())
        if unparsed:
            print(f"  [verbose] {len(unparsed)} position lines not parsed as holdings")
            for line in unparsed[:15]:
                print(f"    | {line}")
        return holdings, totals, cash

    @staticmethod
    def holding_label(holding: dict) -> str:
        if holding["asset_class"] != "Options":
            return holding["symbol"]
        parts = [holding["symbol"]]
        if holding["strike"] is not None:
            parts.append(f"{holding['strike']:g}")
        if holding["option_type"]:
            parts.append(holding["option_type"][0])
        if holding["expiry"]:
            parts.append(holding["expiry"][:5])
        return " ".join(parts)

    # -- dated external cash flows ---------------------------------------
    def parse_cash_flows(self, start: date, end: date):
        flows = []
        current = None
        in_details = False
        for page in self.pages:
            for line in page:
                text = compact(line)
                if text.startswith("TransactionDetails"):
                    in_details = True
                    continue
                if text.startswith("PendingTransactions") or text.startswith("GENERALINFORMATION"):
                    in_details = False
                if not in_details:
                    continue
                stamp = re.match(r"^(\d{2})/(\d{2})\s", line.strip())
                if stamp:
                    month, day = int(stamp.group(1)), int(stamp.group(2))
                    year = end.year if month <= end.month else end.year - 1
                    try:
                        current = date(year, month, day)
                    except ValueError:
                        current = None
                body = re.sub(r"^\d{2}/\d{2}\s+", "", line.strip())
                head = compact(body)
                sign = 0
                if re.match(r"^Deposit\b", head) or "FundsReceived" in head:
                    sign = 1
                elif re.match(r"^Withdrawal\b", head) or "FundsPaid" in head:
                    sign = -1
                if sign:
                    values = nums(body)
                    if values:
                        amount = abs(values[-1]) * sign
                        flows.append(
                            {
                                "date": current or (start + (end - start) / 2),
                                "amount": amount,
                                "description": body[:70],
                            }
                        )
        return flows

    # -- transaction detail ------------------------------------------------
    @staticmethod
    def split_values(body: str):
        """Split a row into its head tokens, numeric tail, and realized gain.

        The tail is scanned from the right so that rows with a missing column
        (an expiration has only a quantity, credit interest only an amount)
        stay correctly aligned.
        """
        tokens = body.split()
        realized = term = None
        if tokens and TXN_REALIZED_RE.match(tokens[-1]):
            token = tokens.pop()
            term = "ST" if "(ST)" in token else "LT"
            realized = to_num(re.sub(r",?\((?:ST|LT)\)$", "", token))
        values = []
        while tokens and TXN_VALUE_RE.match(tokens[-1]):
            values.insert(0, to_num(tokens.pop()))
        return tokens, values, realized, term

    @staticmethod
    def option_details(description: str, continuation: str):
        """Option type, strike and expiry for a trade row.

        Strike is printed twice - glued to the description ($450) and in the
        continuation lines (450.00P) - so either source will do.
        """
        text = re.sub(r"^\d{2}/\d{2}/\d{4}", "", compact(description).upper())
        option_type = "CALL" if text.startswith("CALL") else "PUT" if text.startswith("PUT") else None
        strike = expiry = settle = None
        if option_type:
            tail = re.search(r"\$([\d,]+(?:\.\d+)?)$", text)
            if tail:
                strike = to_num(tail.group(1))
        found = re.search(r"EXP(\d{2}/\d{2}/\d{2})", continuation)
        if found:
            expiry = stamp_date(found.group(1))
        found = re.search(r"(\d{2}/\d{2}/\d{4})", continuation)
        if found:
            settle = stamp_date(found.group(1))
        if option_type and strike is None:
            residue = re.sub(r"\d{2}/\d{2}/\d{4}|EXP\d{2}/\d{2}/\d{2}", " ", continuation)
            found = re.search(r"([\d,]+\.\d{2})\s*[PC]\b", residue)
            if found:
                strike = to_num(found.group(1))
        return option_type, strike, expiry, settle

    @staticmethod
    def row_start(line: str) -> bool:
        body = line.strip()
        if re.match(r"^\d{2}/\d{2}\s+", body):
            return True
        head = body.split()
        return bool(head) and head[0] in TXN_CATEGORIES

    def transaction_rows(self, page: list):
        """Content lines of a page's transaction window, page furniture removed."""
        return [line for line in page if not TXN_SKIP_RE.match(compact(line))]

    def parse_transactions(self, start: date, end: date):
        transactions = []
        in_details = False
        unparsed = []
        for page in self.pages:
            lines = self.transaction_rows(page)
            current = None
            for index, line in enumerate(lines):
                text = compact(line)
                if text.startswith("TransactionDetails"):
                    in_details = True
                    continue
                if any(text.startswith(marker) for marker in TXN_END_MARKERS):
                    in_details = False
                if not in_details:
                    continue

                body = line.strip()
                stamp = re.match(r"^(\d{2})/(\d{2})\s+", body)
                if stamp:
                    month, day = int(stamp.group(1)), int(stamp.group(2))
                    year = end.year if month <= end.month else end.year - 1
                    try:
                        current = date(year, month, day)
                    except ValueError:
                        current = None
                    body = body[stamp.end() :]

                head, values, realized, term = self.split_values(body)
                if not head or head[0] not in TXN_CATEGORIES or not values:
                    if self.verbose and values and not stamp:
                        unparsed.append(line.strip())
                    continue

                category = head[0]
                rest = head[1:]
                action = None
                if rest and not rest[0].isupper():
                    action = rest.pop(0)
                symbol = None
                if rest:
                    split = SYMBOL_SPLIT_RE.match(rest[0])
                    if split:
                        symbol = split.group("sym")
                        rest[0] = split.group("tail")
                    elif len(rest) > 1 and re.match(r"^[A-Z][A-Z0-9./\-]{0,20}$", rest[0]):
                        symbol = rest.pop(0)
                description = " ".join(rest)

                follow = []
                for candidate in lines[index + 1 : index + 4]:
                    if self.row_start(candidate):
                        break
                    follow.append(candidate)
                continuation = compact(" ".join(follow))
                option_type, strike, expiry, settle = self.option_details(description, continuation)

                # The Other Activity category wraps onto the next line, which can
                # also carry the second half of the action (Option Assignment).
                if category == "Other":
                    wrapped = re.match(r"^Activity([A-Z][a-z]+)", continuation)
                    if wrapped:
                        action = f"{action} {wrapped.group(1)}".strip()
                    if not stamp and settle:
                        current = settle

                # Only the Other Activity category lacks an Amount column. With
                # a single value that value is a quantity, never cash.
                if category == "Other" and len(values) == 1:
                    quantity, price, charges, amount = values[0], None, None, None
                else:
                    amount = values[-1]
                    head_values = values[:-1]
                    quantity = head_values[0] if len(head_values) >= 1 else None
                    price = head_values[1] if len(head_values) >= 2 else None
                    charges = head_values[2] if len(head_values) >= 3 else None

                transactions.append(
                    {
                        "settled": True,
                        "trade_date": current or (start + (end - start) / 2),
                        "settle_date": settle,
                        "category": "Other Activity" if category == "Other" else category,
                        "action": action,
                        "symbol": symbol,
                        "description": description[:80],
                        "quantity": quantity,
                        "price": price,
                        "charges": charges,
                        "amount": amount,
                        "realized": realized,
                        "term": term,
                        "is_option": option_type is not None,
                        "option_type": option_type,
                        "strike": strike,
                        "expiry": expiry,
                    }
                )
        if unparsed and self.verbose:
            print(f"  [verbose] {len(unparsed)} transaction lines not parsed")
            for line in unparsed[:15]:
                print(f"    | {line}")
        return transactions

    def parse_pending(self, end: date):
        """Unsettled activity. Reported separately - it is not in account value."""
        pending = []
        printed_total = None
        in_pending = False
        for page in self.pages:
            lines = self.transaction_rows(page)
            current = None
            for index, line in enumerate(lines):
                text = compact(line)
                if text.startswith("Pending/OpenActivity"):
                    in_pending = True
                    continue
                if text.startswith("TotalPendingTransactions"):
                    printed_total = first_num(line)
                    in_pending = False
                if text.startswith("OpenOrders") or text.startswith("EndnotesForYourAccount"):
                    in_pending = False
                if not in_pending:
                    continue

                match = PENDING_ROW_RE.match(line.strip())
                if not match:
                    continue
                if match.group("date"):
                    month, day = (int(part) for part in match.group("date").split("/"))
                    year = end.year if month <= end.month else end.year - 1
                    try:
                        current = date(year, month, day)
                    except ValueError:
                        current = None

                description = match.group("desc")
                follow = []
                for candidate in lines[index + 1 : index + 4]:
                    if PENDING_ROW_RE.match(candidate.strip()):
                        break
                    # The section terminator sits directly under the last row; compact()
                    # would glue it to the strike and defeat the strike pattern.
                    if PENDING_END_RE.match(compact(candidate)):
                        break
                    follow.append(candidate)
                continuation = compact(" ".join(follow))
                option_type, strike, _, expiry = self.option_details(description, continuation)
                action = match.group("action")
                quantity = to_num(match.group("qty"))
                # Settled short sales print the quantity in parentheses; pending
                # rows print it bare. Normalize to the statement's own convention.
                if quantity is not None and action == "ShortSale":
                    quantity = -abs(quantity)
                settle_month, settle_day = (int(part) for part in match.group("settle").split("/"))
                settle_year = end.year if settle_month >= end.month else end.year + 1
                pending.append(
                    {
                        "settled": False,
                        "trade_date": current,
                        "settle_date": stamp_date(f"{settle_month:02d}/{settle_day:02d}/{settle_year}"),
                        "category": "Sale" if action == "ShortSale" else "Purchase" if action == "CoverShort" else action,
                        "action": action,
                        "symbol": match.group("sym"),
                        "description": description[:80],
                        "quantity": quantity,
                        "price": to_num(match.group("price")),
                        "charges": None,
                        "amount": to_num(match.group("amount")),
                        "realized": None,
                        "term": None,
                        "is_option": option_type is not None,
                        "option_type": option_type,
                        "strike": strike,
                        "expiry": expiry,
                    }
                )
        return pending, printed_total

    # -- assemble ---------------------------------------------------------
    def parse(self) -> dict:
        start, end = self.parse_period()
        holdings, totals, cash = self.parse_holdings()
        record = {
            "file": self.name,
            "period_start": start,
            "period_end": end,
            "days": (end - start).days + 1,
        }
        record.update(self.parse_account_summary())
        record.update(self.parse_positions_summary())
        record.update(self.parse_transactions_summary())
        record.update(self.parse_margin())
        record.update(self.parse_income())
        record.update(self.parse_gain_loss())
        record.update(cash)

        allocation = self.parse_allocation()
        for name in CLASS_COLUMNS:
            record[f"alloc_{name}"] = allocation.get(name)
        record["alloc_total"] = allocation.get("total")
        record["liabilities"] = allocation.get("liabilities")

        record["holdings_count"] = len(holdings)
        record["_holdings"] = holdings
        record["_class_totals"] = totals
        record["_flows"] = self.parse_cash_flows(start, end)
        record["_transactions"] = self.parse_transactions(start, end)
        pending, pending_total = self.parse_pending(end)
        record["_pending"] = pending

        # Tie parsed rows back to printed figures: the Transactions - Summary
        # block states the same cash movement the detail rows add up to.
        stated = [
            record.get(key)
            for key in ("txn_deposits", "txn_withdrawals", "purchases", "sales", "txn_dividends_interest", "txn_expenses")
        ]
        if any(value is not None for value in stated):
            derived = sum(row["amount"] or 0.0 for row in record["_transactions"])
            record["txn_amount_residual"] = abs(derived - sum(value or 0.0 for value in stated))
        else:
            record["txn_amount_residual"] = None

        record["pending_total"] = pending_total
        if pending_total is None:
            record["pending_residual"] = None
        else:
            record["pending_residual"] = abs(sum(row["amount"] or 0.0 for row in pending) - pending_total)
        return record
