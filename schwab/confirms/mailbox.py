"""Read-only IMAP access to the mailbox that receives forwarded Schwab eConfirms.

The mailbox holds thousands of unrelated personal messages, so the search is narrow:
a date floor plus a subject match. The connection selects the folder in readonly mode
and this module never deletes, moves or flags anything - IMAP stays the archive of
record and the database only ever holds parsed figures.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import date, timedelta, timezone
from email.header import decode_header, make_header

from .parse import SUBJECT_HINT

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
FOLDER = "INBOX"

# Gmail's SEARCH wants DD-Mon-YYYY.
IMAP_DATE = "%d-%b-%Y"


class MailboxError(RuntimeError):
    """Login, connection or search failure - reported, never silently swallowed.

    `reason` is the short label the notification subject carries, so an unreachable host
    does not read as a rejected password and send the reader after the app password.
    """

    def __init__(self, message: str, reason: str = "mailbox error"):
        super().__init__(message)
        self.reason = reason


def credentials() -> tuple[str, str]:
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not user or not password:
        raise MailboxError("GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env",
                           reason="gmail credentials missing")
    return user, password


def decode(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def text_body(message: email.message.Message) -> str:
    """The plain-text part. Falls back to stripping tags out of the HTML part."""
    plain, html = [], []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" in disposition.lower():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        if part.get_content_subtype() == "html":
            html.append(decoded)
        else:
            plain.append(decoded)
    if plain:
        return "\n".join(plain)
    if html:
        stripped = re.sub(r"<(script|style)\b.*?</\1>", " ", "\n".join(html),
                          flags=re.IGNORECASE | re.DOTALL)
        stripped = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        return re.sub(r"[ \t]{2,}", " ", stripped)
    return ""


def received_at(message: email.message.Message):
    raw = message.get("Date")
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def fetch(days: int = 7, subject_hint: str = SUBJECT_HINT, limit: int = 200) -> list[dict]:
    """Candidate confirm messages received in the last `days`, newest last.

    Returns dicts of {gmail_uid, message_id, subject, sender, internal_date, body}.
    Both the direct Schwab sender and the forwarded copy have to match, so the subject
    is the anchor rather than the From address.
    """
    user, password = credentials()
    since = (date.today() - timedelta(days=max(days, 1))).strftime(IMAP_DATE)

    try:
        client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    except OSError as exc:
        raise MailboxError(f"cannot reach {IMAP_HOST}: {exc}",
                           reason="imap unreachable") from exc

    try:
        try:
            client.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"imap login rejected for {user}: {exc}",
                               reason="imap login") from exc

        status, _ = client.select(FOLDER, readonly=True)
        if status != "OK":
            raise MailboxError(f"cannot select {FOLDER}", reason="imap folder")

        status, data = client.search(None, "SINCE", since, "SUBJECT", f'"{subject_hint}"')
        if status != "OK":
            raise MailboxError("imap search failed", reason="imap search")

        uids = (data[0] or b"").split()
        if not uids:
            return []
        uids = uids[-limit:]

        messages = []
        for uid in uids:
            status, payload = client.fetch(uid, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            message = email.message_from_bytes(payload[0][1])
            body = text_body(message)
            if not body.strip():
                continue
            messages.append({
                "gmail_uid": uid.decode("ascii", errors="replace"),
                "message_id": (message.get("Message-ID") or "").strip() or None,
                "subject": decode(message.get("Subject")),
                "sender": decode(message.get("From")),
                "internal_date": received_at(message),
                "body": body,
            })
        return messages
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass
