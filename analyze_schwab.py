#!/usr/bin/env python3
"""Analyze Charles Schwab monthly brokerage statements.

Parses statement PDFs into a monthly performance time series, computes a full
attribution suite (time-weighted return via day-weighted Modified Dietz,
money-weighted IRR, realized/unrealized split, asset-class contribution, risk
statistics), and writes CSV/JSON data plus charts.

Usage:
    python3 analyze_schwab.py [--dir DIR] [--out DIR] [--rf 0.042] [--verbose]
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pdfplumber
from matplotlib.ticker import FuncFormatter

logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

MONTH_NAMES = list(calendar.month_name)[1:]

# A token counts as a number only if the entire token is numeric. Substring
# matching corrupts glued PDF artifacts (07/24/202649.50EXP07/24/26) and would
# silently swallow percent columns (46.40%).
TOKEN_NUM_RE = re.compile(r"^\(?\$?-?[\d,]+\.\d{2,5}\)?,?[A-Za-z]?$")

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


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
def compact(line: str) -> str:
    return re.sub(r"\s+", "", line)


def to_num(token: str):
    negative = "(" in token
    cleaned = re.sub(r"[^\d.]", "", token)
    if not cleaned or cleaned == ".":
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def nums(line: str) -> list:
    out = []
    for token in line.split():
        if TOKEN_NUM_RE.match(token):
            value = to_num(token)
            if value is not None:
                out.append(value)
    return out


def first_num(line: str):
    values = nums(line)
    return values[0] if values else None


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def stamp_date(text: str):
    """MM/DD/YYYY or MM/DD/YY as printed on statement rows."""
    if not text:
        return None
    parts = text.split("/")
    if len(parts) != 3:
        return None
    try:
        month, day, year = (int(part) for part in parts)
    except ValueError:
        return None
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# statement parsing
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# analytics
# --------------------------------------------------------------------------
def modified_dietz(beginning, ending, flows, start: date, end: date):
    """Day-weighted Modified Dietz return; falls back to mid-period weighting."""
    if not beginning or beginning <= 0:
        return None, None
    days = max((end - start).days + 1, 1)
    net = sum(flow["amount"] for flow in flows)
    weighted = 0.0
    for flow in flows:
        elapsed = (flow["date"] - start).days
        weight = (days - min(max(elapsed, 0), days)) / days
        weighted += flow["amount"] * weight
    denominator = beginning + weighted
    if denominator <= 0:
        return None, None
    gain = ending - beginning - net
    return gain / denominator, gain


def irr_monthly(cash_flows, low=-0.9999, high=10.0, tolerance=1e-10):
    """Monthly IRR by bisection. cash_flows[0] is at t=0."""

    def npv(rate):
        return sum(amount / (1 + rate) ** period for period, amount in enumerate(cash_flows))

    if npv(low) * npv(high) > 0:
        return None
    for _ in range(400):
        mid = (low + high) / 2
        value = npv(mid)
        if abs(value) < tolerance:
            return mid
        if npv(low) * value < 0:
            high = mid
        else:
            low = mid
    return (low + high) / 2


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


def transaction_frame(records: list, include_pending: bool = False) -> pd.DataFrame:
    """Every statement transaction as one row, tagged with its calendar month."""
    rows = []
    for record in records:
        activity = list(record.get("_transactions") or [])
        if include_pending:
            activity += list(record.get("_pending") or [])
        for row in activity:
            rows.append(dict(row, file=record.get("file"), period_end=record.get("period_end")))
    frame = pd.DataFrame(rows, columns=TXN_COLUMNS + ["file", "period_end"])
    if frame.empty:
        frame["month"] = pd.Series(dtype="object")
        return frame
    for column in TXN_NUMERIC:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["is_option"] = frame["is_option"].fillna(False).astype(bool)
    frame["settled"] = frame["settled"].fillna(True).astype(bool)
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["month"] = dates.dt.strftime("%Y-%m")
    return frame.sort_values("trade_date", kind="stable").reset_index(drop=True)


def premium_summary(frame: pd.DataFrame) -> dict:
    """Option premium from the printed Amount column.

    Amounts are net of commission, which is how Schwab prints them. Premium is
    not income: a short option's credit is a liability until the position is
    closed or expires.
    """
    options = frame[frame["is_option"]] if not frame.empty else frame
    if options.empty:
        opened = closed = bought = options
    else:
        opened = options[options["category"] == "Sale"]
        closed = options[options["action"] == "CoverShort"]
        bought = options[(options["category"] == "Purchase") & (options["action"] != "CoverShort")]
    collected = float(opened["amount"].sum())
    paid = float(closed["amount"].sum())
    realized = options["realized"] if not options.empty else pd.Series(dtype=float)
    term = options["term"] if not options.empty else pd.Series(dtype=object)
    return {
        "premium_collected": collected,
        "premium_paid_to_close": paid,
        "premium_net": collected + paid,
        "long_option_purchases": float(bought["amount"].sum()),
        "charges": float(options["charges"].sum()) if not options.empty else 0.0,
        "contracts_opened": float(opened["quantity"].abs().sum()) if not opened.empty else 0.0,
        "contracts_closed": float(closed["quantity"].abs().sum()) if not closed.empty else 0.0,
        # Derived, not printed: the 100-share multiplier is assumed.
        "gross_notional": float((opened["quantity"].abs() * opened["price"] * 100).sum()) if not opened.empty else 0.0,
        # Only closing trades print a realized gain. Expirations realize the
        # premium too, so these are smaller than the statement's Realized Gain
        # section, which is the authoritative figure for the period.
        "realized_closed_st": float(realized[term == "ST"].sum()),
        "realized_closed_lt": float(realized[term == "LT"].sum()),
        "trades": int(len(options)),
    }


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


def premium_by_month(frame: pd.DataFrame) -> pd.DataFrame:
    """premium_summary() split by calendar month of the trade date."""
    if frame.empty:
        return pd.DataFrame(columns=["month", *PREMIUM_MONTH_COLUMNS])
    rows = []
    for month, group in frame.groupby("month", dropna=True):
        summary = premium_summary(group)
        rows.append({"month": month, **{key: summary[key] for key in PREMIUM_MONTH_COLUMNS}})
    return pd.DataFrame(rows).sort_values("month").reset_index(drop=True)


def premium_by_symbol(frame: pd.DataFrame) -> pd.DataFrame:
    """premium_summary() split by underlying symbol, largest net premium first."""
    if frame.empty:
        return pd.DataFrame(columns=["symbol", *PREMIUM_MONTH_COLUMNS])
    rows = []
    for symbol, group in frame.groupby("symbol", dropna=True):
        summary = premium_summary(group)
        if not summary["trades"]:
            continue
        rows.append({"symbol": symbol, **{key: summary[key] for key in PREMIUM_MONTH_COLUMNS}})
    if not rows:
        return pd.DataFrame(columns=["symbol", *PREMIUM_MONTH_COLUMNS])
    return (
        pd.DataFrame(rows)
        .sort_values("premium_net", ascending=False)
        .reset_index(drop=True)
    )


def build_frame(records: list) -> pd.DataFrame:
    rows = []
    for record in records:
        row = {key: value for key, value in record.items() if not key.startswith("_")}
        flows = record["_flows"]
        beginning = row.get("beginning_value")
        ending = row.get("ending_value")

        stated_net = (row.get("deposits") or 0.0) + (row.get("withdrawals") or 0.0)
        parsed_net = sum(flow["amount"] for flow in flows)
        # Trust the Account Summary totals; use dated rows only for weighting.
        if flows and abs(parsed_net) > 0.005 and abs(parsed_net - stated_net) > 0.02:
            scale = stated_net / parsed_net
            flows = [dict(flow, amount=flow["amount"] * scale) for flow in flows]
        elif abs(stated_net) > 0.005 and not flows:
            midpoint = row["period_start"] + (row["period_end"] - row["period_start"]) / 2
            flows = [{"date": midpoint, "amount": stated_net, "description": "assumed mid-period"}]

        row["net_flow"] = stated_net
        row["flow_dates_known"] = bool(record["_flows"])
        dietz, gain = modified_dietz(
            beginning, ending, flows, row["period_start"], row["period_end"]
        )
        row["twr"] = dietz
        row["gain"] = gain
        row["simple_return"] = (
            (ending - beginning) / beginning if beginning and beginning > 0 else None
        )
        row["value_change"] = (
            ending - beginning if ending is not None and beginning is not None else None
        )

        # Reconcile derived gain against the statement's own components.
        components = [
            row.get("market_appreciation"),
            row.get("dividends_interest"),
            row.get("expenses"),
        ]
        if gain is not None and all(value is not None for value in components):
            row["reconciliation_residual"] = gain - sum(components)
        else:
            row["reconciliation_residual"] = None

        realized = row.get("st_net")
        long_term = row.get("lt_net")
        if realized is not None or long_term is not None:
            row["realized_net"] = (realized or 0.0) + (long_term or 0.0)
        else:
            row["realized_net"] = None
        rows.append(row)

    frame = pd.DataFrame(rows).sort_values("period_end").reset_index(drop=True)
    frame["month"] = pd.to_datetime(frame["period_end"]).dt.strftime("%Y-%m")

    # Records reloaded from Postgres arrive as Decimal, and a column that is
    # null for every statement lands as object dtype. Both break the arithmetic
    # below, so normalize regardless of where the records came from.
    for column in frame.columns:
        if column in ("file", "month", "period_start", "period_end", "flow_dates_known"):
            continue
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    returns = frame["twr"].fillna(0.0)
    frame["twr_index"] = 100.0 * (1.0 + returns).cumprod()
    frame["cumulative_twr"] = frame["twr_index"] / 100.0 - 1.0
    peak = frame["twr_index"].cummax().clip(lower=100.0)
    frame["drawdown"] = frame["twr_index"] / peak - 1.0

    for name in CLASS_COLUMNS:
        column = f"alloc_{name}"
        total = frame["alloc_total"].replace(0, np.nan)
        frame[f"weight_{name}"] = frame[column] / total
    return frame


def compute_metrics(frame: pd.DataFrame, risk_free: float) -> dict:
    returns = frame["twr"].dropna()
    months = len(returns)
    beginning = frame["beginning_value"].iloc[0]
    ending = frame["ending_value"].iloc[-1]
    cumulative = float((1.0 + returns).prod() - 1.0) if months else None

    metrics = {
        "months": int(months),
        "period_start": str(frame["period_start"].iloc[0]),
        "period_end": str(frame["period_end"].iloc[-1]),
        "beginning_value": float(beginning) if pd.notna(beginning) else None,
        "ending_value": float(ending) if pd.notna(ending) else None,
        "net_deposits": float(frame["net_flow"].sum()),
        "total_value_change": float(frame["value_change"].sum()),
        "investment_gain": float(frame["gain"].dropna().sum()),
        "cumulative_twr": cumulative,
        "annualized_twr": None,
        "annualization_valid": months >= 12,
        "volatility_annualized": None,
        "downside_deviation_annualized": None,
        "sharpe": None,
        "sortino": None,
        "max_drawdown": float(frame["drawdown"].min()) if months else None,
        "best_month": None,
        "worst_month": None,
        "hit_rate": None,
        "monthly_mwr": None,
        "annualized_mwr": None,
        "income_total": float(frame.get("income_total", pd.Series(dtype=float)).sum(min_count=1))
        if "income_total" in frame
        else None,
        "dividends_total": float(frame.get("dividends", pd.Series(dtype=float)).sum(min_count=1))
        if "dividends" in frame
        else None,
        "interest_total": float(frame.get("interest", pd.Series(dtype=float)).sum(min_count=1))
        if "interest" in frame
        else None,
        "expenses_total": float(frame["expenses"].sum(min_count=1))
        if "expenses" in frame
        else None,
        "realized_st": float(frame["st_net"].sum(min_count=1)) if "st_net" in frame else None,
        "realized_lt": float(frame["lt_net"].sum(min_count=1)) if "lt_net" in frame else None,
        "unrealized_latest": float(frame["unrealized"].iloc[-1])
        if "unrealized" in frame and pd.notna(frame["unrealized"].iloc[-1])
        else None,
        "max_reconciliation_residual": float(
            frame["reconciliation_residual"].abs().max()
        )
        if frame["reconciliation_residual"].notna().any()
        else None,
    }

    if cumulative is not None and months:
        metrics["annualized_twr"] = float((1.0 + cumulative) ** (12.0 / months) - 1.0)
        best = returns.idxmax()
        worst = returns.idxmin()
        metrics["best_month"] = [frame["month"].iloc[best], float(returns.loc[best])]
        metrics["worst_month"] = [frame["month"].iloc[worst], float(returns.loc[worst])]
        metrics["hit_rate"] = float((returns > 0).mean())

    if months >= 2:
        volatility = float(returns.std(ddof=1) * np.sqrt(12))
        metrics["volatility_annualized"] = volatility
        downside = returns[returns < 0]
        if len(downside) >= 2:
            deviation = float(downside.std(ddof=1) * np.sqrt(12))
            metrics["downside_deviation_annualized"] = deviation
            if deviation > 0 and metrics["annualized_twr"] is not None:
                metrics["sortino"] = (metrics["annualized_twr"] - risk_free) / deviation
        if volatility > 0 and metrics["annualized_twr"] is not None:
            metrics["sharpe"] = (metrics["annualized_twr"] - risk_free) / volatility

    flows = [-float(beginning)] if pd.notna(beginning) else [0.0]
    for _, row in frame.iterrows():
        flows.append(-float(row["net_flow"] or 0.0))
    flows[-1] += float(ending) if pd.notna(ending) else 0.0
    rate = irr_monthly(flows)
    if rate is not None:
        metrics["monthly_mwr"] = float(rate)
        metrics["annualized_mwr"] = float((1.0 + rate) ** 12 - 1.0)
    return metrics


def class_attribution(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-class value change and average weight.

    Statements do not disclose per-class cash flows, so this reconciles value
    change by class (which includes intra-class trading) rather than claiming a
    clean Brinson attribution.
    """
    rows = []
    for name in CLASS_COLUMNS:
        column = f"alloc_{name}"
        series = frame[column].dropna()
        if series.empty:
            continue
        change = float(series.iloc[-1] - series.iloc[0]) if len(series) > 1 else np.nan
        rows.append(
            {
                "asset_class": name,
                "start_value": float(series.iloc[0]),
                "end_value": float(series.iloc[-1]),
                "value_change": change,
                "avg_weight": float(frame[f"weight_{name}"].dropna().mean())
                if frame[f"weight_{name}"].notna().any()
                else np.nan,
                "end_weight": float(frame[f"weight_{name}"].dropna().iloc[-1])
                if frame[f"weight_{name}"].notna().any()
                else np.nan,
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------
def money_axis(axis):
    axis.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))


