"""Outbound notification mail.

Subjects are short on purpose: a phone lock-screen push truncates hard, and the point
of these messages is to say what happened without opening them. Detail goes in the body.

The mail is multipart/alternative. The HTML part puts the trades in a table, which is
what a phone actually renders; the plain-text part is the same information as prose and
is what a preview pane and any client without HTML falls back to, so both parts have to
carry the whole story rather than one being a stub.

Silence is deliberate on a no-op run. Cron fires 288 times a day, so a "nothing new"
email every tick would train the notification to be muted, defeating the purpose.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from html import escape

from .domain import fmt_money, fmt_qty

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

CREDIT_COLOR = "#1a7f37"
DEBIT_COLOR = "#a40e26"
MUTED_COLOR = "#5c5c5c"
RULE = "1px solid #e0e0e0"

log = logging.getLogger(__name__)


def recipients() -> list[str]:
    raw = os.environ.get("GMAIL_TO") or os.environ.get("GMAIL_USER") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def send(subject: str, body: str, html: str | None = None) -> bool:
    """True when the mail was accepted. A notification failure never masks a result."""
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to = recipients()
    if not user or not password or not to:
        log.warning("notification skipped: GMAIL_USER, GMAIL_APP_PASSWORD or GMAIL_TO missing")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = ", ".join(to)
    message.set_content(body)
    if html:
        # Alternatives are ordered least to most preferred, so the text set above stays
        # the fallback and this becomes what a client renders when it can.
        message.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
    except Exception as exc:
        log.warning("notification to %s failed: %s", ", ".join(to), exc)
        return False
    return True


def signed(amount) -> str:
    if amount is None:
        return "n/a"
    return ("+" if amount >= 0 else "-") + fmt_money(abs(amount))


def _cash(amount) -> str:
    if amount is None:
        return "unknown cash"
    return ("credit " if amount >= 0 else "debit ") + fmt_money(abs(amount))


UNKNOWN_INTENT_NOTE = "the email did not say whether this opened or closed a position"


def _verb(trade: dict) -> str:
    """`Sold to open`, `Bought to close`, or the bare action when intent is unknown."""
    action = str(trade.get("action") or "").strip().lower()
    verb = "Sold" if action.startswith("sale") else "Bought" if action.startswith("purchase") \
        else str(trade.get("action") or "Traded")
    intent = trade.get("intent")
    return f"{verb} to {intent}" if intent in ("open", "close") else verb


def _count(trade: dict) -> str:
    quantity = trade.get("quantity")
    return fmt_qty(abs(quantity)) if quantity is not None else "?"


def _contract(trade: dict) -> str:
    """The instrument alone: `SOXL $70 put`, or the bare ticker for shares."""
    symbol = trade.get("symbol") or "?"
    if not trade.get("is_option"):
        return symbol
    kind = str(trade.get("option_type") or "option").lower()
    strike = trade.get("strike")
    return f"{symbol} ${strike:,g} {kind}" if strike is not None else f"{symbol} {kind}"


def describe(trade: dict) -> str:
    """One trade as a sentence. Field names mean nothing on a lock screen."""
    count = _count(trade)
    if trade.get("is_option"):
        instrument = f"{count} {_contract(trade)}"
        expiry = trade.get("expiry")
        if expiry is not None:
            instrument += f" expiring {expiry:%b %d}"
    else:
        instrument = f"{count} {_contract(trade)} share{'' if count == '1' else 's'}"

    price = trade.get("price")
    at = f" at {fmt_money(price)}" if price is not None else ""
    line = f"{_verb(trade)} {instrument}{at} - {_cash(trade.get('net_amount'))}"
    if trade.get("intent") not in ("open", "close"):
        line += f" ({UNKNOWN_INTENT_NOTE})"
    return line


def subject_for(outcome: dict) -> str | None:
    """The short line. None means send nothing."""
    status = outcome.get("status")
    if status == "failed":
        return f"Schwab: INGEST FAILED - {outcome.get('reason', 'unknown')}"
    if status == "nothing":
        return None
    stored = outcome.get("trades_stored", 0)
    failed = outcome.get("blocks_failed", 0)
    if failed:
        return f"Schwab: {stored} of {stored + failed} trades - {failed} failed"
    plural = "trade" if stored == 1 else "trades"
    net = outcome.get("net_amount")
    tail = "net" if net is None else ("credit" if net >= 0 else "debit")
    return f"Schwab: {stored} {plural}, {signed(net)} {tail}"


def body_for(outcome: dict) -> str:
    lines = []
    if outcome.get("status") == "failed":
        lines.append("The confirmation ingestion did not finish, so nothing was recorded.")
        lines.append("")
        lines.append(f"What went wrong: {outcome.get('reason', 'unknown')}")
        if outcome.get("error"):
            lines.append("")
            lines.append("Details from the failure:")
            lines.append(f"  {outcome['error']}")
        lines.append("")
        lines.append("The next run will try again. If it keeps failing, check the Gmail app")
        lines.append("password and that the database is reachable.")
        return "\n".join(lines)

    trades = outcome.get("trades", [])
    dates = sorted({trade["trade_date"] for trade in trades if trade.get("trade_date")})
    when = ""
    if len(dates) == 1:
        when = f" from {dates[0]:%d %b %Y}"
    elif dates:
        when = f" from {dates[0]:%d %b} to {dates[-1]:%d %b %Y}"
    count = len(trades)
    lines.append(f"{count} trade{'' if count == 1 else 's'}{when} "
                 f"{'was' if count == 1 else 'were'} added to the ledger.")
    lines.append("")
    for trade in trades:
        lines.append(f"  {describe(trade)}")
    if trades:
        lines.append("")
    net = outcome.get("net_amount")
    kind = "" if net is None else f" ({'credit' if net >= 0 else 'debit'})"
    lines.append(f"Cash from these trades: {signed(net)}{kind}")

    skipped = outcome.get("emails_skipped") or 0
    if skipped:
        one = skipped == 1
        lines.append(f"{skipped} confirmation email{'' if one else 's'} had already been "
                     f"recorded and {'was' if one else 'were'} left alone.")

    failed = outcome.get("blocks_failed") or 0
    if failed:
        lines.append("")
        lines.append(f"{failed} trade{'' if failed == 1 else 's'} could not be read and "
                     f"{'was' if failed == 1 else 'were'} not stored, so the ledger is "
                     "incomplete until they are looked at:")
        for reason in outcome.get("failures", []):
            lines.append(f"  - {reason}")

    if outcome.get("positions") is not None or outcome.get("quotes_ok") is not None:
        lines.append("")
        lines.append("Where the account stands now")
        if outcome.get("positions") is not None:
            lines.append(f"  Open positions: {outcome['positions']}")
        if outcome.get("quotes_ok") is not None:
            lines.append(f"  Live prices refreshed: {outcome['quotes_ok']} of "
                         f"{outcome.get('quotes_total', 0)}")

    if outcome.get("warnings"):
        lines.append("")
        lines.append("Worth a look")
        for warning in outcome["warnings"]:
            lines.append(f"  - {warning}")

    lines.append("")
    lines.append("Positions and prices here come from the confirmation emails. Those carry no")
    lines.append("cash balance, dividend or expiration information, so account value, return")
    lines.append("and risk figures still come from the monthly statement.")
    return "\n".join(lines)


def _paragraph(content: str, color: str | None = None) -> str:
    style = "margin:0 0 12px;line-height:1.45;"
    if color:
        style += f"color:{color};"
    return f'<p style="{style}">{content}</p>'


def _heading(text: str) -> str:
    return (f'<div style="margin:24px 0 8px;font-size:12px;font-weight:600;'
            f'letter-spacing:.06em;text-transform:uppercase;color:{MUTED_COLOR};">'
            f"{escape(text)}</div>")


def _bullets(items) -> str:
    cells = "".join(f'<li style="margin:0 0 5px;">{escape(str(item))}</li>' for item in items)
    return f'<ul style="margin:0;padding-left:20px;line-height:1.45;">{cells}</ul>'


def _cash_color(amount) -> str:
    """Green for a credit, red for a debit, grey when the email printed no amount."""
    if amount is None:
        return MUTED_COLOR
    return CREDIT_COLOR if amount >= 0 else DEBIT_COLOR


def _cell(content: str, align: str = "left", color: str | None = None) -> str:
    style = f"padding:8px 10px;border-bottom:{RULE};white-space:nowrap;"
    if color:
        style += f"color:{color};"
    return f'<td align="{align}" style="{style}">{content}</td>'


def _trade_table(trades: list[dict]) -> str:
    columns = (("Action", "left"), ("Contract", "left"), ("Expiry", "left"),
               ("Qty", "right"), ("Price", "right"), ("Cash", "right"))
    head = "".join(
        f'<th align="{align}" style="padding:0 10px 6px;border-bottom:2px solid #999;'
        f'font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;'
        f'color:{MUTED_COLOR};white-space:nowrap;">{escape(label)}</th>'
        for label, align in columns
    )

    rows = []
    for trade in trades:
        net = trade.get("net_amount")
        price = trade.get("price")
        expiry = trade.get("expiry")
        action = escape(_verb(trade))
        if trade.get("intent") not in ("open", "close"):
            action += f' <span style="color:{MUTED_COLOR};">*</span>'
        rows.append(
            "<tr>"
            + _cell(action)
            + _cell(escape(_contract(trade)))
            + _cell(f"{expiry:%d %b %Y}" if expiry is not None else "&mdash;")
            + _cell(escape(_count(trade)), align="right")
            + _cell(escape(fmt_money(price)) if price is not None else "n/a", align="right")
            + _cell(escape(signed(net)), align="right", color=_cash_color(net))
            + "</tr>"
        )

    return ('<table cellpadding="0" cellspacing="0" border="0" role="presentation" '
            'style="border-collapse:collapse;width:100%;font-size:14px;">'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>")


def _stat_table(pairs: list[tuple[str, str]]) -> str:
    rows = "".join(
        f'<tr><td style="padding:3px 16px 3px 0;color:{MUTED_COLOR};">{escape(label)}</td>'
        f'<td style="padding:3px 0;font-weight:600;">{escape(value)}</td></tr>'
        for label, value in pairs
    )
    return ('<table cellpadding="0" cellspacing="0" border="0" role="presentation" '
            f'style="border-collapse:collapse;font-size:14px;">{rows}</table>')


def _page(blocks: list[str]) -> str:
    return ('<html><body style="margin:0;padding:18px;background:#ffffff;color:#1a1a1a;'
            'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,'
            'sans-serif;font-size:15px;">'
            f"{''.join(blocks)}</body></html>")


def html_for(outcome: dict) -> str:
    """The same story as `body_for`, with the trades as a table."""
    if outcome.get("status") == "failed":
        blocks = [
            _paragraph("The confirmation ingestion did not finish, so "
                       "<strong>nothing was recorded</strong>."),
            _stat_table([("What went wrong", str(outcome.get("reason", "unknown")))]),
        ]
        if outcome.get("error"):
            blocks.append(_heading("Details from the failure"))
            blocks.append('<pre style="margin:0;padding:10px;background:#f6f6f6;'
                          f'border-left:3px solid {DEBIT_COLOR};font-size:13px;'
                          'white-space:pre-wrap;">'
                          f"{escape(str(outcome['error']))}</pre>")
        blocks.append(_paragraph(
            "The next run will try again. If it keeps failing, check the Gmail app "
            "password and that the database is reachable.", color=MUTED_COLOR))
        return _page(blocks)

    trades = outcome.get("trades", [])
    dates = sorted({trade["trade_date"] for trade in trades if trade.get("trade_date")})
    when = ""
    if len(dates) == 1:
        when = f" from {dates[0]:%d %b %Y}"
    elif dates:
        when = f" from {dates[0]:%d %b} to {dates[-1]:%d %b %Y}"
    count = len(trades)
    blocks = [_paragraph(
        f"<strong>{count} trade{'' if count == 1 else 's'}</strong>{escape(when)} "
        f"{'was' if count == 1 else 'were'} added to the ledger.")]

    if trades:
        blocks.append(_trade_table(trades))
        if any(trade.get("intent") not in ("open", "close") for trade in trades):
            blocks.append(_paragraph(f"* {escape(UNKNOWN_INTENT_NOTE)}.", color=MUTED_COLOR))

    net = outcome.get("net_amount")
    blocks.append(_heading("Cash from these trades"))
    blocks.append(_paragraph(
        f'<strong style="font-size:17px;color:{_cash_color(net)};">'
        f"{escape(signed(net))}</strong>"
        + ("" if net is None else escape(f" ({'credit' if net >= 0 else 'debit'})"))))

    skipped = outcome.get("emails_skipped") or 0
    if skipped:
        one = skipped == 1
        blocks.append(_paragraph(
            f"{skipped} confirmation email{'' if one else 's'} had already been recorded "
            f"and {'was' if one else 'were'} left alone.", color=MUTED_COLOR))

    failed = outcome.get("blocks_failed") or 0
    if failed:
        blocks.append(_heading("Not stored"))
        blocks.append(_paragraph(
            f"{failed} trade{'' if failed == 1 else 's'} could not be read, so the ledger "
            "is incomplete until they are looked at:", color=DEBIT_COLOR))
        blocks.append(_bullets(outcome.get("failures", [])))

    stats = []
    if outcome.get("positions") is not None:
        stats.append(("Open positions", str(outcome["positions"])))
    if outcome.get("quotes_ok") is not None:
        stats.append(("Live prices refreshed",
                      f"{outcome['quotes_ok']} of {outcome.get('quotes_total', 0)}"))
    if stats:
        blocks.append(_heading("Where the account stands now"))
        blocks.append(_stat_table(stats))

    if outcome.get("warnings"):
        blocks.append(_heading("Worth a look"))
        blocks.append(_bullets(outcome["warnings"]))

    blocks.append(f'<hr style="margin:24px 0 12px;border:0;border-top:{RULE};">')
    blocks.append(_paragraph(
        "Positions and prices here come from the confirmation emails. Those carry no cash "
        "balance, dividend or expiration information, so account value, return and risk "
        "figures still come from the monthly statement.", color=MUTED_COLOR))
    return _page(blocks)


def announce(outcome: dict) -> bool:
    subject = subject_for(outcome)
    if subject is None:
        return False
    return send(subject, body_for(outcome), html_for(outcome))
