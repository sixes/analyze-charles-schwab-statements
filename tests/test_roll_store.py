"""Roll-history tests; PostgreSQL tests start only a private disposable cluster.

Run: python3 -B -m unittest discover -s tests -p test_roll_store.py -v
No application connection helper, environment credentials or running server is used.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Barrier
import unittest
from unittest.mock import MagicMock, Mock

import psycopg

from schwab import store


POSITION = "TEST260918P00100000"
CANDIDATE = "TEST261016P00095000"
OTHER_CANDIDATE = "TEST261016P00090000"
OTHER_POSITION = "TEST260918C00100000"
AT = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)


def alert(**changes):
    return {
        "key": POSITION,
        "candidate_key": CANDIDATE,
        "reference_basis": Decimal("-123.4567"),
        "quantity": Decimal("-1"),
        "display_only": "not stored",
        **changes,
    }


def position(**changes):
    return {
        "key": POSITION,
        "status": "open",
        "is_option": True,
        "expiry": date(2026, 9, 18),
        "cost_basis": Decimal("-123.4567"),
        "quantity": Decimal("-1"),
        **changes,
    }


def compact(sql):
    return " ".join(sql.lower().split())


class RollStoreMockTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.cur = self.conn.cursor.return_value.__enter__.return_value

    def executed(self):
        return [(compact(call.args[0]), call.args[1:])
                for call in self.cur.execute.call_args_list]

    def test_schema_is_separate_and_has_lifetime_identity(self):
        store.ensure_schema(self.conn)
        definitions = [sql for sql, _ in self.executed()
                       if sql.startswith("create table")]
        roll = next(sql for sql in definitions if "schwab.roll_alerts" in sql)
        self.assertIn("id bigserial primary key", roll)
        for field in ("reference_basis", "quantity"):
            self.assertIn(f"{field} numeric(20,4) not null", roll)
        self.assertIn("created_at timestamptz not null", roll)
        self.assertIn("unique (position_key, candidate_key, reference_basis, quantity)", roll)
        self.assertNotIn("session_date", roll)
        self.assertTrue(any("schwab.alerts (" in sql for sql in definitions))
        self.conn.commit.assert_called_once_with()

    def test_numeric_rounding_matches_postgres_not_decimal_default(self):
        for value, expected in [("-1.00005", "-1.0001"), ("1.00005", "1.0001"),
                                ("-1.00004", "-1.0000"), ("0", "0.0000"),
                                ("9999999999999999.9999", "9999999999999999.9999"),
                                ("-9999999999999999.9999", "-9999999999999999.9999")]:
            with self.subTest(value=value), localcontext() as ctx:
                ctx.prec = 6
                self.assertEqual(store._roll_numeric(Decimal(value)), Decimal(expected))
        for value in (None, True, "NaN", "Infinity", "-Infinity", "broken", "1e16",
                      "9999999999999999.99995"):
            with self.subTest(value=value):
                self.assertIsNone(store._roll_numeric(value))

    def test_invalid_claims_fail_before_any_database_work(self):
        for changes in ({"quantity": Decimal("-1.00001")}, {"quantity": 0},
                        {"quantity": None}, {"reference_basis": Decimal("NaN")},
                        {"reference_basis": None}, {"key": ""}, {"candidate_key": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                store.claim_roll_alerts(self.conn, [alert(), alert(**changes)])
        self.conn.cursor.assert_not_called()
        self.conn.transaction.assert_not_called()
        self.conn.commit.assert_not_called()

    def test_missing_table_preview_is_select_only_and_aligned(self):
        self.cur.fetchone.return_value = (None,)
        self.assertEqual(store.roll_alert_states(self.conn, [alert(), alert()]),
                         ["would send", "would send"])
        self.cur.execute.assert_called_once_with(
            "select to_regclass(%s)", ("schwab.roll_alerts",))
        self.conn.commit.assert_not_called()
        self.conn.transaction.assert_not_called()

    def test_preview_preserves_order_and_normalizes_signed_basis(self):
        self.cur.fetchone.side_effect = [("schwab.roll_alerts",), ("cooldown",),
                                         ("already reported",), ("would send",)]
        candidate = alert(reference_basis=Decimal("-100.00005"))
        self.assertEqual(store.roll_alert_states(self.conn, [candidate, alert(), alert()]),
                         ["cooldown", "already reported", "would send"])
        calls = self.cur.execute.call_args_list
        self.assertEqual(calls[1].args[1],
                         (POSITION, CANDIDATE, Decimal("-100.0001"), Decimal("-1"), POSITION))
        self.assertTrue(all(sql.startswith("select ") for sql, _ in self.executed()))
        self.assertTrue(all("pg_advisory" not in sql for sql, _ in self.executed()))
        self.conn.commit.assert_not_called()
        self.conn.transaction.assert_not_called()

    def test_claim_locks_sorted_positions_then_inserts_and_returns_copies(self):
        alerts = [alert(key="Z"), alert(key="A"), alert(key="Z")]
        self.cur.fetchone.side_effect = [(41,), (42,), None]
        result = store.claim_roll_alerts(self.conn, alerts)
        self.assertEqual([row["claim_id"] for row in result], [41, 42])
        self.assertTrue(all("claim_id" not in row for row in alerts))
        self.assertIsNot(result[0], alerts[0])
        calls = self.cur.execute.call_args_list
        for index, key in enumerate(("A", "Z")):
            digest = hashlib.sha256(f"schwab.roll_alerts:{key}".encode()).digest()
            self.assertEqual(calls[index].args,
                             ("select pg_advisory_xact_lock(%s::bigint)",
                              (int.from_bytes(digest[:8], "big", signed=True),)))
        for call in calls[2:]:
            sql, params = call.args
            self.assertIn("on conflict (position_key, candidate_key, reference_basis, quantity)",
                          compact(sql))
            self.assertIn("created_at > statement_timestamp() - interval '24 hours'", sql)
            self.assertEqual(params[:4], params[4:])
            self.assertNotIn("not stored", params)
        self.conn.transaction.assert_called_once_with()
        self.conn.commit.assert_called_once_with()
        self.assertEqual(self.conn.mock_calls[-1].args, ())
        self.assertEqual(self.conn.mock_calls[-1][0], "commit")

    def test_failed_insert_does_not_commit(self):
        self.cur.execute.side_effect = [None, RuntimeError("insert failed")]
        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            store.claim_roll_alerts(self.conn, [alert()])
        self.conn.commit.assert_not_called()
        self.assertIs(self.conn.transaction.return_value.__exit__.call_args.args[0],
                      RuntimeError)

    def test_cleanup_uses_full_book_and_retains_uncertain_rows(self):
        self.cur.fetchall.return_value = [("absent",), ("expired",), ("uncertain",),
                                          ("missing_basis",), ("open",)]
        store.cleanup_roll_alerts(self.conn, [
            position(key="expired", status="assumed_expired_worthless"),
            position(key="uncertain", status="uncertain"),
            position(key="missing_basis", cost_basis=None),
            position(key="open", price=None),
        ])
        deletes = [(sql, params[0]) for sql, params in self.executed()
                   if sql.startswith("delete")]
        self.assertEqual([params[0] for _, params in deletes], ["absent", "expired", "open"])
        self.assertIn("reference_basis <> %s or quantity <> %s", deletes[-1][0])
        self.assertIn("created_at <= statement_timestamp() - interval '24 hours'", deletes[-1][0])
        self.assertNotIn("price", deletes[-1][0])
        self.conn.commit.assert_called_once_with()

    def test_drop_uses_only_exact_ids(self):
        store.drop_roll_alerts(self.conn, [alert(claim_id=11), {"claim_id": 29}])
        self.cur.executemany.assert_called_once_with(
            "delete from schwab.roll_alerts where id = %s", [(11,), (29,)])
        self.conn.commit.assert_called_once_with()

    def test_drop_without_id_never_falls_back_to_identity(self):
        with self.assertRaises(KeyError):
            store.drop_roll_alerts(self.conn, [alert()])
        self.conn.cursor.assert_not_called()
        self.conn.commit.assert_not_called()

    def test_empty_claim_preview_drop_do_nothing(self):
        self.assertEqual(store.claim_roll_alerts(self.conn, []), [])
        self.assertEqual(store.roll_alert_states(self.conn, []), [])
        self.assertIsNone(store.drop_roll_alerts(self.conn, []))
        self.conn.cursor.assert_not_called()
        self.conn.commit.assert_not_called()


class FixedClock:
    """Execute real PostgreSQL SQL, replacing ONLY its clock for exact edge tests."""
    def __init__(self, conn, at):
        self.conn, self.at = conn, at

    def __getattr__(self, name):
        return getattr(self.conn, name)

    @contextmanager
    def cursor(self):
        with self.conn.cursor() as cur:
            proxy = Mock(wraps=cur)
            def execute(sql, params=None):
                timestamp = f"'{self.at.isoformat()}'::timestamptz"
                return cur.execute(sql.replace("statement_timestamp()", timestamp), params)
            proxy.execute.side_effect = execute
            yield proxy


def postgres_bin():
    executable = shutil.which("initdb")
    candidates = ([Path(executable).parent] if executable else [])
    candidates += list(Path("/usr/lib/postgresql").glob("*/bin"))
    candidates += list(Path("/usr").glob("pgsql-*/bin"))
    candidates += [Path("/usr/local/pgsql/bin")]
    return next((path for path in candidates
                 if all((path / name).is_file() for name in ("initdb", "pg_ctl", "postgres"))), None)


class RollStorePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binaries = postgres_bin()
        if binaries is None or os.geteuid() == 0:
            raise unittest.SkipTest("Disposable PostgreSQL needs server binaries and a non-root user")
        cls.temp = tempfile.TemporaryDirectory(prefix="schwab-roll-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.data = root / "data"
        cls.pg_ctl = binaries / "pg_ctl"
        cls.socket = root / "socket"
        cls.socket.mkdir(mode=0o700)
        subprocess.run([str(binaries / "initdb"), "-D", str(cls.data), "--no-locale",
                        "--encoding=UTF8", "-A", "trust", "-U", "roll_test"],
                       check=True, capture_output=True, text=True, timeout=30)
        cls.addClassCleanup(cls.stop_cluster)
        # No TCP listener, no inherited connection URL, and a private socket path.
        options = (f"-c listen_addresses='' -c unix_socket_directories='{cls.socket}' "
                   "-c port=5432 -c fsync=off -c max_connections=12")
        subprocess.run([str(cls.pg_ctl), "-D", str(cls.data), "-l", str(root / "postgres.log"),
                        "-o", options, "-w", "start"],
                       check=True, capture_output=True, text=True, timeout=30)
        cls.connection_args = dict(host=str(cls.socket), port=5432, user="roll_test",
                                   dbname="schwab_statements", sslmode="disable", connect_timeout=5,
                                   options="-c statement_timeout=5000 -c lock_timeout=4000")
        with psycopg.connect(**{**cls.connection_args, "dbname": "postgres"},
                             autocommit=True) as conn:
            conn.execute("create database schwab_statements")
        with cls.connect() as conn:
            store.ensure_schema(conn)

    @classmethod
    def stop_cluster(cls):
        if (cls.data / "postmaster.pid").exists():
            subprocess.run([str(cls.pg_ctl), "-D", str(cls.data), "-m", "fast", "-w", "stop"],
                           check=True, capture_output=True, text=True, timeout=15)

    @classmethod
    def connect(cls, **kwargs):
        return psycopg.connect(**cls.connection_args, **kwargs)

    def setUp(self):
        self.conn = self.connect()
        self.addCleanup(self.conn.close)
        self.conn.execute("truncate schwab.roll_alerts")
        self.conn.commit()
        self.clock = FixedClock(self.conn, AT)

    def rows(self):
        return self.conn.execute(
            "select id, position_key, candidate_key, reference_basis, quantity, created_at "
            "from schwab.roll_alerts order by id").fetchall()

    def test_schema_constraints_and_signed_numeric_storage(self):
        a = alert(reference_basis=Decimal("-123.45675"))
        claimed = store.claim_roll_alerts(self.conn, [a])
        row = self.rows()[0]
        self.assertEqual(row[:5], (claimed[0]["claim_id"], POSITION, CANDIDATE,
                                  Decimal("-123.4568"), Decimal("-1.0000")))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "insert into schwab.roll_alerts (position_key, candidate_key, reference_basis, quantity) "
                "values (%s, %s, %s, %s)", row[1:5])
        self.conn.rollback()
        with self.assertRaises(psycopg.errors.NotNullViolation):
            self.conn.execute(
                "insert into schwab.roll_alerts (position_key, candidate_key, reference_basis, quantity) "
                "values ('missing-basis', 'candidate', null, -1)")
        self.conn.rollback()

    def test_same_tuple_lifetime_and_different_candidate_cooldown(self):
        a, b = alert(), alert(candidate_key=OTHER_CANDIDATE)
        self.assertEqual(store.roll_alert_states(self.clock, [a, b]), ["would send"] * 2)
        store.claim_roll_alerts(self.clock, [a])
        self.assertEqual(store.roll_alert_states(self.clock, [b, a]),
                         ["cooldown", "already reported"])
        self.assertEqual(store.claim_roll_alerts(self.clock, [a, b]), [])
        later = FixedClock(self.conn, AT + timedelta(days=20))
        self.assertEqual(store.roll_alert_states(later, [a, b]),
                         ["already reported", "would send"])
        self.assertEqual(len(store.claim_roll_alerts(later, [b])), 1)
        self.assertEqual(store.roll_alert_states(later, [a]), ["already reported"])

    def test_multiple_inputs_one_claim_per_position(self):
        a = alert()
        b = alert(candidate_key=OTHER_CANDIDATE)
        c = alert(key=OTHER_POSITION, quantity=Decimal("1"), reference_basis=Decimal("123"))
        result = store.claim_roll_alerts(self.clock, [a, a, b, c])
        self.assertEqual([row["key"] for row in result], [POSITION, OTHER_POSITION])
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all("claim_id" not in row for row in (a, b, c)))

    def test_exact_24_hour_boundary(self):
        for offset, expected in [(-1, "cooldown"), (0, "would send"), (1, "would send")]:
            with self.subTest(microseconds_from_24_hours=offset):
                self.conn.execute("truncate schwab.roll_alerts")
                self.conn.commit()
                store.claim_roll_alerts(self.clock, [alert()])
                later = FixedClock(self.conn, AT + timedelta(hours=24, microseconds=offset))
                b = alert(candidate_key=OTHER_CANDIDATE)
                self.assertEqual(store.roll_alert_states(later, [b]), [expected])
                self.assertEqual(bool(store.claim_roll_alerts(later, [b])), expected == "would send")

    def test_rebasing_or_quantity_change_preserves_recent_cooldown(self):
        for changes in ({"reference_basis": Decimal("-200")}, {"quantity": Decimal("-2")},
                        {"reference_basis": Decimal("123.4567"), "quantity": Decimal("1")}):
            with self.subTest(changes=changes):
                self.conn.execute("truncate schwab.roll_alerts")
                self.conn.commit()
                original = store.claim_roll_alerts(self.clock, [alert()])[0]
                new = alert(**changes)
                book = [position(cost_basis=new["reference_basis"], quantity=new["quantity"])]
                recent = FixedClock(self.conn, AT + timedelta(hours=24, microseconds=-1))
                store.cleanup_roll_alerts(recent, book)
                self.assertEqual(self.rows()[0][0], original["claim_id"])
                self.assertEqual(store.roll_alert_states(recent, [new]), ["cooldown"])
                self.assertEqual(store.claim_roll_alerts(recent, [new]), [])
                later = FixedClock(self.conn, AT + timedelta(hours=24))
                store.cleanup_roll_alerts(later, book)
                self.assertEqual(self.rows(), [])
                self.assertEqual(store.roll_alert_states(later, [new]), ["would send"])
                claimed = store.claim_roll_alerts(later, [new])
                self.assertEqual(len(claimed), 1)
                self.assertNotEqual(claimed[0]["claim_id"], original["claim_id"])

    def test_cleanup_never_ages_out_unchanged_identity_or_other_candidates(self):
        store.claim_roll_alerts(self.clock, [alert()])
        later = FixedClock(self.conn, AT + timedelta(days=2))
        b = alert(candidate_key=OTHER_CANDIDATE)
        store.claim_roll_alerts(later, [b])
        much_later = FixedClock(self.conn, AT + timedelta(days=20))
        store.cleanup_roll_alerts(much_later, [position(price=None, mark=None)])
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(store.roll_alert_states(much_later, [alert(), b]), ["already reported"] * 2)

    def test_cleanup_closed_and_assumed_expired_removes_even_recent_claims(self):
        store.claim_roll_alerts(self.clock, [alert(), alert(key="expired"), alert(key="retained")])
        store.cleanup_roll_alerts(self.clock, [
            position(key="expired", status="assumed_expired_worthless"),
            position(key="retained", price=None),
        ])
        self.assertEqual([row[1] for row in self.rows()], ["retained"])
        store.cleanup_roll_alerts(self.clock, [])
        self.assertEqual(self.rows(), [])

    def test_cleanup_preserves_uncertain_and_nonfinite_positions(self):
        variants = [dict(status="uncertain"), dict(status="unreliable"), dict(cost_basis=None),
                    dict(cost_basis=Decimal("NaN")), dict(cost_basis=Decimal("Infinity")),
                    dict(quantity=Decimal("NaN")), dict(quantity=None), dict(quantity=0),
                    dict(quantity=Decimal("-1.5")), dict(expiry=None), dict(is_option=False)]
        alerts = [alert(key=str(i)) for i in range(len(variants))]
        book = [position(key=str(i), **{"cost_basis": Decimal("-999"), **changes})
                for i, changes in enumerate(variants)]
        store.claim_roll_alerts(self.clock, alerts)
        later = FixedClock(self.conn, AT + timedelta(days=5))
        store.cleanup_roll_alerts(later, book)
        self.assertEqual(len(self.rows()), len(variants))

    def test_basis_quantization_agrees_with_actual_numeric_cast_and_cleanup(self):
        for basis in (Decimal("123.45675"), Decimal("-123.45675")):
            with self.subTest(basis=basis):
                expected = self.conn.execute("select %s::numeric(20,4)", (basis,)).fetchone()[0]
                self.assertEqual(store._roll_numeric(basis), expected)
                a = alert(key=str(basis), reference_basis=basis)
                store.claim_roll_alerts(self.clock, [a])
                later = FixedClock(self.conn, AT + timedelta(days=5))
                book = [position(key=str(basis), cost_basis=basis)]
                store.cleanup_roll_alerts(later, book)
                normalized = {**a, "reference_basis": expected}
                self.assertEqual(store.roll_alert_states(later, [normalized]), ["already reported"])
                self.assertEqual(store.claim_roll_alerts(later, [normalized]), [])

    def test_release_only_exact_ids_and_failed_send_can_retry(self):
        first = store.claim_roll_alerts(self.clock, [alert()])
        store.drop_roll_alerts(self.conn, first)
        self.assertEqual(self.rows(), [])
        second = store.claim_roll_alerts(self.clock, [alert()])
        self.assertNotEqual(first[0]["claim_id"], second[0]["claim_id"])
        store.drop_roll_alerts(self.conn, first)  # A delayed failure from the old sender.
        self.assertEqual(self.rows()[0][0], second[0]["claim_id"])
        self.assertEqual(store.roll_alert_states(self.clock, [alert()]), ["already reported"])
        store.drop_roll_alerts(self.conn, [{"claim_id": second[0]["claim_id"]}])
        self.assertEqual(len(store.claim_roll_alerts(self.clock,
                         [alert(candidate_key=OTHER_CANDIDATE)])), 1)

    def test_claim_commits_before_return_even_after_prior_reads(self):
        self.conn.execute("select 1")
        claimed = store.claim_roll_alerts(self.conn, [alert()])
        self.assertEqual(self.conn.info.transaction_status, psycopg.pq.TransactionStatus.IDLE)
        with self.connect() as observer:
            self.assertEqual(observer.execute("select id from schwab.roll_alerts").fetchone(),
                             (claimed[0]["claim_id"],))

    def test_preview_works_in_read_only_transaction(self):
        store.claim_roll_alerts(self.conn, [alert()])
        self.conn.read_only = True
        self.assertEqual(store.roll_alert_states(self.conn, [alert(), alert(candidate_key=OTHER_CANDIDATE)]),
                         ["already reported", "cooldown"])
        self.conn.rollback()
        self.conn.read_only = False
        self.assertEqual(len(self.rows()), 1)

    def test_missing_table_preview_on_read_only_connection_does_not_initialize(self):
        self.conn.execute("drop table schwab.roll_alerts")
        self.conn.commit()
        try:
            self.conn.read_only = True
            self.assertEqual(store.roll_alert_states(self.conn, [alert(), alert()]), ["would send"] * 2)
            self.assertIsNone(self.conn.execute("select to_regclass('schwab.roll_alerts')").fetchone()[0])
        finally:
            self.conn.rollback()
            self.conn.read_only = False
            store.ensure_schema(self.conn)

    def test_price_alert_history_is_untouched(self):
        self.conn.execute("insert into schwab.alerts (position_key, direction, band) "
                          "values (%s, 'down', 50)", (POSITION,))
        self.conn.commit()
        before = self.conn.execute("select * from schwab.alerts order by id").fetchall()
        claimed = store.claim_roll_alerts(self.conn, [alert()])
        store.roll_alert_states(self.conn, [alert()])
        store.drop_roll_alerts(self.conn, claimed)
        store.claim_roll_alerts(self.conn, [alert()])
        store.cleanup_roll_alerts(self.conn, [])
        self.assertEqual(self.conn.execute("select * from schwab.alerts order by id").fetchall(), before)

    def concurrent_claims(self, left, right, *, autocommit=False):
        barrier = Barrier(2, timeout=5)
        def claim(alerts):
            with self.connect(autocommit=autocommit) as conn:
                barrier.wait()
                return store.claim_roll_alerts(conn, alerts)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim, alerts) for alerts in (left, right)]
            return [future.result(timeout=10) for future in futures]

    def test_simultaneous_same_candidate_has_one_claim(self):
        result = self.concurrent_claims([alert()], [alert()])
        self.assertEqual(sorted(map(len, result)), [0, 1])
        self.assertEqual(len(self.rows()), 1)

    def test_simultaneous_different_candidates_cannot_bypass_cooldown(self):
        result = self.concurrent_claims([alert()], [alert(candidate_key=OTHER_CANDIDATE)])
        self.assertEqual(sorted(map(len, result)), [0, 1])
        self.assertEqual(len(self.rows()), 1)

    def test_autocommit_connections_still_hold_transaction_locks(self):
        result = self.concurrent_claims([alert()], [alert(candidate_key=OTHER_CANDIDATE)],
                                        autocommit=True)
        self.assertEqual(sorted(map(len, result)), [0, 1])
        self.assertEqual(len(self.rows()), 1)

    def test_reversed_position_batches_do_not_deadlock(self):
        a, b = alert(), alert(key=OTHER_POSITION)
        result = self.concurrent_claims([a, b], [{**b, "candidate_key": OTHER_CANDIDATE},
                                                {**a, "candidate_key": OTHER_CANDIDATE}])
        self.assertEqual(sorted(map(len, result)), [0, 2])
        self.assertEqual(len(self.rows()), 2)


if __name__ == "__main__":
    unittest.main()
