#!/usr/bin/env python3
"""Postgres persistence for parsed Schwab statements.

Pure I/O: this module stores and rebuilds the record dictionaries that
``analyze_schwab.StatementParser.parse()`` produces. It performs no analytics —
every derived figure stays in ``analyze_schwab``.

The statements live in their own database (``SCHWAB_DB``, default
``schwab_statements``) inside schema ``schwab``. Databases that already exist on
the server are only ever read from ``pg_database``; nothing else is touched.

Raw PDF bytes and page text are never stored: they carry the account number and
a home address. Only parsed figures are persisted.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from datetime import date, datetime
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.types.json import Jsonb

import analyze_schwab as A

SCHEMA = "schwab"
PARSER_VERSION = "2026.08"

# Columns given a real type because analytics index them without guarding for
# absence: build_frame and build_report read them directly.
TYPED_KEYS = (
    "beginning_value",
    "ending_value",
    "deposits",
    "withdrawals",
    "dividends_interest",
    "market_appreciation",
    "expenses",
    "st_net",
    "lt_net",
    "unrealized",
    "dividends",
    "interest",
    "income_total",
    "liabilities",
    "alloc_total",
) + tuple(f"alloc_{name}" for name in A.CLASS_COLUMNS)

# The long tail. Stored in ``extra`` jsonb, but listed so a loaded record always
# has the same shape as a freshly parsed one.
EXTRA_KEYS = (
    "beginning_value_ytd",
    "buying_power",
    "cash_beginning",
    "cash_ending",
    "cost_basis",
    "deposits_ytd",
    "dividends_interest_ytd",
    "dividends_ytd",
    "ending_value_ytd",
    "expenses_ytd",
    "funds_available",
    "income_total_ytd",
    "interest_ytd",
    "lt_gain",
    "lt_loss",
    "lt_net_ytd",
    "margin_closing",
    "margin_opening",
    "market_appreciation_ytd",
    "pending_residual",
    "pending_total",
    "pos_beginning",
    "pos_cash_activity",
    "pos_div_reinvested",
    "pos_ending",
    "pos_market_change",
    "pos_transfers",
    "purchases",
    "sales",
    "st_gain",
    "st_loss",
    "st_net_ytd",
    "txn_amount_residual",
    "txn_deposits",
    "txn_dividends_interest",
    "txn_expenses",
    "txn_withdrawals",
    "unrealized_gain",
    "withdrawals_ytd",
)

IDENTITY_KEYS = ("file", "period_start", "period_end", "days", "holdings_count")
SCALAR_KEYS = IDENTITY_KEYS + TYPED_KEYS + EXTRA_KEYS

HOLDING_KEYS = (
    "asset_class",
    "symbol",
    "label",
    "description",
    "quantity",
    "price",
    "market_value",
    "cost_basis",
    "unrealized",
    "option_type",
    "strike",
    "expiry",
)
HOLDING_NUMERIC = ("quantity", "price", "market_value", "cost_basis", "unrealized", "strike")
FLOW_KEYS = ("date", "amount", "description")


def column_for(key: str) -> str:
    """``alloc_Fixed Income`` -> ``alloc_fixed_income``."""
    return re.sub(r"[^0-9a-z]+", "_", key.lower()).strip("_")


KEY_FOR_COLUMN = {column_for(key): key for key in TYPED_KEYS}


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------
def maintenance_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Put it in .env and start the app with ./run.sh."
        )
    return url


def database_name() -> str:
    return os.environ.get("SCHWAB_DB", "schwab_statements")


def target_url() -> str:
    parts = urlsplit(maintenance_url())
    return urlunsplit(parts._replace(path=f"/{database_name()}"))


def connect() -> psycopg.Connection:
    """Open a connection to the statement database."""
    return psycopg.connect(target_url())


def ensure_database() -> bool:
    """Create the statement database if absent. Returns True when created.

    Runs against the maintenance database in autocommit mode because
    CREATE DATABASE cannot run inside a transaction. Existing databases are
    only read from pg_database.
    """
    name = database_name()
    with psycopg.connect(maintenance_url(), autocommit=True) as conn:
        exists = conn.execute(
            "select 1 from pg_database where datname = %s", (name,)
        ).fetchone()
        if exists:
            return False
        conn.execute(f'create database "{name}"')
    return True


def ensure_schema(conn: psycopg.Connection) -> None:
    typed = ",\n            ".join(
        f"{column_for(key)} double precision" for key in TYPED_KEYS
    )
    with conn.cursor() as cur:
        cur.execute(f"create schema if not exists {SCHEMA}")
        cur.execute(
            f"""
            create table if not exists {SCHEMA}.statements (
            id bigserial primary key,
            sha256 text not null unique,
            file text not null,
            period_start date not null,
            period_end date not null,
            days integer,
            holdings_count integer,
            parser_version text,
            created_at timestamptz not null default now(),
            {typed},
            extra jsonb not null default '{{}}'::jsonb,
            class_totals jsonb not null default '{{}}'::jsonb
            )
            """
        )
        cur.execute(
            f"""
            create table if not exists {SCHEMA}.holdings (
                id bigserial primary key,
                statement_id bigint not null
                    references {SCHEMA}.statements(id) on delete cascade,
                asset_class text,
                symbol text,
                label text,
                description text,
                quantity numeric(20,4),
                price numeric(20,4),
                market_value numeric(20,4),
                cost_basis numeric(20,4),
                unrealized numeric(20,4),
                option_type text,
                strike numeric(20,4),
                expiry date
            )
            """
        )
        cur.execute(
            f"""
            create table if not exists {SCHEMA}.transactions (
                id bigserial primary key,
                statement_id bigint not null
                    references {SCHEMA}.statements(id) on delete cascade,
                settled boolean not null default true,
                trade_date date,
                settle_date date,
                category text,
                action text,
                symbol text,
                description text,
                quantity numeric(20,4),
                price numeric(20,4),
                charges numeric(20,4),
                amount numeric(20,4),
                realized numeric(20,4),
                term text,
                is_option boolean not null default false,
                option_type text,
                strike numeric(20,4),
                expiry date
            )
            """
        )
        cur.execute(
            f"""
            create table if not exists {SCHEMA}.cash_flows (
                id bigserial primary key,
                statement_id bigint not null
                    references {SCHEMA}.statements(id) on delete cascade,
                date date,
                amount numeric(20,4),
                description text
            )
            """
        )
        cur.execute(
            f"create index if not exists holdings_statement_idx "
            f"on {SCHEMA}.holdings (statement_id)"
        )
        cur.execute(
            f"create index if not exists holdings_symbol_idx on {SCHEMA}.holdings (symbol)"
        )
        cur.execute(
            f"create index if not exists transactions_statement_idx "
            f"on {SCHEMA}.transactions (statement_id)"
        )
        cur.execute(
            f"create index if not exists transactions_trade_date_idx "
            f"on {SCHEMA}.transactions (trade_date)"
        )
        cur.execute(
            f"create index if not exists transactions_symbol_idx "
            f"on {SCHEMA}.transactions (symbol)"
        )
        cur.execute(
            f"create index if not exists transactions_option_idx "
            f"on {SCHEMA}.transactions (is_option, settled)"
        )
        cur.execute(
            f"create index if not exists cash_flows_statement_idx "
            f"on {SCHEMA}.cash_flows (statement_id)"
        )
    conn.commit()


def initialize() -> bool:
    created = ensure_database()
    with connect() as conn:
        ensure_schema(conn)
    return created


# --------------------------------------------------------------------------
# coercion helpers
# --------------------------------------------------------------------------
def statement_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(value):
    """None for absent or non-finite values; a plain float otherwise.

    NaN must never reach jsonb, and Decimal must never reach pandas.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def as_date(value):
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def save_record(conn: psycopg.Connection, record: dict, sha256: str) -> int:
    """Upsert one statement and replace its child rows. Idempotent per sha256."""
    typed_columns = [column_for(key) for key in TYPED_KEYS]
    typed_values = [clean(record.get(key)) for key in TYPED_KEYS]

    extra = {
        key: clean(record[key]) for key in EXTRA_KEYS if key in record
    }
    class_totals = {
        name: {inner: clean(value) for inner, value in totals.items()}
        for name, totals in (record.get("_class_totals") or {}).items()
    }

    columns = [
        "sha256",
        "file",
        "period_start",
        "period_end",
        "days",
        "holdings_count",
        "parser_version",
        *typed_columns,
        "extra",
        "class_totals",
    ]
    values = [
        sha256,
        record.get("file"),
        as_date(record.get("period_start")),
        as_date(record.get("period_end")),
        record.get("days"),
        record.get("holdings_count"),
        PARSER_VERSION,
        *typed_values,
        Jsonb(extra),
        Jsonb(class_totals),
    ]
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in columns if name != "sha256"
    )
    placeholders = ", ".join(["%s"] * len(values))

    with conn.cursor() as cur:
        cur.execute(
            f"insert into {SCHEMA}.statements ({', '.join(columns)}) "
            f"values ({placeholders}) "
            f"on conflict (sha256) do update set {updates}, created_at = now() "
            f"returning id",
            values,
        )
        statement_id = cur.fetchone()[0]

        for table in ("holdings", "transactions", "cash_flows"):
            cur.execute(
                f"delete from {SCHEMA}.{table} where statement_id = %s", (statement_id,)
            )

        holdings = [
            [statement_id]
            + [
                clean(holding.get(key))
                if key in HOLDING_NUMERIC
                else as_date(holding.get(key))
                if key == "expiry"
                else holding.get(key)
                for key in HOLDING_KEYS
            ]
            for holding in record.get("_holdings") or []
        ]
        if holdings:
            cur.executemany(
                f"insert into {SCHEMA}.holdings "
                f"(statement_id, {', '.join(HOLDING_KEYS)}) "
                f"values ({', '.join(['%s'] * (len(HOLDING_KEYS) + 1))})",
                holdings,
            )

        rows = list(record.get("_transactions") or []) + list(record.get("_pending") or [])
        transactions = [
            [statement_id]
            + [
                clean(row.get(key))
                if key in A.TXN_NUMERIC
                else as_date(row.get(key))
                if key in ("trade_date", "settle_date", "expiry")
                else bool(row.get(key))
                if key in ("settled", "is_option")
                else row.get(key)
                for key in A.TXN_COLUMNS
            ]
            for row in rows
        ]
        if transactions:
            cur.executemany(
                f"insert into {SCHEMA}.transactions "
                f"(statement_id, {', '.join(A.TXN_COLUMNS)}) "
                f"values ({', '.join(['%s'] * (len(A.TXN_COLUMNS) + 1))})",
                transactions,
            )

        flows = [
            [statement_id, as_date(flow.get("date")), clean(flow.get("amount")),
             flow.get("description")]
            for flow in record.get("_flows") or []
        ]
        if flows:
            cur.executemany(
                f"insert into {SCHEMA}.cash_flows "
                f"(statement_id, date, amount, description) values (%s, %s, %s, %s)",
                flows,
            )
    conn.commit()
    return statement_id


