"""Outbound notification mail.

Subjects are short on purpose: a phone lock-screen push truncates hard, and the point
of these messages is to say what happened without opening them. Detail goes in the body.

The mail is multipart/alternative. The HTML part puts the trades in a table, which is
what a phone actually renders; the plain-text part carries the same comparison rows and
is what a preview pane and any client without HTML falls back to, so both parts have to
carry the whole story rather than one being a stub.

Silence is deliberate on a no-op run. Cron fires 288 times a day, so a "nothing new"
email every tick would train the notification to be muted, defeating the purpose.
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from html import escape
from math import isfinite

from .domain import NETWORK_ATTEMPTS, fmt_money, fmt_qty, retry

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

    def deliver() -> None:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)

    try:
        retry(deliver, on_retry=lambda attempt, exc: log.warning(
            "notification attempt %s of %s failed: %s; retrying",
            attempt, NETWORK_ATTEMPTS, exc))
    except Exception as exc:
        log.warning("notification to %s failed after %s attempts: %s",
                    ", ".join(to), NETWORK_ATTEMPTS, exc)
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
        when = f" from {_roll_date(dates[0])}"
    elif dates:
        when = f" from {_roll_date(dates[0])} to {_roll_date(dates[-1])}"
    count = len(trades)
    lines.append(f"{count} trade{'' if count == 1 else 's'}{when} "
                 f"{'was' if count == 1 else 'were'} added to the ledger.")
    lines.append("")
    for trade in trades:
        rows, _ = _econfirm_rows(trade)
        lines.extend([_roll_text_table(ECONFIRM_COLUMNS, rows), ""])
    if trades:
        if any(trade.get("intent") not in ("open", "close") for trade in trades):
            lines.append(f"* {UNKNOWN_INTENT_NOTE}.")
        lines.extend([ECONFIRM_NOTE, ""])
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


ECONFIRM_COLUMNS = ("Contract", "Size", "Close / Open", "Price ($/share)",
                    "Cash ($)", "Trade date", "Expiry")
ECONFIRM_NOTE = (
    "Original is historical context reconstructed from available confirmations; its full "
    "size and cash may differ from Current. Totals count Current rows only. "
    "Dates: MM-DD-YY; - = not applicable; n/a = unavailable."
)


def _econfirm_rows(trade: dict) -> tuple[list[tuple[str, ...]], dict]:
    context = trade.get("original_context") or {}
    original = context.get("original") if context.get("status") == "verified" else None
    rows, colors = [], {}
    for index, (label, fill) in enumerate((("Original", original), ("Current", trade))):
        if fill is None:
            absent = context.get("status") == "none"
            marker = "-" if absent else "n/a"
            reason = "No prior position" if absent else context.get("reason") or "Original history unavailable"
            rows.append((f"{label} · {_econfirm_contract(trade)}\n{reason}", *(marker,) * 6))
            continue
        price = fill.get("price")
        net = fill.get("net_amount")
        price = price if price is not None and isfinite(price) else None
        net = net if net is not None and isfinite(net) else None
        intent = fill.get("intent")
        action = _verb(fill) + (" *" if intent not in ("open", "close") else "")
        rows.append((
            f"{label} · {_econfirm_contract(fill)}", _count(fill), action,
            f"{price:,.2f}" if price is not None else "n/a", signed(net),
            _roll_date(fill.get("trade_date")),
            _roll_date(fill.get("expiry")) if fill.get("is_option") else "-",
        ))
        colors[index, 2] = ROLL_OPEN_COLOR if intent == "open" else ROLL_CLOSE_COLOR if intent == "close" else MUTED_COLOR
        colors[index, 4] = _cash_color(net)
    return rows, colors


def _econfirm_contract(trade: dict) -> str:
    if not trade.get("is_option") or trade.get("strike") is None:
        return _contract(trade)
    strike = Decimal(str(trade["strike"]))
    digits = max(2, -strike.as_tuple().exponent)
    return f"{trade.get('symbol') or '?'} ${strike:,.{digits}f} {str(trade.get('option_type') or 'option').lower()}"


def _trade_table(trades: list[dict]) -> str:
    tables = []
    for trade in trades:
        rows, colors = _econfirm_rows(trade)
        tables.append(_roll_html_table(ECONFIRM_COLUMNS, rows, comparison=True, cell_colors=colors))
    return '<div style="height:14px;"></div>'.join(tables)


def _stat_table(pairs: list[tuple[str, str]]) -> str:
    rows = "".join(
        f'<tr><td style="padding:3px 16px 3px 0;color:{MUTED_COLOR};">{escape(label)}</td>'
        f'<td style="padding:3px 0;font-weight:600;">{escape(value)}</td></tr>'
        for label, value in pairs
    )
    return ('<table cellpadding="0" cellspacing="0" border="0" role="presentation" '
            f'style="border-collapse:collapse;font-size:14px;">{rows}</table>')


def _page(blocks: list[str], head: str = "") -> str:
    return (f'<html>{head}<body style="margin:0;padding:18px;background:#ffffff;color:#1a1a1a;'
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
        when = f" from {_roll_date(dates[0])}"
    elif dates:
        when = f" from {_roll_date(dates[0])} to {_roll_date(dates[-1])}"
    count = len(trades)
    blocks = [_paragraph(
        f"<strong>{count} trade{'' if count == 1 else 's'}</strong>{escape(when)} "
        f"{'was' if count == 1 else 'were'} added to the ledger.")]

    if trades:
        blocks.append(_trade_table(trades))
        if any(trade.get("intent") not in ("open", "close") for trade in trades):
            blocks.append(_paragraph(f"* {escape(UNKNOWN_INTENT_NOTE)}.", color=MUTED_COLOR))
        blocks.append(_paragraph(escape(ECONFIRM_NOTE), color=MUTED_COLOR))

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
    return _page(['<div style="max-width:980px;margin:0 auto;overflow-wrap:anywhere;">',
                  *blocks, '</div>'], head=_comparison_head())


def announce(outcome: dict) -> bool:
    subject = subject_for(outcome)
    if subject is None:
        return False
    return send(subject, body_for(outcome), html_for(outcome))


# --------------------------------------------------------------------------
# price-move alerts
# --------------------------------------------------------------------------
# The move is a price move from the traded price. On anything held short its sign is the
# opposite of the profit, so every rendering pairs the percentage with its effect - a red
# "-80%" on a short put would read as precisely the wrong news.
ALERT_FOOTER = (
    "Bands are 10% for shares and ETFs and 50% for options, measured against the price "
    "the position was traded at. Each band is reported once for the life of the "
    "position, so this stays quiet until something reaches a level it has not reported "
    "before. Marks are delayed third-party quotes, not statement figures."
)


def _move(alert: dict) -> str:
    return f"{alert['move_pct']:+.1f}%"


def _effect(alert: dict) -> str:
    """`gain` or `loss`. On a short this is the opposite of the price direction."""
    return "loss" if alert["adverse"] else "gain"


def _effect_color(alert: dict) -> str:
    return DEBIT_COLOR if alert["adverse"] else CREDIT_COLOR


def _side(alert: dict) -> str:
    return "short" if alert["quantity"] < 0 else "long"


def _holding(alert: dict) -> str:
    """`short 2 SOXL $67 put`, `long 1,000 FNGU`."""
    return f"{_side(alert)} {fmt_qty(abs(alert['quantity']))} {_contract(alert)}"


def describe_alert(alert: dict) -> str:
    """One alert as a sentence, for the text part and the preview pane."""
    amount = alert.get("unrealized")
    standing = f", a {_effect(alert)} of {fmt_money(abs(amount))}" if amount is not None \
        else f", {'against' if alert['adverse'] else 'for'} the position"
    return (f"{_holding(alert)}: traded at {fmt_money(alert['entry_price'])}, "
            f"now {fmt_money(alert['price'])} - {_move(alert)}{standing} "
            f"({alert['band']}% band)")


def alert_subject(alerts: list) -> str | None:
    """The short line. None means send nothing."""
    if not alerts:
        return None
    if len(alerts) == 1:
        alert = alerts[0]
        return f"Schwab: {_contract(alert)} {_move(alert)} - {_effect(alert)}"
    losses = sum(1 for alert in alerts if alert["adverse"])
    gains = len(alerts) - losses
    split = []
    if gains:
        split.append(f"{gains} gain{'' if gains == 1 else 's'}")
    if losses:
        split.append(f"{losses} loss{'' if losses == 1 else 'es'}")
    return f"Schwab: {len(alerts)} price alerts - {', '.join(split)}"


def alert_body(alerts: list, unevaluated=None) -> str:
    count = len(alerts)
    lines = [
        f"{count} position{'' if count == 1 else 's'} reached a price band "
        f"{'it has' if count == 1 else 'they have'} not reported before.",
        "",
    ]
    for alert in alerts:
        lines.append(f"  {describe_alert(alert)}")
    lines.append("")

    losses = [alert for alert in alerts if alert["adverse"]]
    gains = [alert for alert in alerts if not alert["adverse"]]
    if gains and losses:
        lines.append(f"{len(gains)} moved in your favour, {len(losses)} against you.")
        lines.append("")

    if any(alert["quantity"] < 0 for alert in alerts):
        lines.append("A short position gains when its price falls, so the percentage and")
        lines.append("the gain or loss point opposite ways on those rows.")
        lines.append("")

    if unevaluated:
        lines.append("Not measured")
        for line in unevaluated:
            lines.append(f"  - {line}")
        lines.append("")

    lines.append(ALERT_FOOTER)
    return "\n".join(lines)


def _alert_table(alerts: list) -> str:
    columns = (("Position", "left"), ("Held", "right"), ("Traded", "right"),
               ("Mark", "right"), ("Move", "right"), ("Band", "right"),
               ("Effect", "right"))
    head = "".join(
        f'<th align="{align}" style="padding:0 10px 6px;border-bottom:2px solid #999;'
        f'font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;'
        f'color:{MUTED_COLOR};white-space:nowrap;">{escape(label)}</th>'
        for label, align in columns
    )

    rows = []
    for alert in alerts:
        color = _effect_color(alert)
        amount = alert.get("unrealized")
        effect = f"{_effect(alert)} {fmt_money(abs(amount))}" if amount is not None \
            else _effect(alert)
        rows.append(
            "<tr>"
            + _cell(escape(_contract(alert)))
            + _cell(escape(f"{_side(alert)} {fmt_qty(abs(alert['quantity']))}"),
                    align="right", color=MUTED_COLOR)
            + _cell(escape(fmt_money(alert["entry_price"])), align="right")
            + _cell(escape(fmt_money(alert["price"])), align="right")
            + _cell(f'<strong>{escape(_move(alert))}</strong>', align="right", color=color)
            + _cell(escape(f"{alert['band']}%"), align="right", color=MUTED_COLOR)
            + _cell(escape(effect), align="right", color=color)
            + "</tr>"
        )

    return ('<table cellpadding="0" cellspacing="0" border="0" role="presentation" '
            'style="border-collapse:collapse;width:100%;font-size:14px;">'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>")


def alert_html(alerts: list, unevaluated=None) -> str:
    count = len(alerts)
    blocks = [_paragraph(
        f"<strong>{count} position{'' if count == 1 else 's'}</strong> reached a price "
        f"band {'it has' if count == 1 else 'they have'} not reported before."
    )]
    blocks.append(_alert_table(alerts))

    if any(alert["quantity"] < 0 for alert in alerts):
        blocks.append(_paragraph(
            "A short position gains when its price falls, so the move and the effect "
            "point opposite ways on those rows. The colour follows the effect.",
            color=MUTED_COLOR))

    if unevaluated:
        blocks.append(_heading("Not measured"))
        blocks.append(_bullets(unevaluated))

    blocks.append(f'<hr style="margin:24px 0 12px;border:0;border-top:{RULE};">')
    blocks.append(_paragraph(ALERT_FOOTER, color=MUTED_COLOR))
    return _page(blocks)


def announce_alerts(alerts: list, unevaluated=None) -> bool:
    subject = alert_subject(alerts)
    if subject is None:
        return False
    return send(subject, alert_body(alerts, unevaluated), alert_html(alerts, unevaluated))


ROLL_COLUMNS = ("Contract", "Size", "Close / Open", "Cost ($/share)",
                "Current ($/share)", "Trade date", "Expiry")
ROLL_CLOSE_COLOR = "#925900"
ROLL_OPEN_COLOR = "#175ca4"
ROLL_NOTES = (
    "Cost/share: original = recorded basis reference, not a fill; new = estimated entry. "
    "Dates: MM-DD-YY; n/a = unverified opening history; - = not traded.",
    "Screen: same underlying/size, same or lower strike, later expiry within 70 calendar DTE; "
    "spread ≤50% of midpoint. No open-interest or last-trade filter.",
    "Coverage: first 4 eligible expiries; ≤500 weighted attempts/pass and rolling minute "
    "(metadata, depth, retries). Not exhaustive.",
    "Quotes: retrieval times only, no exchange quote timestamp; may be stale/asynchronous. "
    "Standard metadata: 100-share multiplier and size. Verify deliverables, full-size fills "
    "and buying power manually.",
    "Indicative, not executable offers. Net credit is not profit or realized P&L. "
    "Actual fees/fills may differ. Manual review; no orders placed.",
)


def _roll_intro(alerts: list) -> str:
    return f"{len(alerts)} roll candidate{'' if len(alerts) == 1 else 's'} · manual review"


def _roll_date(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%m-%d-%y %H:%M:%S %Z").strip()
    if isinstance(value, date):
        return value.strftime("%m-%d-%y")
    return str(value) if value is not None else "n/a"


def _roll_rows(alert: dict) -> list[tuple[str, ...]]:
    count = abs(alert["quantity"])
    basis = abs(Decimal(str(alert["reference_basis"])))
    basis /= Decimal(str(count)) * Decimal(str(alert["current"]["multiplier"]))
    dates = alert.get("original_trade_dates")
    original_date = ", ".join(_roll_date(value) for value in dates) if dates else "n/a"
    put = alert["option_type"] == "PUT"
    rows = []
    for label, key, leg, price_side, cost, trade_date in (
        ("Original", "current", "close", "ask" if put else "bid", basis, original_date),
        ("New", "candidate", "open", "bid" if put else "ask", alert["open_price"], "-"),
    ):
        quote = alert[key]
        strike = Decimal(str(quote["strike"]))
        strike_label = f"${strike:,.{max(2, -strike.as_tuple().exponent)}f}"
        rows.append((
            f"{label} · {quote['symbol']} {strike_label} {quote['option_type'].lower()}",
            fmt_qty(count), f"{alert[f'{leg}_action']}\n{price_side}", f"{cost:,.2f}",
            f"{alert[f'{leg}_price']:,.2f}\nBid {fmt_money(quote['bid'])} / Ask {fmt_money(quote['ask'])}",
            trade_date, _roll_date(quote["expiry"]),
        ))
    return rows


def _roll_summary(alert: dict) -> tuple[str, str]:
    gap = alert["open_price"] - alert["close_price"]
    if alert["option_type"] == "CALL":
        gap = -gap
    count = abs(alert["quantity"])
    total = alert["net_credit_total"]
    return (
        f"Gap {signed(gap)}/share · Net {_cash(total)} · +{alert['added_days']} days",
        f"Close {_cash(alert['close_cash_per_contract'] * count)} → "
        f"open {_cash(alert['open_cash_per_contract'] * count)} − fees {fmt_money(alert['fees_total'])}; "
        f"net {_cash(alert['net_credit_per_contract'])}/contract; {alert['dte']} DTE. "
        "Gap before fees; net after fees.",
    )


def _roll_quote_rows(alerts: list) -> list[tuple[str, ...]]:
    rows = []
    for index, alert in enumerate(alerts, 1):
        details = []
        for label, key in (("Original", "current"), ("New", "candidate")):
            quote = alert[key]
            details.append(f"{label}: {_roll_date(quote.get('fetched_at'))} · "
                           f"{quote.get('source') or 'Longbridge OPRA'}")
        rows.append((f"{index}. {alert['symbol']}", "\n".join(details),
                     f"{alert['current_spread']:.2%} / {alert['candidate_spread']:.2%}"))
    return rows


def _roll_notes(alerts: list) -> list[str]:
    notes = list(ROLL_NOTES)
    fees = ", ".join(fmt_money(fee) for fee in sorted({alert["fee_per_leg"] for alert in alerts}))
    notes.append(f"Fee allowance: {fees}/contract/leg; both legs included above.")
    if any(alert["option_type"] == "PUT" for alert in alerts):
        notes.append("Short puts: longer assignment/downside exposure.")
    if any(alert["option_type"] == "CALL" for alert in alerts):
        notes.append("No-cash long calls: unusual quotes; verify both legs.")
    return notes


def _roll_text_table(columns, rows) -> str:
    flattened = [columns, *(tuple(cell.replace("\n", "; ") for cell in row) for row in rows)]
    widths = [max(len(row[index]) for row in flattened) for index in range(len(columns))]
    lines = [" | ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
             for row in flattened]
    lines.insert(1, "-+-".join("-" * width for width in widths))
    return "\n".join(lines)


def _roll_html_table(columns, rows, comparison=False, cell_colors=None) -> str:
    headers = "".join(f'<th scope="col" style="padding:8px 6px;text-align:left;'
                      f'font-size:11px;color:{MUTED_COLOR};border-bottom:{RULE};">'
                      f'{escape(label)}</th>' for label in columns)
    rendered = []
    for row_index, row in enumerate(rows):
        cells = []
        for index, (label, value) in enumerate(zip(columns, row)):
            lines = value.split("\n")
            content = escape(lines[0])
            style = f"padding:9px 6px;vertical-align:top;border-bottom:{RULE};"
            if comparison:
                if index in (5, 6):
                    content = ", ".join(
                        f'<span style="white-space:nowrap;">{escape(part)}</span>'
                        if len(part) == 8 and part[2] == part[5] == "-" and part.replace("-", "").isdigit()
                        else escape(part) for part in lines[0].split(", "))
                if index in (1, 3, 4):
                    style += "text-align:right;font-variant-numeric:tabular-nums;"
                if index in (0, 2, 4):
                    content = f"<strong>{content}</strong>"
                if index == 2 and cell_colors is None:
                    style += f"color:{ROLL_CLOSE_COLOR if row_index == 0 else ROLL_OPEN_COLOR};"
            if cell_colors and (row_index, index) in cell_colors:
                style += f"color:{cell_colors[row_index, index]};"
            if len(lines) > 1:
                content += (f'<br><span style="font-size:11px;color:{MUTED_COLOR};">'
                            + "<br>".join(escape(line) for line in lines[1:]) + "</span>")
            cells.append(f'<td data-label="{escape(label, quote=True)}" style="{style}">{content}</td>')
        rendered.append("<tr>" + "".join(cells) + "</tr>")
    name = "roll-comparison" if comparison else "roll-details"
    widths = (26, 5, 15, 11, 18, 13, 12) if comparison else (20, 60, 20) if len(columns) == 3 else ()
    cols = '<colgroup>' + ''.join(f'<col style="width:{width}%;">' for width in widths) + '</colgroup>'
    layout = "table-layout:fixed;" if widths else ""
    return (f'<table class="{name}" cellpadding="0" cellspacing="0" '
            f'style="{layout}border-collapse:collapse;width:100%;font-size:13px;overflow-wrap:anywhere;">'
            f'{cols if widths else ""}<thead><tr>{headers}</tr></thead>'
            f'<tbody>{"".join(rendered)}</tbody></table>')


def roll_subject(alerts: list) -> str | None:
    if not alerts:
        return None
    return f"Schwab: {len(alerts)} roll candidate{'' if len(alerts) == 1 else 's'} to review"


def roll_body(alerts: list, unevaluated=None) -> str:
    if not alerts:
        return ""
    lines = [_roll_intro(alerts)]
    for index, alert in enumerate(alerts, 1):
        lines.extend(["", f"{index}. {alert['symbol']}",
                      _roll_text_table(ROLL_COLUMNS, _roll_rows(alert)), *_roll_summary(alert)])
    lines.extend(["", "Quote details", _roll_text_table(
        ("Position", "Depth retrieved / source", "Spread: original / new"), _roll_quote_rows(alerts))])
    if unevaluated:
        rows = [(line,) for line in dict.fromkeys(map(str, unevaluated))]
        lines.extend(["", _roll_text_table(("Coverage / skipped",), rows)])
    lines.extend(["", "Notes", *_roll_notes(alerts)])
    return "\n".join(lines)


def _comparison_head() -> str:
    return """<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
