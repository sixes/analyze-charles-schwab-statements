"""Command line entry point: python3 -m schwab <command>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import store
from .domain import DEFAULT_RISK_FREE, fmt_money


def build_holdings_frame(records: list):
    import pandas as pd

    rows = []
    for record in records:
        for holding in record["_holdings"]:
            rows.append(dict(
                holding,
                month=record["period_end"].strftime("%Y-%m"),
                period_end=record["period_end"],
            ))
    return pd.DataFrame(rows)


def build_flows_frame(records: list):
    import pandas as pd

    rows = []
    for record in records:
        for flow in record["_flows"]:
            rows.append({
                "month": record["period_end"].strftime("%Y-%m"),
                "date": flow["date"],
                "amount": flow["amount"],
                "description": flow["description"],
            })
    return pd.DataFrame(rows)


def parse_pdfs(directory: Path, verbose: bool):
    from .statements import StatementParser

    pdfs = sorted(p for p in directory.iterdir() if p.suffix.lower() == ".pdf")
    if not pdfs:
        return [], []
    print(f"Parsing {len(pdfs)} statement(s) from {directory}")
    records, paths = [], []
    for path in pdfs:
        try:
            record = StatementParser(path, verbose=verbose).parse()
        except Exception as error:  # a malformed statement must not kill the run
            print(f"  FAILED {path.name}: {error}", file=sys.stderr)
            continue
        records.append(record)
        paths.append(path)
        print(
            f"  {path.name}: {record['period_start']} to {record['period_end']}  "
            f"begin {fmt_money(record.get('beginning_value'))} -> "
            f"end {fmt_money(record.get('ending_value'))}  "
            f"({record['holdings_count']} positions)"
        )
    return records, paths


def resolve_rf(args) -> float:
    """--rf wins; otherwise the rate saved from the UI, else the shared default."""
    if args.rf is not None:
        return args.rf
    try:
        with store.connect() as conn:
            return store.risk_free(conn)
    except Exception:
        return DEFAULT_RISK_FREE


def cmd_analyze(args) -> int:
    from . import analytics, charts as chart_module, report as report_module

    rf = resolve_rf(args)
    records, paths = [], []
    if args.from_db:
        with store.connect() as conn:
            records = store.load_records(conn)
        if not records:
            print("No statements stored. Upload some, or run without --from-db.", file=sys.stderr)
            return 1
        print(f"Loaded {len(records)} statement(s) from {store.database_name()}")
    else:
        source = Path(args.dir).expanduser().resolve()
        records, paths = parse_pdfs(source, args.verbose)
        if not records:
            print(f"No statements parsed from {source}.", file=sys.stderr)
            return 1

    seen = {}
    for record in records:
        key = record["period_end"]
        if key in seen:
            print(f"  WARNING duplicate period {key}: {seen[key]} and {record['file']}")
        seen[key] = record["file"]

    if args.save_db:
        store.initialize()
        with store.connect() as conn:
            for path, record in zip(paths, records):
                digest = store.statement_digest(path.read_bytes())
                store.save_record(conn, record, digest)
                print(f"  stored {record['file']} ({digest[:12]})")

    frame = analytics.build_frame(records)
    metrics = analytics.compute_metrics(frame, rf)
    attribution = analytics.class_attribution(frame)
    holdings = build_holdings_frame(records)
    flows = build_flows_frame(records)
    transactions = analytics.transaction_frame(records)
    pending = analytics.transaction_frame(records, include_pending=True)
    pending = pending[~pending["settled"]] if not pending.empty else pending

    trades = None
    marks = {}
    if not args.no_confirms:
        try:
            with store.connect() as conn:
                trades = store.load_trades(conn)
                if not args.no_quotes:
                    marks = store.load_quotes(conn)
        except Exception as error:
            print(f"  confirm trades unavailable: {error}", file=sys.stderr)

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    written = []

    def dump(name: str, payload):
        if payload is None:
            return
        if hasattr(payload, "empty"):
            if payload.empty:
                return
            payload.to_csv(out / name, index=False)
        else:
            (out / name).write_text(payload)
        written.append(name)

    dump("monthly_summary.csv", frame.drop(columns=[c for c in frame.columns if c.startswith("_")]))
    dump("asset_class_reconciliation.csv", attribution)
    dump("holdings.csv", holdings)
    dump("cash_flows.csv", flows)
    dump("transactions.csv", transactions)
    dump("pending_transactions.csv", pending)
    if transactions is not None and not transactions.empty:
        dump("premium_by_month.csv", analytics.premium_by_month(transactions))
        dump("premium_by_symbol.csv", analytics.premium_by_symbol(transactions))
    dump("confirm_trades.csv", trades)
    dump("metrics.json", json.dumps(metrics, indent=2, default=str))

    reconciliation = None
    if trades is not None and not trades.empty:
        from . import reconcile as reconcile_module

        reconciliation, _ = reconcile_module.reconcile_all(records, trades)
        dump("reconciliation.csv", reconciliation)

    positions = None
    interim = None
    if trades is not None and not trades.empty:
        from . import positions as positions_module

        positions = positions_module.rollforward(records, trades, marks=marks)
        interim = positions_module.interim_performance(
            records, trades, marks=marks, frame=positions
        )
        dump("current_positions.csv", positions)

    report = report_module.build_report(
        frame, metrics, attribution, rf, transactions,
        reconciliation=reconciliation, positions=positions, interim=interim,
    )
    dump("performance_report.txt", report + "\n")

    chart_names = []
    if not args.no_charts:
        chart_names = chart_module.write_charts(frame, holdings, metrics, out / "charts")

    print()
    print(report)
    print()
    print(f"Data written to {out}")
    for name in written:
        print(f"  {name}")
    for name in chart_names:
        print(f"  charts/{name}")
    return 0


def cmd_initdb(args) -> int:
    created = store.initialize()
    print(f"database {store.database_name()}: {'created' if created else 'already present'}")
    print(f"schema {store.SCHEMA}: ready")
    return 0


def cmd_inventory(args) -> int:
    with store.connect() as conn:
        rows = store.statement_index(conn)
        trades = store.trade_index(conn)
        alerts = store.alert_index(conn)
    if rows:
        print(f"{len(rows)} statement(s) stored:")
        for row in rows:
            print(
                f"  {row['period_start']} to {row['period_end']}  "
                f"{fmt_money(store.clean(row['ending_value']))}  "
                f"{row['transaction_rows']} transaction rows  {row['file']}"
            )
    else:
        print("No statements stored.")
    if trades:
        total = sum(row["trade_count"] for row in trades)
        print(f"\n{len(trades)} confirm email(s), {total} trade(s):")
        for row in trades:
            flag = "" if row["status"] == "ok" else f"  [{row['status']}]"
            print(f"  {row['confirm_date']}  {row['trade_count']} trade(s){flag}")
    else:
        print("\nNo confirm emails ingested.")
    if alerts:
        print(f"\n{len(alerts)} price band(s) already reported:")
        for row in alerts:
            when = row["created_at"].astimezone()
            print(f"  {when:%Y-%m-%d %H:%M}  {row['position_key']:<24} "
                  f"{row['direction']} {row['band']}%  at "
                  f"{fmt_money(store.clean(row['price']))} "
                  f"from {fmt_money(store.clean(row['entry_price']))}")
        print("  A band is reported once for the life of a position, so these stay silent.")
    return 0


def cmd_ingest(args) -> int:
    from .confirms import ingest

    return ingest.run(
        days=args.days,
        reprocess=args.reprocess,
        dry_run=args.dry_run,
        notify=not args.no_notify,
        quotes=not args.no_quotes,
    )


def cmd_monitor(args) -> int:
    from . import monitor

    return monitor.run(dry_run=args.dry_run, notify=not args.no_notify,
                       rolls_on=not args.no_rolls, roll_fee=args.roll_fee_per_leg,
                       put_min_credit=args.put_min_credit)


def cmd_news(args) -> int:
    from . import news

    return news.run(dry_run=args.dry_run, notify=not args.no_notify)


def _roll_fee(value):
    from .rolls import parameters
    try:
        return parameters(fee=value)[0]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _put_credit(value):
    from .rolls import parameters
    try:
        return parameters(put_min_credit=value)[1]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def cmd_positions(args) -> int:
    from . import positions as positions_module

    with store.connect() as conn:
        records = store.load_records(conn)
        trades = store.load_trades(conn)
        marks = {} if args.no_quotes else store.load_quotes(conn)
    if not records:
        print("No statements stored; nothing to anchor positions on.", file=sys.stderr)
        return 1
    frame = positions_module.rollforward(records, trades, marks=marks)
    if frame.empty:
        print("No open positions.")
        return 0
    print(positions_module.render(frame))
    for warning in positions_module.warnings(frame):
        print(f"  ! {warning}")
    return 0


def cmd_quotes(args) -> int:
    from . import positions as positions_module, quotes as quotes_module

    with store.connect() as conn:
        records = store.load_records(conn)
        trades = store.load_trades(conn)
        frame = positions_module.rollforward(records, trades)
        symbols = positions_module.quote_symbols(frame)
        if not symbols:
            print("No symbols to quote.")
            return 0
        results = quotes_module.fetch_quotes(symbols)
        store.save_quotes(conn, results)
    good = sum(1 for r in results.values() if r["price"] is not None)
    print(f"{good} of {len(results)} symbol(s) quoted")
    for symbol, result in sorted(results.items()):
        if result["price"] is None:
            print(f"  {symbol:<24} n/a  {result['error']}")
        else:
            print(f"  {symbol:<24} {fmt_money(result['price'])}  as of {result['as_of']}")
    return 0


def cmd_reconcile(args) -> int:
    from . import reconcile as reconcile_module

    with store.connect() as conn:
        records = store.load_records(conn)
        trades = store.load_trades(conn)
    if trades.empty:
        print("No confirm trades ingested; nothing to reconcile against.", file=sys.stderr)
        return 1
    frame, summary = reconcile_module.reconcile_all(records, trades)
    print(reconcile_module.render(frame, summary))
    return 0 if summary["discrepancies"] == 0 else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m schwab", description="Charles Schwab statement and trade-confirm analysis"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="parse statements and write data, report and charts")
    analyze.add_argument("--dir", default=".", help="directory containing statement PDFs")
    analyze.add_argument("--out", default="output", help="output directory")
    analyze.add_argument("--rf", type=float, default=None,
                         help="annual risk-free rate; defaults to the rate saved in the UI")
    analyze.add_argument("--from-db", action="store_true",
                         help="analyze the statements already stored instead of reading PDFs")
    analyze.add_argument("--save-db", action="store_true",
                         help="also store each parsed statement in Postgres")
    analyze.add_argument("--no-charts", action="store_true", help="skip chart generation")
    analyze.add_argument("--no-confirms", action="store_true",
                         help="skip confirm trades, reconciliation and positions")
    analyze.add_argument("--no-quotes", action="store_true",
                         help="omit market marks from the position rollforward")
    analyze.add_argument("--verbose", action="store_true", help="report unparsed lines")
    analyze.set_defaults(func=cmd_analyze)

    ingest = sub.add_parser("ingest", help="pull new eConfirm emails into the database")
    ingest.add_argument("--days", type=int, default=7, help="how far back to search the mailbox")
    ingest.add_argument("--reprocess", action="store_true",
                        help="re-parse confirms already stored, replacing their trades")
    ingest.add_argument("--dry-run", action="store_true", help="parse but write nothing")
    ingest.add_argument("--no-notify", action="store_true", help="skip the notification email")
    ingest.add_argument("--no-quotes", action="store_true", help="skip refreshing quotes")
    ingest.set_defaults(func=cmd_ingest)

    monitor = sub.add_parser(
        "monitor", help="price-band and defensive option-roll email alerts",
        description="Monitor price bands and screen same-underlying short-put/long-call rolls. "
                    "Replacements have later expiry, same/lower strike, unchanged quantity, "
                    "at most 70 calendar DTE (New York date), and bid/ask spread <=50% of "
                    "midpoint. No open-interest or last-trade requirement. Calls require "
                    "no added cash; puts require net credit after estimated fees.",
        epilog="Roll data requires LONGBRIDGE_APP_KEY, LONGBRIDGE_APP_SECRET, "
               "LONGBRIDGE_ACCESS_TOKEN and OPRA US Options Quotes (OpenAPI) permission. "
               "Shared local limit: 500 contract attempts per rolling minute and per pass; "
               "metadata and depth each consume quota. At most four later expiries are "
               "searched. Duplicate rolls are suppressed; different candidates have a "
               "24-hour per-position cooldown. Indicative screens only; never places orders."
    )
    monitor.add_argument("--dry-run", action="store_true",
                         help="preview without database writes or email; real API calls still "
                              "consume quota and update local quota counters")
    monitor.add_argument("--no-notify", action="store_true",
                         help="record bands and rolls as reported without email, silencing "
                              "them and starting roll cooldowns")
    monitor.add_argument("--no-rolls", action="store_true",
                         help="price-band monitoring only; do not request Longbridge data")
    monitor.add_argument("--roll-fee-per-leg", type=_roll_fee, default="0.65", metavar="USD",
                         help="estimated fee per contract per leg (default: 0.65)")
    monitor.add_argument("--put-min-credit", type=_put_credit, default="5.00", metavar="USD",
                         help="positive minimum net put-roll credit per contract (default: 5)")
    monitor.set_defaults(func=cmd_monitor)

    from .news_qoder import DEADLINE as NEWS_DEADLINE

    news = sub.add_parser(
        "news", help="email a position-aware Sonus market briefing", allow_abbrev=False,
        description="Research the last completed US trading session, its drivers and movers, "
                    "next-session catalysts, and material position/watchlist updates with Sonus. "
                    "Email GMAIL_TO without report copies. Only underlying tickers, long/short "
                    "shares/call/put categories and watchlist symbols go to Qoder. Portfolio data stays read-only.",
        epilog="Reuses the news profile's existing login unattended; requires verified web-only execution "
               "inside bubblewrap. Qoder's home, diagnostics, plugin caches and temporary files stay "
               "in RAM; only the existing authentication directory is writable on disk. "
               "Sonus is the primary model; Qoder 1.1.66 WebFetch uses a secondary lite model. "
               "Search any publisher; public-web fetches receive per-request approval without "
               "a publisher allowlist. Non-public destinations and other tools are not approved. "
               f"Research consumes Qoder usage/credits: {NEWS_DEADLINE / 60:g}-minute deadline, 6000 output "
               "tokens per response, one model-request retry; these are not a monetary cap. "
               "Stage messages and a 30-second research heartbeat show progress without report content. "
               "Normal ./run and ./run.sh runs print to the terminal and append to private .run/news.log; "
               "cron redirection to the same log does not duplicate lines. Help and dry-run previews "
               "are not written to the log. Report content is never saved; only operational logs, "
               "a lock and delivery date/timestamp are kept locally. Already SMTP-accepted "
               "Shanghai dates are skipped. SMTP retries within a run reuse memory; retrying a "
               "failed run requires fresh research. SMTP acceptance is not inbox delivery; "
               "a crash can cause duplicate email."
    )
    news.add_argument("--dry-run", action="store_true",
                      help="print sanitized prompt after a read-only position snapshot; no "
                           "model, report files, lock or email (launcher still loads/chmods .env)")
    news.add_argument("--no-notify", action="store_true",
                      help="research and validate, then discard without saving or emailing; consumes credits")
    news.set_defaults(func=cmd_news)

    positions = sub.add_parser("positions", help="current positions rolled forward from confirms")
    positions.add_argument("--no-quotes", action="store_true", help="omit market marks")
    positions.set_defaults(func=cmd_positions)

    quotes = sub.add_parser("quotes", help="refresh cached market marks for open positions")
    quotes.set_defaults(func=cmd_quotes)

    reconcile = sub.add_parser("reconcile", help="check statements against confirm trades")
    reconcile.set_defaults(func=cmd_reconcile)

    sub.add_parser("initdb", help="create the database and schema").set_defaults(func=cmd_initdb)
    sub.add_parser("inventory", help="what is stored").set_defaults(func=cmd_inventory)

    args = parser.parse_args(argv)
    return args.func(args)
