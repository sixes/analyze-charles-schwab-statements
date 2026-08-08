"""Outbound notification mail.

Subjects are short on purpose: a phone lock-screen push truncates hard, and the point
of these messages is to say what happened without opening them. Detail goes in the body.

Silence is deliberate on a no-op run. Cron fires 288 times a day, so a "nothing new"
email every tick would train the notification to be muted, defeating the purpose.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from .domain import fmt_money, fmt_qty

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

log = logging.getLogger(__name__)


def recipients() -> list[str]:
    raw = os.environ.get("GMAIL_TO") or os.environ.get("GMAIL_USER") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def send(subject: str, body: str) -> bool:
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


def describe(trade: dict) -> str:
    """One trade as a sentence. Field names mean nothing on a lock screen."""
    action = str(trade.get("action") or "").strip().lower()
    intent = trade.get("intent")
    verb = "Sold" if action.startswith("sale") else "Bought" if action.startswith("purchase") \
        else str(trade.get("action") or "Traded")
    if intent in ("open", "close"):
        verb = f"{verb} to {intent}"

    quantity = trade.get("quantity")
    count = fmt_qty(abs(quantity)) if quantity is not None else "?"
    symbol = trade.get("symbol") or "?"
    if trade.get("is_option"):
        kind = str(trade.get("option_type") or "option").lower()
        strike = trade.get("strike")
        instrument = f"{count} {symbol} ${strike:,g} {kind}" if strike is not None \
            else f"{count} {symbol} {kind}"
        expiry = trade.get("expiry")
        if expiry is not None:
            instrument += f" expiring {expiry:%b %d}"
    else:
        instrument = f"{count} {symbol} share{'' if count == '1' else 's'}"

    price = trade.get("price")
    at = f" at {fmt_money(price)}" if price is not None else ""
    line = f"{verb} {instrument}{at} - {_cash(trade.get('net_amount'))}"
    if intent not in ("open", "close"):
        line += " (the email did not say whether this opened or closed a position)"
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


def announce(outcome: dict) -> bool:
    subject = subject_for(outcome)
    if subject is None:
        return False
    return send(subject, body_for(outcome))
