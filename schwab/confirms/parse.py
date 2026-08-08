"""Parse a Schwab eConfirms email body into trade rows.

Pure text in, dicts out: no network, no database, so it can be tested against a
saved message. The trades this yields are the primary record of what was traded;
the monthly statement later audits them.

The shape of one trade block, as Schwab prints it in the text/plain part:

      Symbol:
    SOXL 08/28/2026 104.00 P
    Security Description: DRXN SEMICN BULL 3X 08/28/2026 $104 Put
    Action: Sale
    Security No./CUSIP: 000144241668
    Type: Short
    Trade Date: 08/06/26
    Settle Date: 08/07/26
    Quantity Price Principal Charge and/or Interest Total Amount
    1 $8.00 $800.00
    Commission: $0.65
    Industry Fee: $0.03
    Total: $0.68 $799.32
    Additional information for this security:
    - We will hold this new option position short in your account (sold to open).

A confirmation too long for one email is split, and the continuation part prints the
same fields one table cell per line, dates the header `20260807` rather than
`08/07/2026`, carries no account line, and repeats the `Symbol:` header to print each
trade's Additional-information notes as a block of its own, after the table blocks:

      Symbol:
    SOXL 08/21/2026 70.00 P
    Security Description:
    DRXN SEMICN BULL 3X 08/21/2026 $70 Put
    Action:
    Purchase
    ...
    Quantity Price Principal Charge and/or Interest Total Amount
    1.00000
    $0.600000000
    $60.00
      Symbol:
    SOXL 08/21/2026 70.00 P
    Additional information for this security:
    - Bought to close your short option position.

Label patterns therefore have to tolerate a newline between a label and its value, and
the intent has to be merged back into the trade it describes.
"""

from __future__ import annotations

import re
from datetime import date

from ..domain import DEBIT_ACTIONS, OPTION_MULTIPLIER, occ_symbol

SUBJECT_HINT = "Schwab eConfirms"

ACCOUNT_RE = re.compile(r"Account ending:\s*(\d{3,4})")
CONFIRM_DATE_RE = re.compile(
    r"trade confirmation\(s\)\s+for\s+(\d{2}/\d{2}/\d{4}|\d{8})"
)
BLOCK_SPLIT_RE = re.compile(r"^\s*Symbol:\s*$", re.MULTILINE)

FIELD_RE = {
    "description": re.compile(r"^Security Description:\s*(.+)$", re.MULTILINE),
    "action": re.compile(r"^Action:\s*(\S+)", re.MULTILINE),
    "cusip": re.compile(r"^Security No\./CUSIP:\s*(\S+)", re.MULTILINE),
    "trade_date": re.compile(r"^Trade Date:\s*(\d{2}/\d{2}/\d{2,4})", re.MULTILINE),
    "settle_date": re.compile(r"^Settle Date:\s*(\d{2}/\d{2}/\d{2,4})", re.MULTILINE),
    "commission": re.compile(r"^Commission:\s*\$?([\d,]+\.\d{2})", re.MULTILINE),
    "industry_fee": re.compile(r"^Industry Fee:\s*\$?([\d,]+\.\d{2})", re.MULTILINE),
}

# "Total: $0.68 $799.32" - charges then the net amount credited or debited.
TOTAL_RE = re.compile(r"^Total:\s*\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})", re.MULTILINE)

# "1 $8.00 $800.00" - quantity, per-share price, principal, following the column
# header. Anchoring on the header is what makes the pattern safe to let whitespace span
# newlines, which a continuation email needs since it prints one cell per line;
# unanchored, the Commission and Total rows below are the same shape. Quantity may be
# fractional for equities and a continuation pads the price to nine decimal places.
AMOUNTS_RE = re.compile(
    r"Total\s*Amount\s+([\d,]+(?:\.\d+)?)\s+\$([\d,]+\.\d+)\s+\$([\d,]+\.\d{2})"
)

# "SOXL 08/28/2026 104.00 P" on the line under the Symbol: marker.
OPTION_SYMBOL_RE = re.compile(
    r"^([A-Z][A-Z0-9.\-/]{0,14})\s+(\d{2}/\d{2}/\d{4})\s+([\d,]+\.\d{1,4})\s+([CP])\s*$"
)
PLAIN_SYMBOL_RE = re.compile(r"^([A-Z][A-Z0-9.\-/]{0,14})\s*$")

