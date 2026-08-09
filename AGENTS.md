# Charles Schwab Statement Analysis

Tracks a Schwab brokerage account from two feeds: trade confirmation emails, which arrive
the day after each fill and are the primary source of trades, and monthly statement PDFs,
which are the printed record everything else is audited against.

## Layout

- `*.PDF` — monthly Schwab statements. There may be none on disk: statements uploaded
  through the web UI live only in Postgres, so the CLI must be able to work from the
  database (`analyze --from-db`).
- `schwab/` — the package. `python3 -m schwab <command>` is the entry point, so cron needs
  no install step and there is no `pyproject.toml`.
  - `domain.py` — shared vocabulary: column lists, sign conventions, `occ_symbol()`,
    formatters. Stdlib only, so `store.py` and `notify.py` import it without pulling in
    pandas or the PDF stack.
  - `text.py` — token and date primitives both parsers need.
  - `statements.py` — `StatementParser`. Accepts a path or a binary file-like object, so
    the CLI and the uploader feed the same parser.
  - `confirms/parse.py` — pure `str -> list[trade dict]`. No network, no database.
  - `confirms/mailbox.py` — read-only IMAP fetch.
  - `confirms/ingest.py` — one ingestion pass, wired for cron.
  - `analytics.py` — Modified Dietz, IRR, frames, metrics, attribution, premium.
  - `positions.py` — statement-anchored rollforward.
  - `reconcile.py` — statement rows versus the confirm feed.
  - `quotes.py` — the only module that touches the market.
  - `charts.py` — Plotly figures. `report.py` — plain-text report. `notify.py` — SMTP.
  - `store.py` — Postgres persistence. Pure I/O, no analytics.
  - `cli.py` / `__main__.py` — `analyze|ingest|positions|quotes|reconcile|initdb|inventory`.
- `app.py` — Streamlit web UI. It imports the package directly and must never reimplement
  parsing or analytics.
- `run.sh` — process control and the cron entry point. It sources `.env` so `DATABASE_URL`,
  `APP_PASSWORD` and the Gmail credentials never appear on a command line.
- `.env` — local secrets, `chmod 600`, never committed. Holds `DATABASE_URL`, `SCHWAB_DB`,
  `APP_PASSWORD`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `GMAIL_TO`.
- `.streamlit/config.toml` — binds the server to `0.0.0.0:9388`. The app is reachable
  over the network, so `app.py` refuses to render anything without `APP_PASSWORD`.
- `output/` — generated artifacts (CSV, JSON, report, `charts/`). Disposable; regenerate
  rather than editing by hand.

## Commands

```bash
pip3 install -r requirements.txt

# CLI
python3 -m schwab analyze                    # parse ./*.PDF -> ./output
python3 -m schwab analyze --from-db          # analyze what is already stored
python3 -m schwab analyze --dir path --out path --rf 0.03
python3 -m schwab analyze --verbose          # show unparsed statement lines
python3 -m schwab analyze --save-db          # also persist each statement
python3 -m schwab ingest [--days 7] [--dry-run] [--reprocess] [--no-notify]
python3 -m schwab positions | quotes | reconcile

# Web UI on port 9388, password from .env
./run.sh initdb          # create the database and schema once
./run.sh start|restart|stop|status|logs
./run.sh inventory       # what is stored
./run.sh ingest          # pull new confirmations, under flock
./run.sh cron-install    # print the crontab line (never installs it silently)
```

The every-five-minute entry is already installed in the user's crontab; `cron-install`
prints it for reinstalling or checking against what is live.

## Two feeds, one direction of trust

Confirmations are the trade feed; statements are the audit. `reconcile.py` compares them
and every status it can emit means something specific:

- `matched` — the confirmation and the statement row agree.
- `amount_mismatch` — they pair up but the price or amount differs beyond tolerance
  ($0.005 on price, $0.02 on amount for fee rounding).
- `statement_only` — the statement prints a trade no confirmation produced: a gap in
  ingestion, or an email that never arrived.
- `confirm_only` — a confirmation the statement does not print: a statement parser miss.
- `not_confirmable` — dividends, interest, NRA tax, fees, journals and Other Activity
  expirations. Schwab issues no confirmation for these, so they are partitioned out
  before comparing. Skip that filter and every dividend reads as a missing confirmation.
- `before_confirm_feed` — traded before the earliest confirmation held, so not auditable.

Both sides of a period are compared: the settled Transaction Details rows **and** the
Pending / Open Activity rows. A trade that fills in the last days of a period settles
after the statement closes, so it prints only as pending. Compare settled rows alone and
every such trade reads as `confirm_only` — a parser miss that is not one. A matched row
sourced from the pending section says so in its note.

Confirmations carry no order or trade ID, and
`trade_date+symbol+strike+expiry+action+quantity+price` is genuinely non-unique — selling
the same contract twice in one day at the same price is ordinary wheel behaviour. So
dedupe is **per email, not per trade**: `unique (email_id, seq)`, and emails are keyed on
`body_sha256` of the URL-stripped body rather than Message-ID, because forwarding rewrites
Message-ID and two forwards of one confirmation would double every trade.

## Shared architecture

Charts are built once by `build_charts()`, which returns `[(slug, figure)]` of Plotly
figures — slugs carry no file extension. The CLI writes each as standalone HTML with
`include_plotlyjs="directory"`, so a shared local `plotly.min.js` is emitted and no page
calls a CDN; the app renders the same figures with `st.plotly_chart`. Add or change a
chart in `build_charts()` only. `kaleido` is not installed, so there is no static image
export — do not add one without installing it.

## Storage

`DATABASE_URL` points at the server's *maintenance* database. `store.py` creates its own
database (`SCHWAB_DB`, default `schwab_statements`) and works only inside schema
`schwab`. Databases that already exist on the server are read from `pg_database` and
never written to; do not add code that writes outside `SCHWAB_DB`. `postgres` and
`stock_watcher` on this host belong to other applications and are actively written by
them, so verify isolation by comparing **table structure**, never row counts.

