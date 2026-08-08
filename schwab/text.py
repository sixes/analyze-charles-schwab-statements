"""Token-level helpers shared by the statement PDF parser and the confirm parser.

Schwab's extraction glues intra-label spaces away and appends footnote letters, so
these are deliberately conservative: a token is a number only when the whole token
is numeric.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

MONTH_NAMES = list(calendar.month_name)[1:]

# A token counts as a number only if the entire token is numeric. Substring
# matching corrupts glued PDF artifacts (07/24/202649.50EXP07/24/26) and would
# silently swallow percent columns (46.40%).
TOKEN_NUM_RE = re.compile(r"^\(?\$?-?[\d,]+\.\d{2,5}\)?,?[A-Za-z]?$")


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
