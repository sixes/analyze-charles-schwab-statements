"""Price-move alerts on the current position book.

One pass: refresh the marks, measure every open position against the price it was
traded at, and mail the bands that have not been reported before. Built for cron every
ten minutes through the US session.

The move is a **price** move, `(mark - entry) / entry`, not a return. Direction is
therefore not the same thing as profit: a short put whose premium falls 80% is a gain,
and one whose premium doubles is a loss. Both renderings state the effect on the
position alongside the price move, because a red "-80%" on a short would read as
exactly the wrong news.

Bands are 10% for shares and ETFs and 50% for options, and a band is news **once for
the life of the position**, not once a day. A holding sitting at +30% has nothing new to
say on a second morning, and mailing it every session is how a notification gets muted -
the same reason ingest stays silent on a tick that finds nothing.

Marks are delayed third-party quotes, so nothing here is a statement figure. This
publishes no account value, return or risk statistic, and a position that cannot be
priced or has no recorded basis is reported as unevaluated rather than dropped: silently
skipping a leg is how a monitor stops monitoring without saying so.
"""

from __future__ import annotations

import sys
from datetime import date

from . import notify, store
from .domain import fmt_money

# Multiples, in percent, of the move from the traded price. Options get a wider step
# because premium is leveraged: a 10% band on a contract would fire on noise.
STOCK_STEP = 10
OPTION_STEP = 50


def _absent(value) -> bool:
    # value != value catches the NaN that pandas puts in an absent numeric cell.
    return value is None or value != value


def step_for(is_option) -> int:
    return OPTION_STEP if is_option else STOCK_STEP


def band_for(move_pct: float, step: int) -> int:
    """The band a move has reached: 0 below the first step, else a multiple of it.

    Truncating rather than rounding means +19% reports the 10% band and only +20%
    reports the 20% one, so the number in the mail is a floor the move has passed.
    """
    return int(abs(move_pct) // step) * step


def evaluate(frame, session: date = None) -> tuple[list[dict], list[str], list[tuple]]:
    """(alerts, unevaluated, bases) for every open position in the frame.

    `alerts` are the positions past a band, before deduplication. `unevaluated` says
    why a position could not be measured. `bases` is every (key, entry) actually
    measured, which is what lets stale bands from an earlier basis be cleared.
    """
    alerts, unevaluated, bases = [], [], []
    if frame is None or getattr(frame, "empty", True):
        return alerts, unevaluated, bases

    from .positions import EXPIRED

    session = session or date.today()
    for _, row in frame.iterrows():
        key = row["key"]
        if row["status"] == EXPIRED:
            # Priced at zero by the rollforward on an assumption, not a quote, so a
            # -100% move here would be an artefact of that assumption.
            unevaluated.append(f"{key}: past expiry, valued at zero on an assumption")
            continue

        entry, price, quantity = row["entry_price"], row["price"], row["quantity"]
        if _absent(entry) or abs(float(entry)) < 1e-9:
            unevaluated.append(f"{key}: no traded price recorded, so no reference to "
                               "measure a move against")
            continue
        if _absent(price):
            unevaluated.append(f"{key}: no market mark")
            continue

        # A short's basis is a credit, so the sign of entry_price follows the sign of the
        # quantity. The reference price itself is the magnitude either way.
        entry = abs(float(entry))
        price = float(price)
        move_pct = (price - entry) / entry * 100
        step = step_for(row["is_option"])
        band = band_for(move_pct, step)
        bases.append((key, entry))
        if band < step:
            continue

        long = float(quantity) > 0
        rising = move_pct > 0
        alerts.append({
            "key": key,
            "symbol": row["symbol"],
            "label": row["label"],
            "is_option": bool(row["is_option"]),
            "option_type": row["option_type"],
            "strike": None if _absent(row["strike"]) else float(row["strike"]),
            "quantity": float(quantity),
            "entry_price": entry,
            "price": price,
            "move_pct": move_pct,
            "band": band,
            "step": step,
            "direction": "up" if rising else "down",
            # Whether the price move went against the position, which is the opposite
            # of its direction on anything held short.
            "adverse": rising != long,
            "unrealized": None if _absent(row["unrealized"]) else float(row["unrealized"]),
            "status": row["status"],
            "session_date": session,
        })

    alerts.sort(key=lambda alert: -abs(alert["move_pct"]))
    return alerts, unevaluated, bases


def describe(alert: dict) -> str:
    """One alert as a line for the log."""
    effect = "against" if alert["adverse"] else "for"
    return (f"{alert['key']} {alert['move_pct']:+.1f}% "
            f"({alert['band']}% band, {alert['direction']}) "
            f"{fmt_money(alert['entry_price'])} -> {fmt_money(alert['price'])}, "
            f"{effect} the position")


def run(dry_run: bool = False, notify_on: bool = True, **kwargs) -> int:
    """One monitoring pass. 0 on success, 1 on failure."""
    notify_on = kwargs.pop("notify", notify_on)

    from . import positions as positions_module, quotes as quotes_module

    try:
        with store.connect() as conn:
            store.ensure_schema(conn)
            records = store.load_records(conn)
            trades = store.load_trades(conn)
            if not records:
                print("No statements stored; nothing to anchor positions on.",
                      file=sys.stderr)
                return 1

            # Quoted fresh rather than read from the cache: a ten-minute tick exists to
            # see the move, and the cache is only as new as the last run.
            base = positions_module.rollforward(records, trades)
            symbols = positions_module.quote_symbols(base)
            marks = {}
            if symbols:
                marks = quotes_module.fetch_quotes(symbols)
                if not dry_run:
                    # The web UI and the report read this cache, so a tick keeps them
                    # current as a side effect.
                    store.save_quotes(conn, marks)

            frame = positions_module.rollforward(records, trades, marks=marks)
            priced = sum(1 for mark in marks.values() if mark.get("price") is not None)
            print(f"{len(frame)} position(s); {priced} of {len(marks)} priced")
            if symbols and not priced:
                # Every quote failed, which is a market or network problem rather than
                # news about the account. Say so and stay silent.
                print("no symbol could be priced; nothing to measure", file=sys.stderr)
                return 0

            alerts, unevaluated, bases = evaluate(frame)
            for line in unevaluated:
                print(f"  unevaluated {line}")

            if not dry_run:
                for key, entry in bases:
                    store.clear_rebased_alerts(conn, key, entry)

            if not alerts:
                print("no position past a band")
                return 0

            print(f"{len(alerts)} position(s) past a band:")
            for alert in alerts:
                print(f"  {describe(alert)}")

            if dry_run:
                print("[dry run] nothing recorded, nothing mailed")
                return 0

            claimed = store.claim_alerts(conn, alerts)
            if not claimed:
                print("every band already reported; staying silent")
                return 0
            print(f"{len(claimed)} band(s) not reported before")

            if not notify_on:
                print("notification suppressed; bands recorded as reported")
                return 0

            if not notify.announce_alerts(claimed, unevaluated=unevaluated):
                # A band recorded but never sent would be lost for the life of the
                # position, so the claim is released and the next tick retries.
                store.drop_alerts(conn, claimed)
                print("notification failed; bands released for the next tick",
                      file=sys.stderr)
                return 1
    except Exception as exc:
        print(f"monitor failed: {exc}", file=sys.stderr)
        return 1
    return 0