def delete_statement(conn: psycopg.Connection, sha256: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"delete from {SCHEMA}.statements where sha256 = %s", (sha256,))
        removed = cur.rowcount
    conn.commit()
    return bool(removed)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def known_digests(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(f"select sha256 from {SCHEMA}.statements")
        return {row[0] for row in cur.fetchall()}


def statement_index(conn: psycopg.Connection) -> list[dict]:
    """One row per stored statement, newest first. For the sidebar inventory."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select sha256, file, period_start, period_end, ending_value, created_at,
                   (select count(*) from {SCHEMA}.transactions t
                     where t.statement_id = s.id) as transaction_rows
              from {SCHEMA}.statements s
             order by period_end desc, created_at desc
            """
        )
        columns = [description.name for description in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _statement_rows(conn: psycopg.Connection) -> list[dict]:
    # Two files can cover one period; counting both would double the month.
    # Keep the most recently saved statement per period.
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select distinct on (period_end) *
              from {SCHEMA}.statements
             order by period_end, created_at desc
            """
        )
        columns = [description.name for description in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _children(conn: psycopg.Connection, table: str, columns) -> dict:
    grouped: dict[int, list[dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            f"select statement_id, {', '.join(columns)} from {SCHEMA}.{table} order by id"
        )
        for row in cur.fetchall():
            grouped.setdefault(row[0], []).append(dict(zip(columns, row[1:])))
    return grouped


def load_records(conn: psycopg.Connection) -> list[dict]:
    """Rebuild parser-shaped record dicts, oldest period first.

    Every SCALAR_KEYS entry is present (None when never parsed), money comes
    back as float rather than Decimal, and dates are ``datetime.date`` — the
    analytics in ``analyze_schwab`` assume all three.
    """
    statements = _statement_rows(conn)
    if not statements:
        return []

    holdings = _children(conn, "holdings", HOLDING_KEYS)
    transactions = _children(conn, "transactions", A.TXN_COLUMNS)
    flows = _children(conn, "cash_flows", FLOW_KEYS)

    records = []
    for row in statements:
        record = {key: None for key in SCALAR_KEYS}
        record["file"] = row["file"]
        record["period_start"] = as_date(row["period_start"])
        record["period_end"] = as_date(row["period_end"])
        record["days"] = None if row["days"] is None else int(row["days"])
        record["holdings_count"] = (
            None if row["holdings_count"] is None else int(row["holdings_count"])
        )
        for key in TYPED_KEYS:
            record[key] = clean(row.get(column_for(key)))
        for key, value in (row.get("extra") or {}).items():
            record[key] = clean(value)

        record["_class_totals"] = {
            name: {inner: clean(value) for inner, value in totals.items()}
            for name, totals in (row.get("class_totals") or {}).items()
        }

        record["_holdings"] = [
            {
                key: clean(holding[key])
                if key in HOLDING_NUMERIC
                else as_date(holding[key])
                if key == "expiry"
                else holding[key]
                for key in HOLDING_KEYS
            }
            for holding in holdings.get(row["id"], [])
        ]
        record["_flows"] = [
            {
                "date": as_date(flow["date"]),
                "amount": clean(flow["amount"]),
                "description": flow["description"],
            }
            for flow in flows.get(row["id"], [])
        ]

        settled, pending = [], []
        for raw in transactions.get(row["id"], []):
            entry = {
                key: clean(raw[key])
                if key in A.TXN_NUMERIC
                else as_date(raw[key])
                if key in ("trade_date", "settle_date", "expiry")
                else raw[key]
                for key in A.TXN_COLUMNS
            }
            (settled if entry["settled"] else pending).append(entry)
        record["_transactions"] = settled
        record["_pending"] = pending

        # Underscore-prefixed so build_frame does not turn it into a column.
        record["_sha256"] = row["sha256"]
        records.append(record)
    return records


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Statement database utilities")
    parser.add_argument(
        "command", choices=["initdb", "inventory"], help="initdb creates the database and schema"
    )
    args = parser.parse_args(argv)

    if args.command == "initdb":
        created = initialize()
        name = database_name()
        print(f"database {name}: {'created' if created else 'already present'}")
        print(f"schema {SCHEMA}: ready")
        return 0

    with connect() as conn:
        rows = statement_index(conn)
    if not rows:
        print("No statements stored.")
        return 0
    print(f"{len(rows)} statement(s) stored:")
    for row in rows:
        print(
            f"  {row['period_start']} to {row['period_end']}  "
            f"{A.fmt_money(clean(row['ending_value']))}  "
            f"{row['transaction_rows']} transaction rows  {row['file']}"
        )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