Statement tables: `statements` (one row per PDF, keyed by the file's `sha256`), plus
`holdings`, `transactions` and `cash_flows` referencing it with `on delete cascade`.
Settled and pending activity share the `transactions` table and are told apart by the
`settled` flag. Saving is an upsert on `sha256`, so re-uploading a PDF replaces its rows
instead of duplicating them.

Confirmation tables: `emails` -> `trades`, plus a `quotes` cache. These are deliberately
**not** children of `statements`: `transactions.statement_id` is `not null ... on delete
cascade` and a statement's child rows are deleted and reinserted on every re-parse, so a
trade stored under that lifecycle would vanish when its PDF was re-uploaded. Email bodies
are never stored — they carry the account tail and the address — and IMAP stays the
archive.

A `settings` key/value table holds the assumptions the UI can change, currently only
`risk_free`. It defaults to `domain.DEFAULT_RISK_FREE` (3%), the app writes it back when
the sidebar value changes, and `analyze` reads it when `--rf` is absent so the CLI and the
web UI never disagree on the rate. It feeds Sharpe and Sortino only, so a missing or
malformed value falls back to the default rather than failing.

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
- Marks from `quotes.py` are delayed third-party quotes, not statement figures. They may
  price positions and show unrealized change, and they must never enter TWR, IRR, Sharpe
  or any figure that has to tie back to a statement.
- A symbol that cannot be priced stays `None` — never zero, never the last statement
  price. Any total containing an unpriced leg reports "n/a (incomplete)", because a
  silently short sum understates short-option liability.

## Confirmation parser rules

- `Type: Short` prints on **both** the Sale and the Purchase side of a short option and
  says nothing about whether a position was opened or closed. Take intent from the
  Additional-information note ("sold to open" -> `open`, "Bought to close your short
  option position" -> `close`) and record `null` when the note is absent. The cash sign
  comes from `Action:` and is independent of intent.
- A confirmation too long for one email is **split**, and the continuation part is a
  different layout, not a corrupt one: it prints one table cell per line, dates its header
  `20260807` instead of `08/07/2026`, prints no account line (so `account_tail` is
  legitimately null), pads the price to nine decimals, and repeats the `Symbol:` header to
  print each trade's Additional-information notes as its own block after all the table
  blocks. Label patterns work across both because `\s*` spans newlines. The amounts row
  cannot: it is anchored to the `Total Amount` column header, because letting whitespace
  span newlines without that anchor lets the Commission and Total rows below match the
  same shape. The notes blocks carry the intent for a trade parsed earlier, so
  `merge_note` attaches them by symbol, strike, type and CUSIP; a note matching no trade
  is still reported, since a genuinely mis-parsed trade block looks identical.
- `parse_confirm` returns `failed` as `{"seq", "error"}` dicts, not strings. Format them
  before joining or putting them in `outcome["failures"]` — the notification renders that
  list as text and a dict there aborts the whole ingestion pass as a "database error".
- The quantity delta follows from `Action:` alone: a sale is `-qty` whether it opens a
  short or closes a long. Intent affects how cost basis is attributed, not direction, so
  a missing intent flags the position rather than discarding a known delta.
- Strip the `click.mail.schwab.com` tracking URLs before parsing and before hashing. They
  differ per send, so they would break body dedupe, they are tracking tokens not worth
  storing, and a `qs=` token can otherwise be read as a number.
- Two-digit years on `Trade Date` expand against the email's own confirmation date, never
  against today.
- Assert the printed arithmetic before storing: `principal == quantity x price x 100` for
  options, and `net == principal -/+ charges`. A block that disagrees goes to `failed`
  with its reason instead of being written.
- Build OCC symbols with `Decimal`, never float. Float rounding on a strike produces a
  different but entirely valid-looking contract.

## What the confirmation feed cannot know

`positions.py` publishes positions and their marked value only — never a rolled-forward
account value, TWR or risk statistic. Confirmations carry no cash balance, margin,
dividend, transfer or corporate-action information.

`interim_performance()` is what keeps performance current between statements: the change in
position value from the statement's printed values to today's marks, plus the cash the
confirmations printed. It is **position-level profit and loss, not a return** — with no cash
balance or deposit history there is no denominator to compute one — so it must never be
linked into the monthly TWR series, and it reads n/a whenever a leg is unpriced.

The rollforward treats the statement as authoritative through its period end and so applies
only trades dated after it. The anchor is therefore holdings **plus** the Pending / Open
Activity rows: those trades filled inside the period but settle after it, so the holdings
section and the account value both omit them while the exposure is already real, and a
pending cover would otherwise leave a closed short showing as open. Each pending row is
valued at its own trade price, because the cash it produced sits outside the account value
too — the two cancel, and only price movement after the statement shows as a change.

When a confirmation on or before the period end appears in neither the settled rows nor the
pending section, the trade lands in neither view. That gap is flagged in the Reconciliation
tab rather than merged in, because a confirmation absent from the statement may equally be a
parser miss or a trade Schwab printed in the next period, and guessing would double-count.

Schwab sends no confirmation for an expiration, and **an expiration cannot be told apart
from an assignment**. An assigned short put silently creates 100 long shares and a debit
that appears in no email. Options past their expiry are therefore dropped as
`assumed_expired_worthless` and carry that warning in the UI and the report. Surface these
limits rather than burying them; the next statement is what resolves them.

Statement option holdings print the expiry as `MM/DD` with no year, so `positions.py`
infers it as the earliest year at or after the statement period end on which options could
actually expire, tested by `domain.is_expiry_day`. Expirations fall on Fridays and shift
back to Thursday when that Friday is a market holiday, which is why the test needs a
holiday set rather than a weekday check — `06/18/2026` and `07/02/2026` are genuine
Thursday expiries behind Juneteenth and the observed Independence Day.

Reading the plain next occurrence instead is wrong and was caught in practice:
`SCHD 35 C 01/21` on the 2026-07-31 statement resolved to 2027-01-21, a Thursday with no
holiday behind it and therefore not an expiration at all. The contract is the January 2028
LEAPS, `SCHD280121C00035000`, which Yahoo marks at $1.75 against the statement's printed
$1.7468. A wrong year yields a different but entirely valid-looking contract — the same
hazard as rounding a strike — and it silently poisons every marked total, so keep the
expiry-day test in place. When no candidate year is justifiable the nearest one is returned
anyway, leaving the position visible and unpriced rather than vanishing.

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
- A pending row's continuation scan must stop at `TotalPendingTransactions` / `OpenOrders`.
  The terminator sits directly under the last row, and `compact()` glues it to the strike
  (`45.00PTotalPendingTransactions$1,286.33`), which kills the word boundary the strike
  pattern needs — the last pending contract of every statement would lose its strike, and
  with it its OCC symbol, its quote and its match to a confirmation.
- The section after Transaction Details is headed `Pending / Open Activity`, not
  "Pending Transactions". Terminate the settled window on the real heading.

## Ingestion and notifications

`./run.sh ingest` is meant for cron every five minutes and runs under `flock` so a slow
IMAP round trip cannot overlap the next tick. It pipes the run's output through a
timestamp prefix, because cron appends every tick to one `.run/ingest.log` and an
untimestamped summary line cannot be told from the tick before it; `pipefail` keeps
ingest's own exit code reaching cron. `mailbox.py` selects the folder **readonly**
and never deletes, moves or flags mail; the mailbox holds thousands of unrelated personal
messages, so the search stays narrow (a `SINCE` floor plus a subject match). The subject is
the anchor rather than the From address, because both the direct Schwab mail and the
forwarded copy have to match.

Transient network failure is not news. A single failed DNS lookup for `imap.gmail.com`
used to mail a full `INGEST FAILED`, and the tick five minutes later succeeded — so the
IMAP fetch and the SMTP send both run through `domain.retry`, five attempts with a linear
2/4/6/8-second backoff, and only the fifth failure is reported. Each swallowed attempt is
still printed, so the log shows what happened rather than hiding it. The total wait has to
stay near those 20 seconds: `flock -n` is **non-blocking**, so a pass that lingers does not
delay the next tick, it makes that tick skip. `MailboxError` carries its own `reason`
(`imap unreachable`, `imap login`, `imap search`, `gmail credentials missing`) and the
subject uses it, because labelling an unresolved host as a rejected login sends the reader
after the app password for a network outage. Parsing and the database pass are deliberately
**not** retried: a parse failure is deterministic, and the ingestion loop's counters are
incremented as it goes, so replaying it would double what it reports.

A notification fires for every confirmation *processed* — success, partial or failure — and
stays **silent when a tick finds nothing new**. That exception is deliberate: 288 ticks a
day of "nothing happened" would train the notification to be muted, defeating its purpose.
Subjects are short enough to read whole on a lock screen (`Schwab: 5 trades, +$1,093.67
credit`, `Schwab: INGEST FAILED - imap login`); detail goes in the body. The mail is
multipart/alternative and both parts have to tell the whole story, because which one a
reader sees is not ours to choose. `html_for()` is the preferred part and puts the trades
in a table (action, contract, expiry, quantity, price, cash); `body_for()` is the text
fallback that drives the preview pane, and it stays prose, not a field dump:
`notify.describe()` turns each trade into a sentence ("Sold to open 1 SOXL $104 put
expiring Aug 28 at $8.00 - credit $799.32"). The two renderings share `_verb()` and
`_contract()` so they cannot drift into describing the same trade differently, a trade
whose intent is unknown is marked in both rather than shown as a bare "Sold", and a
failure says what broke and what to check. Styles are inline because mail clients strip
stylesheets, and every interpolated value is escaped — it all comes from email text. It is
read on a phone by a person, so keep it that way. A notification failure is
logged and never masks the ingestion result, and ingest exits non-zero on failure so cron's
own mail is a backstop if SMTP is what broke.

## Privacy

Statements contain account numbers, holdings, and a home address. Keep all data local:
never upload statements or parsed output to third-party services, and redact account
numbers and personal details from anything shared.

`quotes.py` is the one exception, and a narrow one: yfinance sends ticker and OCC contract
symbols to Yahoo, which reveals what is held but not the account, the balances or the
address. It is a real outbound flow — `--no-quotes` on the CLI switches it off, and it must
stay optional.

The web UI is bound to `0.0.0.0:9388` at the user's request, so the only thing between a
stranger and the statements is the `APP_PASSWORD` gate in `app.py`. Do not weaken or
remove that gate while the bind address stands. Traffic is plain HTTP: the password and
the statement data cross the network unencrypted, so keep the port firewalled or put a
TLS-terminating reverse proxy in front of it.

`GMAIL_APP_PASSWORD` is a Gmail app password with full IMAP and SMTP reach over a personal
mailbox. It lives only in `.env` (`chmod 600`) and must never be logged, echoed or passed
on a command line.
