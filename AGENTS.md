# Charles Schwab Statement Analysis

Analyzes monthly Charles Schwab brokerage statement PDFs into performance data and charts.

## Layout

- `*.PDF` — monthly Schwab statements, one per statement period, kept in the repo root.
- `analyze_schwab.py` — parser + analytics + chart generation. CLI entry point, and the
  single source of truth for every number and figure.
- `app.py` — Streamlit web UI. Users upload statement PDFs and get the same results.
  It imports `analyze_schwab`; it must never reimplement parsing or analytics.
- `store.py` — Postgres persistence. Pure I/O: it stores and rebuilds the record dicts
  the parser produces and contains no analytics.
- `run.sh` — process control (`start|stop|restart|status|logs|initdb|inventory`). It
  sources `.env` so `DATABASE_URL` and `APP_PASSWORD` never appear on a command line.
- `.env` — local secrets, `chmod 600`, never committed.
- `.streamlit/config.toml` — binds the server to `0.0.0.0:9388`. The app is reachable
  over the network, so `app.py` refuses to render anything without `APP_PASSWORD`.
- `output/` — generated artifacts (CSV, JSON, report, `charts/`). Disposable; regenerate
  rather than editing by hand.

## Commands

```bash
pip3 install -r requirements.txt

# CLI: parse ./*.PDF -> ./output
python3 analyze_schwab.py
python3 analyze_schwab.py --dir path --out path --rf 0.042
python3 analyze_schwab.py --verbose       # show unparsed statement lines
python3 analyze_schwab.py --save-db       # also persist to Postgres

# Web UI on port 9388, password from .env
./run.sh initdb          # create the database and schema once
./run.sh start|restart|stop|status|logs
./run.sh inventory       # what is stored
```

## Shared architecture

`StatementParser` accepts either a filesystem path or a binary file-like object, so the
CLI and the uploader feed the same parser. Charts are built once by `build_charts()`,
which returns `[(filename, figure)]`; the CLI's `make_charts()` writes them to disk and
the app renders the same figures with `st.pyplot`. Add or change a chart in
`build_charts()` only — both front ends pick it up.

## Storage

`DATABASE_URL` points at the server's *maintenance* database. `store.py` creates its own
database (`SCHWAB_DB`, default `schwab_statements`) and works only inside schema
`schwab`. Databases that already exist on the server are read from `pg_database` and
never written to; do not add code that writes outside `SCHWAB_DB`.

Tables: `statements` (one row per PDF, keyed by the file's `sha256`), plus
`holdings`, `transactions` and `cash_flows` referencing it with `on delete cascade`.
Settled and pending activity share the `transactions` table and are told apart by the
`settled` flag. Saving is an upsert on `sha256`, so re-uploading a PDF replaces its rows
instead of duplicating them.

`load_records()` must return dicts indistinguishable from freshly parsed ones, because
the analytics assume it:

- every `SCALAR_KEYS` entry present, `None` when the statement did not print it;
- money as `float`, never `Decimal` — `build_frame` adds floats and `compute_metrics`
  takes a standard deviation;
- dates as `datetime.date`, never `timestamptz` — `modified_dietz` subtracts dates;
- `_holdings`, `_flows`, `_transactions`, `_pending` defaulting to `[]`.

Two files can cover one statement period, and counting both would double that month, so
reads use `distinct on (period_end)` and keep the most recently saved one.

Never persist PDF bytes or raw page text: they carry the account number and a home
address. Only parsed figures belong in the database.

## Domain conventions

Financial analysis in this repo follows the `cfa-analyst` agent's standards. The essentials:

- Report time-weighted return (Modified Dietz, day-weighted flows) for investment
  performance and money-weighted IRR for the investor's realized experience. Never
  present raw account-value change as performance — deposits inflate it.
- Link periodic returns geometrically. Do not annualize a track record shorter than
  12 months; label it cumulative.
- Keep realized separate from unrealized, and short-term separate from long-term.
- Short options and margin balances are negative exposures. Leveraged ETFs (the 2x/3x
  Direxion, ProShares, and MicroSectors holdings here) are path-dependent — never
  extrapolate their returns linearly.
- Option premium is not income. A short option's credit is a liability until the position
  is closed or expires, and only then is a gain realized. Report premium **net** (credits
  from short sales plus what was paid to close) and show the gross credits beside it.
  Long option purchases are reported separately, never netted into premium.
  `premium_summary`'s realized figures cover closing trades only — expirations print no
  realized column — so the Gain or Loss section stays the authoritative realized total.
- Gross notional assumes the standard 100-share multiplier, which the statement does not
  print. Label it derived wherever it appears.
- Pending activity is stored and shown because it is real economic exposure, but it is
  always flagged: it is unsettled, excluded from the statement's account value, and can
  still change.
- Every derived figure must tie back to a printed figure on the statement. The parser
  cross-checks derived gain against the statement's Market Appreciation line and reports
  the residual; investigate residuals rather than suppressing them.

## Parser rules

- Schwab's PDF text extraction drops intra-label spaces (`BeginningAccountValue`), so
  match labels against a whitespace-stripped copy of the line while extracting numbers
  from the raw, space-separated line.
- Only treat a whitespace-delimited token as a number when the **whole** token is numeric.
  Substring matching corrupts glued artifacts like `07/24/202649.50EXP07/24/26` and
  percent columns like `46.40%`.
- Parenthesized values are negative. Trailing marker letters (`(1.0000)S`, `$4,454.21A`)
  are footnote flags, not part of the value.
- Statements vary by period: sections may be absent, options rows may wrap across several
  lines, and cost basis may be missing. Parse defensively and return `None` for absent
  data instead of substituting zero — a missing figure and a zero figure mean different
  things.
- Transaction Details rows have variable arity: some print only a quantity
  (`Other ExpiredLong KORU ... 1.0000`), some only an amount (`Interest CreditInterest
  ... 0.62`), some all nine columns. Match anchored per-shape patterns; "last number is
  the amount" books an expiration's contract count as cash.
- The realized column carries its term (`280.67(ST)`, `(188.66)(LT)`). Strip the
  `(ST)`/`(LT)` tag before converting, or the tag's parentheses read as a negative sign.
- Never scan continuation lines for numbers: a strike (`450.00P`) and a settle date
  (`08/03`) both look numeric. Pull option type, strike and expiry from them by pattern.
- The section after Transaction Details is headed `Pending / Open Activity`, not
  "Pending Transactions". Terminate the settled window on the real heading.

## Privacy

Statements contain account numbers, holdings, and a home address. Keep all data local:
never upload statements or parsed output to third-party services, and redact account
numbers and personal details from anything shared.

The web UI is bound to `0.0.0.0:9388` at the user's request, so the only thing between a
stranger and the statements is the `APP_PASSWORD` gate in `app.py`. Do not weaken or
remove that gate while the bind address stands. Traffic is plain HTTP: the password and
the statement data cross the network unencrypted, so keep the port firewalled or put a
TLS-terminating reverse proxy in front of it.