OPEN_HINTS = ("sold to open", "bought to open", "new option position")
CLOSE_HINTS = ("bought to close", "sold to close", "closing transaction")


def strip_urls(text: str) -> str:
    """Remove the tracking links.

    They carry a per-send `qs=` token, so leaving them in would defeat body
    deduplication, and their digits could be read as a number by a tail pattern -
    the same hazard the statement parser guards against on continuation lines.
    """
    return re.sub(r"<?https?://\S+>?", "", text or "")


def to_amount(token: str):
    if token is None:
        return None
    try:
        return float(token.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def to_date(token: str, reference: date = None):
    if not token:
        return None
    if len(token) == 8 and token.isdigit():
        # A continuation email's header dates itself 20260807.
        try:
            return date(int(token[:4]), int(token[4:6]), int(token[6:]))
        except ValueError:
            return None
    parts = token.split("/")
    if len(parts) != 3:
        return None
    try:
        month, day, year = (int(part) for part in parts)
    except ValueError:
        return None
    if year < 100:
        # Expand against the confirm date, not today: a confirm re-read years later
        # must still resolve to the year it was sent.
        century = (reference.year // 100 * 100) if reference else 2000
        year += century
    try:
        return date(year, month, day)
    except ValueError:
        return None


def field(pattern: re.Pattern, block: str):
    match = pattern.search(block)
    return match.group(1).strip() if match else None


def read_intent(block: str):
    """open / close / None, from the Additional-information note.

    `Type: Short` prints on both the sale and the purchase side of a short option, so
    it says nothing about whether a position was opened or closed. When the note is
    absent the answer is unknown, and None is recorded rather than guessed - guessing
    inverts a position.
    """
    lowered = block.lower()
    for hint in CLOSE_HINTS:
        if hint in lowered:
            return "close"
    for hint in OPEN_HINTS:
        if hint in lowered:
            return "open"
    return None


def parse_symbol_line(block: str):
    """The line under the Symbol: marker: an OCC-style option line or a bare ticker."""
    for line in block.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        option = OPTION_SYMBOL_RE.match(candidate)
        if option:
            return {
                "symbol": option.group(1),
                "expiry_text": option.group(2),
                "strike": to_amount(option.group(3)),
                "option_type": "PUT" if option.group(4) == "P" else "CALL",
                "is_option": True,
            }
        plain = PLAIN_SYMBOL_RE.match(candidate)
        if plain:
            return {"symbol": plain.group(1), "is_option": False}
        return None
    return None


def parse_block(block: str, seq: int, header: dict) -> tuple[dict | None, str | None]:
    identity = parse_symbol_line(block)
    if not identity:
        return None, "no symbol line"

    action = field(FIELD_RE["action"], block)
    if not action:
        return None, f"{identity['symbol']}: no Action line"

    amounts = AMOUNTS_RE.search(block)
    totals = TOTAL_RE.search(block)
    if not amounts:
        return None, f"{identity['symbol']}: no quantity/price/principal line"

    quantity = to_amount(amounts.group(1))
    price = to_amount(amounts.group(2))
    principal = to_amount(amounts.group(3))
    charges = to_amount(totals.group(1)) if totals else None
    printed_total = to_amount(totals.group(2)) if totals else None

    is_option = identity["is_option"]
    confirm_date = header.get("confirm_date")
    expiry = to_date(identity.get("expiry_text"), confirm_date) if is_option else None
    strike = identity.get("strike")

    debit = action in DEBIT_ACTIONS
    # Sign comes from Action, independent of intent: a Sale credits the account and a
    # Purchase debits it whether or not it opened the position.
    net_amount = None
    if printed_total is not None:
        net_amount = -printed_total if debit else printed_total

    trade = {
        "seq": seq,
        "symbol": identity["symbol"],
        "description": field(FIELD_RE["description"], block),
        "action": action,
        "intent": read_intent(block),
        "cusip": field(FIELD_RE["cusip"], block),
        "trade_date": to_date(field(FIELD_RE["trade_date"], block), confirm_date),
        "settle_date": to_date(field(FIELD_RE["settle_date"], block), confirm_date),
        "quantity": quantity,
        "price": price,
        "principal": principal,
        "commission": to_amount(field(FIELD_RE["commission"], block)),
        "industry_fee": to_amount(field(FIELD_RE["industry_fee"], block)),
        "charges": charges,
        "net_amount": net_amount,
        "is_option": is_option,
        "option_type": identity.get("option_type"),
        "strike": strike,
        "expiry": expiry,
        "account_tail": header.get("account_tail"),
    }
    trade["occ_symbol"] = (
        occ_symbol(trade["symbol"], expiry, trade["option_type"], strike) if is_option else None
    )

    problem = check_arithmetic(trade)
    if problem:
        return None, f"{identity['symbol']}: {problem}"
    return trade, None


def merge_note(trades: list[dict], block: str) -> bool:
    """Attach a notes-only block to the trade it describes. True when consumed.

    A continuation email prints the trade tables first and then repeats `Symbol:` once
    per trade to carry the Additional-information notes, so the intent arrives in a
    block of its own with no Action line and no amounts row. Matching is required rather
    than assumed: a genuinely mis-parsed trade block has the same shape, and returning
    False leaves it to be reported as a failure instead of silently dropped.
    """
    if FIELD_RE["action"].search(block) or AMOUNTS_RE.search(block):
        return False
    intent = read_intent(block)
    identity = parse_symbol_line(block)
    if intent is None or not identity:
        return False

    cusip = field(FIELD_RE["cusip"], block)
    for trade in trades:  # print order, so repeated contracts pair up in order
        if trade["symbol"] != identity["symbol"]:
            continue
        if trade["strike"] != identity.get("strike"):
            continue
        if trade["option_type"] != identity.get("option_type"):
            continue
        if cusip and trade["cusip"] and cusip != trade["cusip"]:
            continue
        if trade["intent"] is None:
            trade["intent"] = intent
            return True
        if trade["intent"] == intent:
            return True
    return False


def check_arithmetic(trade: dict) -> str | None:
    """Reject a block whose printed numbers do not agree.

    A silently mis-parsed price or quantity would corrupt premium totals, so a block
    that fails is reported rather than stored.
    """
    quantity, price, principal = trade["quantity"], trade["price"], trade["principal"]
    if quantity is None or price is None or principal is None:
        return "missing quantity, price or principal"
    multiplier = OPTION_MULTIPLIER if trade["is_option"] else 1
    expected = quantity * price * multiplier
    if abs(expected - principal) > 0.02:
        return f"principal {principal:.2f} != qty x price x {multiplier} ({expected:.2f})"

    charges = trade.get("charges")
    if charges is not None:
        commission = trade.get("commission") or 0.0
        fee = trade.get("industry_fee") or 0.0
        if abs((commission + fee) - charges) > 0.02:
            return f"charges {charges:.2f} != commission + fee ({commission + fee:.2f})"

    net = trade.get("net_amount")
    if net is not None and charges is not None:
        # A credit nets down by its charges, a debit nets up by them.
        expected_net = (principal - charges) if net >= 0 else -(principal + charges)
        if abs(expected_net - net) > 0.02:
            return f"net {net:.2f} != principal +/- charges ({expected_net:.2f})"
    return None


def parse_header(text: str) -> dict:
    account = ACCOUNT_RE.search(text)
    confirm = CONFIRM_DATE_RE.search(text)
    return {
        "account_tail": account.group(1) if account else None,
        "confirm_date": to_date(confirm.group(1)) if confirm else None,
    }


def parse_confirm(text: str) -> dict:
    """Parse a whole eConfirms body.

    Returns the header fields, the parsed trades and any block that failed, so a
    partial parse can be stored and reported instead of discarded.
    """
    clean = strip_urls(text or "")
    header = parse_header(clean)
    blocks = BLOCK_SPLIT_RE.split(clean)

    trades, failed = [], []
    for block in blocks[1:]:  # blocks[0] is the message header, before the first Symbol:
        seq = len(trades) + len(failed) + 1
        trade, error = parse_block(block, seq, header)
        if trade:
            trades.append(trade)
        elif merge_note(trades, block):
            continue
        elif error:
            failed.append({"seq": seq, "error": error})

    return {
        "account_tail": header["account_tail"],
        "confirm_date": header["confirm_date"],
        "trades": trades,
        "failed": failed,
        "net_amount": round(sum(t["net_amount"] or 0.0 for t in trades), 2),
    }
