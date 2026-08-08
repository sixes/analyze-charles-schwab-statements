"""One ingestion pass: mailbox -> parser -> database -> quotes -> notification.

Designed to be run from cron every few minutes and to exit quickly. Every step is
guarded so that a single failure produces one legible notification rather than a
traceback in a log nobody reads, and the exit code is non-zero on failure so cron's own
mail acts as a backstop if SMTP is what broke.
"""

from __future__ import annotations

import logging
import sys

from .. import notify, store
from . import mailbox, parse

log = logging.getLogger(__name__)


def _refresh_quotes(conn) -> tuple[int, int, int, list]:
    """Warm the quote cache for the current derived position set."""
    from .. import positions as positions_module, quotes as quotes_module

    records = store.load_records(conn)
    trades = store.load_trades(conn)
    frame = positions_module.rollforward(records, trades)
    symbols = positions_module.quote_symbols(frame)
    if not symbols:
        return len(frame), 0, 0, positions_module.warnings(frame)
    results = quotes_module.fetch_quotes(symbols)
    store.save_quotes(conn, results)
    good = sum(1 for result in results.values() if result["price"] is not None)
    return len(frame), good, len(results), positions_module.warnings(frame)


def run(days: int = 7, reprocess: bool = False, dry_run: bool = False,
        notify_on: bool = True, quotes: bool = True, **kwargs) -> int:
    # cli.py passes notify= and quotes=; accept both spellings without breaking either.
    notify_on = kwargs.pop("notify", notify_on)

    outcome = {
        "status": "nothing",
        "emails_seen": 0,
        "emails_stored": 0,
        "emails_skipped": 0,
        "trades_stored": 0,
        "blocks_failed": 0,
        "net_amount": 0.0,
        "trades": [],
        "failures": [],
    }

    def finish(code: int) -> int:
        if notify_on:
            notify.announce(outcome)
        return code

    try:
        messages = mailbox.fetch(days=days)
    except mailbox.MailboxError as exc:
        outcome.update(status="failed", reason="imap login", error=str(exc))
        print(f"mailbox unreachable: {exc}", file=sys.stderr)
        return finish(1)
    except Exception as exc:
        outcome.update(status="failed", reason="mailbox error", error=str(exc))
        print(f"mailbox error: {exc}", file=sys.stderr)
        return finish(1)

    outcome["emails_seen"] = len(messages)
    if not messages:
        print(f"No eConfirm messages in the last {days} day(s).")
        return finish(0)

    try:
        with store.connect() as conn:
            store.ensure_schema(conn)
            seen = store.known_bodies(conn)

            for message in messages:
                digest = store.body_digest(message["body"])
                if digest in seen and not reprocess:
                    outcome["emails_skipped"] += 1
                    continue

                try:
                    parsed = parse.parse_confirm(message["body"])
                except Exception as exc:
                    outcome["blocks_failed"] += 1
                    outcome["failures"].append(f"{message.get('subject', '?')}: {exc}")
                    log.warning("parse failed for %s: %s", message.get("subject"), exc)
                    continue

                trades = parsed["trades"]
                failures = [f"block {item['seq']}: {item['error']}"
                            for item in parsed["failed"]]
                outcome["blocks_failed"] += len(failures)
                outcome["failures"].extend(failures)

                if not trades and not failures:
                    # A confirm-shaped subject with no trade block: a summary or a bounce.
                    outcome["emails_skipped"] += 1
                    continue

                meta = {
                    "body_sha256": digest,
                    "message_id": message.get("message_id"),
                    "gmail_uid": message.get("gmail_uid"),
                    "internal_date": message.get("internal_date"),
                    "confirm_date": parsed.get("confirm_date"),
                    "account_tail": parsed.get("account_tail"),
                    "status": "partial" if failures else "ok",
                    "error": "; ".join(failures) or None,
                }

                if dry_run:
                    print(f"[dry run] would store {len(trades)} trade(s) from "
                          f"{parsed.get('confirm_date')} ({digest[:12]})")
                else:
                    store.save_confirm(conn, meta, trades)

                outcome["emails_stored"] += 1
                outcome["trades_stored"] += len(trades)
                outcome["net_amount"] += parsed.get("net_amount") or 0.0
                outcome["trades"].extend(trades)

            if outcome["emails_stored"] and not dry_run and quotes:
                try:
                    count, good, total, warnings = _refresh_quotes(conn)
                    outcome.update(positions=count, quotes_ok=good, quotes_total=total,
                                   warnings=warnings)
                except Exception as exc:
                    # A quote failure must not lose the trades that were just stored.
                    outcome.setdefault("warnings", []).append(f"quote refresh failed: {exc}")
                    log.warning("quote refresh failed: %s", exc)
    except Exception as exc:
        outcome.update(status="failed", reason="database error", error=str(exc))
        print(f"database error: {exc}", file=sys.stderr)
        return finish(1)

    if outcome["emails_stored"] or outcome["blocks_failed"]:
        outcome["status"] = "stored"
    else:
        outcome["status"] = "nothing"

    print(f"{outcome['emails_seen']} message(s) matched; "
          f"{outcome['emails_stored']} stored, {outcome['emails_skipped']} already known; "
          f"{outcome['trades_stored']} trade(s), net {outcome['net_amount']:+,.2f}")
    for failure in outcome["failures"]:
        print(f"  ! {failure}", file=sys.stderr)

    if dry_run:
        return 0
    return finish(2 if outcome["blocks_failed"] else 0)