def percent_axis(axis):
    axis.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v * 100:.1f}%"))


def unique_labels(series) -> list:
    counts = {}
    out = []
    for label in series:
        counts[label] = counts.get(label, 0) + 1
        out.append(label if counts[label] == 1 else f"{label} #{counts[label]}")
    return out


def emit(fig, name: str, figures: list):
    fig.tight_layout()
    figures.append((name, fig))


def make_charts(frame: pd.DataFrame, holdings: pd.DataFrame, metrics: dict, directory: Path):
    """Write every chart to `directory` and return the filenames."""
    directory.mkdir(parents=True, exist_ok=True)
    names = []
    for name, fig in build_charts(frame, holdings, metrics):
        fig.savefig(directory / name, dpi=130)
        plt.close(fig)
        names.append(name)
    return names


def build_charts(frame: pd.DataFrame, holdings: pd.DataFrame, metrics: dict):
    """Build the chart set and return [(filename, figure)] without saving."""
    figures = []
    months = frame["month"].tolist()
    single = len(frame) == 1
    plt.rcParams.update({"axes.grid": True, "grid.alpha": 0.3, "figure.autolayout": False})

    # 1. account value and cost basis
    fig, axis = plt.subplots(figsize=(11, 5.5))
    axis.plot(months, frame["ending_value"], marker="o", lw=2, color="#1f4e79", label="Account value")
    if frame["cost_basis"].notna().any():
        axis.plot(
            months,
            frame["cost_basis"],
            marker="s",
            ls="--",
            lw=1.5,
            color="#7f7f7f",
            label="Cost basis (positions)",
        )
    contributions = frame[frame["net_flow"].abs() > 0.01]
    if not contributions.empty:
        axis.scatter(
            contributions["month"],
            contributions["ending_value"],
            s=90,
            color="#2ca02c",
            zorder=5,
            label="Month with net external flow",
        )
    axis.set_title("Account Value by Statement Period")
    axis.set_ylabel("Value")
    money_axis(axis)
    axis.legend(loc="best", fontsize=9)
    plt.setp(axis.get_xticklabels(), rotation=45, ha="right")
    emit(fig, "01_account_value.png", figures)

    # 2. monthly return bars
    fig, axis = plt.subplots(figsize=(11, 5.5))
    values = frame["twr"].fillna(0.0)
    colors = ["#2ca02c" if value >= 0 else "#c0392b" for value in values]
    axis.bar(months, values, color=colors, width=0.6)
    axis.axhline(0, color="black", lw=0.8)
    if len(frame) <= 18:
        for x, value in zip(months, values):
            axis.annotate(
                f"{value * 100:.2f}%",
                (x, value),
                textcoords="offset points",
                xytext=(0, 6 if value >= 0 else -14),
                ha="center",
                fontsize=9,
            )
    axis.set_title("Monthly Time-Weighted Return (Modified Dietz)")
    percent_axis(axis)
    plt.setp(axis.get_xticklabels(), rotation=45, ha="right")
    emit(fig, "02_monthly_returns.png", figures)

    # 3. cumulative index + drawdown
    if not single:
        fig, (top, bottom) = plt.subplots(
            2, 1, figsize=(11, 7.5), sharex=True, gridspec_kw={"height_ratios": [2, 1]}
        )
        top.plot(months, frame["twr_index"], marker="o", lw=2, color="#1f4e79")
        top.axhline(100, color="gray", ls="--", lw=1)
        top.set_title("Cumulative Time-Weighted Return Index (start = 100)")
        top.set_ylabel("Index")
        bottom.fill_between(months, frame["drawdown"], 0, color="#c0392b", alpha=0.4)
        bottom.set_title("Drawdown from Peak (return index)")
        percent_axis(bottom)
        plt.setp(bottom.get_xticklabels(), rotation=45, ha="right")
        emit(fig, "03_cumulative_and_drawdown.png", figures)

    # 4. allocation
    weight_columns = [f"weight_{name}" for name in CLASS_COLUMNS if frame[f"weight_{name}"].notna().any()]
    if weight_columns:
        fig, (left, right) = plt.subplots(1, 2, figsize=(13, 5.5))
        data = frame[weight_columns].fillna(0.0)
        if single:
            bottom = 0.0
            for column in weight_columns:
                value = float(data[column].iloc[0])
                left.bar(months, [value], bottom=[bottom], label=column.replace("weight_", ""))
                bottom += value
        else:
            left.stackplot(
                months,
                *[data[column] for column in weight_columns],
                labels=[column.replace("weight_", "") for column in weight_columns],
                alpha=0.85,
            )
        left.set_title("Asset Allocation Weights")
        percent_axis(left)
        left.legend(fontsize=8, loc="upper left")
        plt.setp(left.get_xticklabels(), rotation=45, ha="right")

        latest = frame.iloc[-1]
        labels, sizes = [], []
        for name in CLASS_COLUMNS:
            value = latest.get(f"alloc_{name}")
            if value is not None and pd.notna(value) and value > 0:
                labels.append(f"{name}\n${value:,.0f}")
                sizes.append(value)
        if sizes:
            right.pie(sizes, labels=labels, autopct="%1.1f%%", startangle=90, textprops={"fontsize": 8})
            right.set_title(f"Allocation of Long Assets — {latest['month']}")
        right.grid(False)
        emit(fig, "04_asset_allocation.png", figures)

    # 5. income
    if "income_total" in frame and frame["income_total"].notna().any():
        fig, axis = plt.subplots(figsize=(11, 5.5))
        dividends = frame.get("dividends", pd.Series(0.0, index=frame.index)).fillna(0.0)
        interest = frame.get("interest", pd.Series(0.0, index=frame.index)).fillna(0.0)
        axis.bar(months, dividends, color="#1f77b4", label="Dividends")
        axis.bar(months, interest, bottom=dividends, color="#ff7f0e", label="Interest")
        axis.set_title("Monthly Income")
        axis.set_ylabel("Income")
        money_axis(axis)
        twin = axis.twinx()
        twin.plot(
            months,
            (dividends + interest).cumsum(),
            color="#333333",
            marker="o",
            lw=1.5,
            label="Cumulative",
        )
        twin.grid(False)
        money_axis(twin)
        axis.legend(loc="upper left", fontsize=9)
        twin.legend(loc="upper right", fontsize=9)
        plt.setp(axis.get_xticklabels(), rotation=45, ha="right")
        emit(fig, "05_income.png", figures)

    # 6. realized vs unrealized
    if "st_net" in frame and frame["st_net"].notna().any():
        fig, axis = plt.subplots(figsize=(11, 5.5))
        short = frame["st_net"].fillna(0.0)
        long_term = frame.get("lt_net", pd.Series(0.0, index=frame.index)).fillna(0.0)
        width = 0.38
        positions = np.arange(len(frame))
        axis.bar(positions - width / 2, short, width, color="#8e44ad", label="Realized short-term")
        axis.bar(positions + width / 2, long_term, width, color="#16a085", label="Realized long-term")
        if "unrealized" in frame and frame["unrealized"].notna().any():
            axis.plot(
                positions,
                frame["unrealized"],
                marker="o",
                color="#d35400",
                lw=2,
                label="Unrealized (period end)",
            )
        axis.axhline(0, color="black", lw=0.8)
        axis.set_xticks(positions)
        axis.set_xticklabels(months, rotation=45, ha="right")
        axis.set_title("Realized Gains by Tax Character vs Unrealized Position")
        money_axis(axis)
        axis.legend(fontsize=9)
        emit(fig, "06_realized_unrealized.png", figures)

    # 7. value reconciliation waterfall
    steps = [
        ("Deposits", float(frame["deposits"].fillna(0.0).sum())),
        ("Withdrawals", float(frame["withdrawals"].fillna(0.0).sum())),
        ("Market appr.", float(frame["market_appreciation"].fillna(0.0).sum())),
        ("Income", float(frame["dividends_interest"].fillna(0.0).sum())),
        ("Expenses", float(frame["expenses"].fillna(0.0).sum())),
    ]
    start_value = float(frame["beginning_value"].iloc[0])
    fig, axis = plt.subplots(figsize=(11, 5.5))
    labels = ["Beginning"] + [label for label, _ in steps] + ["Ending"]
    running = start_value
    levels = [start_value]
    axis.bar(0, start_value, color="#1f4e79", width=0.6)
    for index, (_, amount) in enumerate(steps, start=1):
        color = "#2ca02c" if amount >= 0 else "#c0392b"
        axis.bar(index, amount, bottom=running, color=color, width=0.6)
        top = running + amount
        axis.annotate(
            f"{amount:+,.0f}",
            (index, max(running, top)),
            textcoords="offset points",
            xytext=(0, 5),
            ha="center",
            fontsize=9,
        )
        running = top
        levels.append(running)
    axis.bar(len(labels) - 1, running, color="#1f4e79", width=0.6)
    for position, value in ((0, start_value), (len(labels) - 1, running)):
        axis.annotate(
            f"${value:,.0f}",
            (position, value),
            textcoords="offset points",
            xytext=(0, 5),
            ha="center",
            fontsize=9,
            fontweight="bold",
        )
    low, high = min(levels), max(levels)
    span = max(high - low, high * 0.01)
    axis.set_ylim(low - span * 0.6, high + span * 0.9)
    axis.set_xticks(range(len(labels)))
    axis.set_xticklabels(labels, rotation=30, ha="right")
    axis.set_title("Value Reconciliation: Beginning to Ending (axis zoomed)")
    money_axis(axis)
    emit(fig, "07_value_reconciliation.png", figures)

    # 8. latest holdings P&L
    if not holdings.empty:
        latest_month = holdings["month"].max()
        current = holdings[holdings["month"] == latest_month].copy()
        current["chart_label"] = unique_labels(
            current["label"].fillna(current["symbol"]) + " [" + current["asset_class"].str[:3] + "]"
        )

        priced = current[current["unrealized"].notna()].sort_values("unrealized")
        if not priced.empty:
            positions = np.arange(len(priced))
            colors = ["#2ca02c" if value >= 0 else "#c0392b" for value in priced["unrealized"]]
            fig, axis = plt.subplots(figsize=(11, max(4.5, 0.42 * len(priced))))
            axis.barh(positions, priced["unrealized"], color=colors)
            axis.set_yticks(positions)
            axis.set_yticklabels(priced["chart_label"], fontsize=9)
            axis.axvline(0, color="black", lw=0.8)
            axis.set_title(f"Unrealized Gain/(Loss) by Position — {latest_month}")
            axis.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
            emit(fig, "08_holdings_pnl.png", figures)

        ordered = current.sort_values("market_value")
        positions = np.arange(len(ordered))
        colors = ["#1f77b4" if value >= 0 else "#e67e22" for value in ordered["market_value"]]
        fig, axis = plt.subplots(figsize=(11, max(4.5, 0.42 * len(ordered))))
        axis.barh(positions, ordered["market_value"], color=colors)
        axis.set_yticks(positions)
        axis.set_yticklabels(ordered["chart_label"], fontsize=9)
        axis.axvline(0, color="black", lw=0.8)
        axis.set_title(f"Market Value by Position (negative = short) — {latest_month}")
        axis.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
        emit(fig, "09_position_exposure.png", figures)

    return figures


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def fmt_money(value):
    return "n/a" if value is None or pd.isna(value) else f"${value:,.2f}"


def fmt_pct(value):
    return "n/a" if value is None or pd.isna(value) else f"{value * 100:.2f}%"


def fmt_ratio(value):
    return "n/a" if value is None or pd.isna(value) else f"{value:.2f}"


def build_report(frame: pd.DataFrame, metrics: dict, attribution: pd.DataFrame, risk_free: float,
                 transactions: pd.DataFrame = None):
    months = metrics["months"]
    lines = []
    add = lines.append
    add("=" * 78)
    add("PORTFOLIO PERFORMANCE REVIEW - Charles Schwab Brokerage Account")
    add(f"Period: {metrics['period_start']} to {metrics['period_end']}  ({months} statement month(s))")
    add("=" * 78)

    add("")
    add("1. HEADLINE RESULT")
    add("-" * 78)
    add(f"  Time-weighted return (cumulative) : {fmt_pct(metrics['cumulative_twr'])}")
    if metrics["annualization_valid"]:
        add(f"  Time-weighted return (annualized) : {fmt_pct(metrics['annualized_twr'])}")
    else:
        add(
            f"  Annualized TWR                    : {fmt_pct(metrics['annualized_twr'])}"
            f"  <- NOT MEANINGFUL, only {months} month(s) of data"
        )
    add(f"  Money-weighted return (annualized): {fmt_pct(metrics['annualized_mwr'])}")
    add(f"  Beginning value                   : {fmt_money(metrics['beginning_value'])}")
    add(f"  Ending value                      : {fmt_money(metrics['ending_value'])}")
    add(f"  Net external cash flow            : {fmt_money(metrics['net_deposits'])}")
    add(f"  Total value change                : {fmt_money(metrics['total_value_change'])}")
    add(f"  Investment gain (flow-adjusted)   : {fmt_money(metrics['investment_gain'])}")
    add("")
    add("  Method: TWR uses Modified Dietz with external flows day-weighted from the")
    add("  transaction detail. TWR measures investment decisions; MWR measures the")
    add("  investor's realized dollar experience including flow timing.")

    add("")
    add("2. RISK")
    add("-" * 78)
    if months >= 2:
        add(f"  Annualized volatility             : {fmt_pct(metrics['volatility_annualized'])}")
        add(f"  Annualized downside deviation     : {fmt_pct(metrics['downside_deviation_annualized'])}")
        add(f"  Sharpe ratio                      : {fmt_ratio(metrics['sharpe'])}"
            f"   (rf = {risk_free * 100:.2f}%)")
        add(f"  Sortino ratio                     : {fmt_ratio(metrics['sortino'])}")
        add(f"  Max drawdown (return index)       : {fmt_pct(metrics['max_drawdown'])}")
        add(f"  Positive months                   : {fmt_pct(metrics['hit_rate'])}")
        if metrics["best_month"]:
            add(f"  Best month                        : {metrics['best_month'][0]} "
                f"{fmt_pct(metrics['best_month'][1])}")
            add(f"  Worst month                       : {metrics['worst_month'][0]} "
                f"{fmt_pct(metrics['worst_month'][1])}")
        if months < 12:
            add(f"  CAVEAT: n = {months} monthly observations. These statistics are")
            add("  descriptive only and carry no statistical confidence.")
    else:
        add(f"  Only {months} statement period parsed. Volatility, Sharpe, Sortino and")
        add("  drawdown require at least two periods, and are not meaningful below 12.")
        add("  Add more monthly statement PDFs to build the track record.")

    add("")
    add("3. RETURN DECOMPOSITION")
    add("-" * 78)
    add(f"  Realized short-term gain/(loss)   : {fmt_money(metrics['realized_st'])}")
    add(f"  Realized long-term gain/(loss)    : {fmt_money(metrics['realized_lt'])}")
    add(f"  Unrealized at period end          : {fmt_money(metrics['unrealized_latest'])}")
    add(f"  Dividends received                : {fmt_money(metrics['dividends_total'])}")
    add(f"  Interest received                 : {fmt_money(metrics['interest_total'])}")
    add(f"  Expenses / margin interest        : {fmt_money(metrics['expenses_total'])}")
    if metrics["realized_st"] is not None and metrics["realized_lt"] is not None:
        realized = metrics["realized_st"] + metrics["realized_lt"]
        if abs(realized) > 0 and metrics["realized_lt"] == 0:
            add("  All realized gains are short-term, taxed as ordinary income.")

    add("")
    add("4. OPTION PREMIUM")
    add("-" * 78)
    if transactions is None or transactions.empty:
        add("  No transaction detail parsed.")
    else:
        premium = premium_summary(transactions)
        add(f"  Premium collected (short sales)    : {fmt_money(premium['premium_collected'])}")
        add(f"  Premium paid to close              : {fmt_money(premium['premium_paid_to_close'])}")
        add(f"  Net premium kept                   : {fmt_money(premium['premium_net'])}")
        add(f"  Long option purchases              : {fmt_money(premium['long_option_purchases'])}")
        add(f"  Contracts opened / closed          : {premium['contracts_opened']:,.0f} / {premium['contracts_closed']:,.0f}")
        add(f"  Commissions and fees               : {fmt_money(premium['charges'])}")
        add(f"  Realized on closing trades (ST/LT) : {fmt_money(premium['realized_closed_st'])}"
            f" / {fmt_money(premium['realized_closed_lt'])}")
        add("  Amounts are as printed, net of commission. A short option's credit is a")
        add("  liability until closed or expired, so premium collected is not income.")
        add("  Expirations print no realized gain, so the closing-trade figures above are")
        add("  smaller than section 3's realized total, which is the authoritative one.")

    add("")
    add("5. ASSET CLASS RECONCILIATION")
    add("-" * 78)
    if attribution.empty:
        add("  No asset allocation data parsed.")
    else:
        add(f"  {'Class':<14}{'Start':>14}{'End':>14}{'Change':>14}{'End wt':>10}")
        for _, row in attribution.iterrows():
            change = "n/a" if pd.isna(row["value_change"]) else f"{row['value_change']:,.0f}"
            weight = "n/a" if pd.isna(row["end_weight"]) else f"{row['end_weight'] * 100:.1f}%"
            add(
                f"  {row['asset_class']:<14}{row['start_value']:>14,.0f}"
                f"{row['end_value']:>14,.0f}{change:>14}{weight:>10}"
            )
        add("  Class change includes intra-class trading; statements do not disclose")
        add("  per-class cash flows, so this is a reconciliation, not a Brinson attribution.")

    latest = frame.iloc[-1]
    add("")
    add("6. EXPOSURE AND LEVERAGE AT PERIOD END")
    add("-" * 78)
    add(f"  Total account value               : {fmt_money(latest.get('alloc_total'))}")
    add(f"  Cash and cash investments         : {fmt_money(latest.get('alloc_Cash'))}")
    add(f"  Liabilities (shorts, margin)      : {fmt_money(latest.get('liabilities'))}")
    add(f"  Margin loan balance               : {fmt_money(latest.get('margin_closing'))}")
    add(f"  Securities buying power           : {fmt_money(latest.get('buying_power'))}")
    total = latest.get("alloc_total")
    cash = latest.get("alloc_Cash")
    if total and pd.notna(total) and cash is not None and pd.notna(cash):
        add(f"  Cash weight                       : {fmt_pct(cash / total)}")
    add("")
    add("  Risk notes:")
    add("  - Short option positions create obligations larger than their market value.")
    add("    Their premium is capped while assignment risk is not.")
    add("  - 2x/3x leveraged ETFs are path-dependent: they compound daily and decay in")
    add("    choppy markets. Do not extrapolate their returns linearly.")

    add("")
    add("7. DATA QUALITY")
    add("-" * 78)
    residual = metrics["max_reconciliation_residual"]
    if residual is None:
        add("  Could not cross-check derived gain against statement components.")
    elif residual < 0.02:
        add(f"  Derived gain ties to the statement's Market Appreciation + Income +")
        add(f"  Expenses lines in every period (max residual {fmt_money(residual)}).")
    else:
        add(f"  WARNING: derived gain differs from stated components by up to "
            f"{fmt_money(residual)}. Investigate before relying on these figures.")
    known = int(frame["flow_dates_known"].sum())
    add(f"  Periods with dated cash flows      : {known} of {len(frame)}")
    if known < len(frame):
        add("  Periods without dated flows use mid-period weighting for Modified Dietz.")
    add("  Cost basis may be incomplete per Schwab's own disclosure. Pending")
    add("  transactions are excluded from account value. Not a tax document.")

    add("")
    add("=" * 78)
    add("Analysis and education only. Not investment, tax, or legal advice.")
    add("Returns are unbenchmarked: no index data is used, so they are not")
    add("characterized as good or bad relative to any mandate.")
    add("=" * 78)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".", help="directory containing statement PDFs")
    parser.add_argument("--out", default="output", help="output directory")
    parser.add_argument("--rf", type=float, default=0.042, help="annual risk-free rate")
    parser.add_argument("--no-charts", action="store_true", help="skip chart generation")
    parser.add_argument("--verbose", action="store_true", help="report unparsed lines")
    parser.add_argument(
        "--save-db",
        action="store_true",
        help="also store each statement in Postgres (needs DATABASE_URL)",
    )
    args = parser.parse_args(argv)

    source = Path(args.dir).expanduser().resolve()
    pdfs = sorted(p for p in source.iterdir() if p.suffix.lower() == ".pdf")
    if not pdfs:
        print(f"No PDF statements found in {source}", file=sys.stderr)
        return 1

    print(f"Parsing {len(pdfs)} statement(s) from {source}")
    records = []
    parsed_paths = []
    for path in pdfs:
        try:
            record = StatementParser(path, verbose=args.verbose).parse()
        except Exception as error:  # a malformed statement must not kill the run
            print(f"  FAILED {path.name}: {error}", file=sys.stderr)
            continue
        records.append(record)
        parsed_paths.append(path)
        print(
            f"  {path.name}: {record['period_start']} to {record['period_end']}  "
            f"begin {fmt_money(record.get('beginning_value'))} -> "
            f"end {fmt_money(record.get('ending_value'))}  "
            f"({record['holdings_count']} positions)"
        )
    if not records:
        print("No statements parsed successfully.", file=sys.stderr)
        return 1

    seen = {}
    for record in records:
        key = record["period_end"]
        if key in seen:
            print(f"  WARNING duplicate period {key}: {seen[key]} and {record['file']}")
        seen[key] = record["file"]

    if args.save_db:
        import store  # imported lazily so the CLI runs without a database

        store.initialize()
        with store.connect() as conn:
            for path, record in zip(parsed_paths, records):
                digest = store.statement_digest(path.read_bytes())
                store.save_record(conn, record, digest)
                print(f"  stored {record['file']} ({digest[:12]})")

    frame = build_frame(records)
    metrics = compute_metrics(frame, args.rf)
    attribution = class_attribution(frame)

    holdings_rows = []
    for record in records:
        for holding in record["_holdings"]:
            holdings_rows.append(
                dict(
                    holding,
                    month=record["period_end"].strftime("%Y-%m"),
                    period_end=record["period_end"],
                )
            )
    holdings = pd.DataFrame(holdings_rows)

    flow_rows = []
    for record in records:
        for flow in record["_flows"]:
            flow_rows.append(
                {
                    "month": record["period_end"].strftime("%Y-%m"),
                    "date": flow["date"],
                    "amount": flow["amount"],
                    "description": flow["description"],
                }
            )
    flows = pd.DataFrame(flow_rows)
    transactions = transaction_frame(records)
    pending = transaction_frame(records, include_pending=True)
    pending = pending[~pending["settled"]] if not pending.empty else pending

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    export = frame.drop(columns=[c for c in frame.columns if c.startswith("_")])
    export.to_csv(out / "monthly_summary.csv", index=False)
    attribution.to_csv(out / "asset_class_reconciliation.csv", index=False)
    if not holdings.empty:
        holdings.to_csv(out / "holdings.csv", index=False)
    if not flows.empty:
        flows.to_csv(out / "cash_flows.csv", index=False)
    if not transactions.empty:
        transactions.to_csv(out / "transactions.csv", index=False)
        premium_by_month(transactions).to_csv(out / "premium_by_month.csv", index=False)
    if not pending.empty:
        pending.to_csv(out / "pending_transactions.csv", index=False)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))

    report = build_report(frame, metrics, attribution, args.rf, transactions)
    (out / "performance_report.txt").write_text(report + "\n")

    charts = []
    if not args.no_charts:
        charts = make_charts(frame, holdings, metrics, out / "charts")

    print()
    print(report)
    print()
    print(f"Data written to {out}")
    for name in ["monthly_summary.csv", "asset_class_reconciliation.csv", "metrics.json",
                 "performance_report.txt"]:
        if (out / name).exists():
            print(f"  {name}")
    if not holdings.empty:
        print("  holdings.csv")
    if not flows.empty:
        print("  cash_flows.csv")
    if not transactions.empty:
        print("  transactions.csv")
        print("  premium_by_month.csv")
    if not pending.empty:
        print("  pending_transactions.csv")
    for name in charts:
        print(f"  charts/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