@media (max-width:600px) {
  .roll-comparison thead, .roll-comparison colgroup { display:none; }
  .roll-comparison, .roll-comparison tbody { display:block; }
  .roll-comparison tr { display:grid; grid-template-columns:repeat(2,minmax(0,1fr));
    border-bottom:2px solid #ddd; }
  .roll-comparison td { display:block; text-align:left !important; }
  .roll-comparison td:first-child { grid-column:1 / -1; background:#f6f8fa; }
  .roll-comparison td::before { content:attr(data-label); display:block;
    font-size:10px; color:#5c5c5c; margin-bottom:3px; }
  .roll-comparison td:first-child::before { display:none; }
  .roll-details { font-size:11px !important; }
  .roll-details col { width:25% !important; }
  .roll-details col:nth-child(2) { width:50% !important; }
}
</style></head>"""


def roll_html(alerts: list, unevaluated=None) -> str:
    if not alerts:
        return ""
    blocks = [_paragraph(f"<strong>{escape(_roll_intro(alerts))}</strong>")]
    for index, alert in enumerate(alerts, 1):
        summary, cash = _roll_summary(alert)
        gap, net, term = summary.split(" · ")
        total = alert["net_credit_total"]
        color = MUTED_COLOR if total == 0 else _cash_color(total)
        blocks.extend([
            _heading(f"{index}. {alert['symbol']}"),
            _roll_html_table(ROLL_COLUMNS, _roll_rows(alert), comparison=True),
            f'<div style="padding:10px 8px;background:#f6f8fa;border-bottom:{RULE};font-size:14px;">'
            f'{escape(gap)} · <strong style="color:{color};">{escape(net)}</strong> · '
            f'<span style="color:{MUTED_COLOR};">{escape(term)}</span></div>',
            f'<p style="margin:5px 8px 18px;font-size:11px;color:{MUTED_COLOR};line-height:1.5;">'
            f'{escape(cash)}</p>',
        ])
    blocks.extend([_heading("Quote details"), _roll_html_table(
        ("Position", "Depth retrieved / source", "Spread: original / new"), _roll_quote_rows(alerts))])
    if unevaluated:
        rows = [(line,) for line in dict.fromkeys(map(str, unevaluated))]
        blocks.append(_roll_html_table(("Coverage / skipped",), rows))
    blocks.extend([_heading("Notes"), f'<div style="font-size:11px;color:{MUTED_COLOR};line-height:1.5;">',
                   _bullets(_roll_notes(alerts)), '</div>'])
    return _page(['<div style="max-width:980px;margin:0 auto;overflow-wrap:anywhere;">',
                  *blocks, '</div>'], head=_comparison_head())


def announce_rolls(alerts: list, unevaluated=None) -> bool:
    subject = roll_subject(alerts)
    if subject is None:
        return False
    return send(subject, roll_body(alerts, unevaluated), roll_html(alerts, unevaluated))
