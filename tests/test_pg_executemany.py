"""executemany on PostgreSQL: one savepoint and one pipelined batch inside a transaction, with the same results and failure behaviour
as when every row was its own statement."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.market_manager import MarketManager

INSERT = ("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,?,?)")


class PgExecuteManyTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def rows(self, conn, code):
        return [tuple(row) for row in conn.execute(
            "SELECT asset_code,price,recorded_epoch FROM market_price_history WHERE asset_code=? ORDER BY recorded_epoch", (code,))]

    def test_batch_inside_a_transaction_applies_every_row_and_sums_rowcount(self):
        with self.db.atomic() as conn:
            cursor = conn.executemany(INSERT, [("ZZ1", 10 + i, 100 + i) for i in range(5)])
            self.assertEqual(cursor.rowcount, 5)
            self.assertEqual(self.rows(conn, "ZZ1"), [("ZZ1", 10 + i, 100 + i) for i in range(5)])

    def test_updates_report_the_rows_they_changed(self):
        with self.db.atomic() as conn:
            conn.executemany(INSERT, [("ZZ2", 1, 1), ("ZZ2", 2, 2), ("ZZ2", 3, 3)])
            cursor = conn.executemany("UPDATE market_price_history SET price=? WHERE asset_code=? AND recorded_epoch=?",
                                      [(20, "ZZ2", 1), (30, "ZZ2", 2), (99, "ZZ2", 77)])   # the last one matches nothing
            self.assertEqual(cursor.rowcount, 2)

    def test_a_generator_and_an_empty_or_single_row_batch_work(self):
        with self.db.atomic() as conn:
            self.assertEqual(conn.executemany(INSERT, ((("ZZ3", i, i)) for i in range(3))).rowcount, 3)
            self.assertEqual(conn.executemany(INSERT, []).rowcount, 0)
            self.assertEqual(conn.executemany(INSERT, [("ZZ3", 9, 9)]).rowcount, 1)
            self.assertEqual(len(self.rows(conn, "ZZ3")), 4)

    def test_a_failing_row_leaves_the_rows_before_it_applied_and_the_transaction_usable(self):
        # What a caller that catches the error and carries on saw when every row was its own statement.
        with self.db.atomic() as conn:
            batch = [("ZZ4", 1, 1), ("ZZ4", 2, 2), ("ZZ4", 3, 2), ("ZZ4", 4, 4)]       # the third repeats (asset_code, recorded_epoch)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.executemany(INSERT, batch)
            self.assertEqual(self.rows(conn, "ZZ4"), [("ZZ4", 1, 1), ("ZZ4", 2, 2)])      # applied before the failure, nothing after
            conn.execute(INSERT, ("ZZ4", 5, 5))                                           # the transaction is still usable
            self.assertEqual(len(self.rows(conn, "ZZ4")), 3)

    def test_a_batch_is_one_savepoint_and_one_pipelined_call(self):
        class Recorder:
            """Wraps the raw psycopg cursor and records what is sent."""
            def __init__(self, cursor, log):
                self._cursor, self.log = cursor, log

            def execute(self, query, *args, **kwargs):
                self.log.append(("execute", str(query)))
                return self._cursor.execute(query, *args, **kwargs)

            def executemany(self, query, seq, **kwargs):
                seq = list(seq)
                self.log.append(("executemany", len(seq)))
                return self._cursor.executemany(query, seq, **kwargs)

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        with self.db.atomic() as conn:
            log = []
            cursor = conn.cursor()
            cursor._cursor = Recorder(cursor._cursor, log)
            cursor.executemany(INSERT, [("ZZ5", i, i) for i in range(50)])
            self.assertEqual(log, [("execute", "SAVEPOINT inazina_batch"), ("executemany", 50), ("execute", "RELEASE SAVEPOINT inazina_batch")])

    def test_outside_a_transaction_rows_are_still_applied_one_by_one(self):
        from database import pg_backend
        conn = pg_backend.connect()
        try:
            self.assertFalse(conn.in_transaction)
            self.assertEqual(conn.executemany(INSERT, [("ZZ6", 1, 1), ("ZZ6", 2, 2)]).rowcount, 2)
            conn.commit()
            self.assertEqual(self.rows(conn, "ZZ6"), [("ZZ6", 1, 1), ("ZZ6", 2, 2)])
        finally:
            conn.close()

    def test_market_loops_ask_each_exchange_once(self):
        calls = []

        class Stub:
            def __init__(self, db):
                pass

            def trading_error(self, conn=None, now=None, *, asset_code=None, exchange_id=None):
                calls.append(exchange_id)
                return None if exchange_id != "CLOSED" else "closed"

        market = MarketManager(self.db)
        memo = {}
        with patch("managers.market_manager.MarketSessionManager", Stub):
            answers = [market._session_blocked(None, 1, exchange, memo) for exchange in ("A", "A", "CLOSED", "A", "CLOSED", None, None)]
        self.assertEqual(answers, [None, None, "closed", None, "closed", None, None])
        self.assertEqual(calls, ["A", "CLOSED", None])


if __name__ == "__main__":
    unittest.main()


class PgJoinedSavepointTests(PgExecuteManyTests):
    """A statement inside a transaction goes out as savepoint + statement + release in one message; a failing one still leaves the
    transaction usable and its earlier work intact."""

    def test_failed_statement_keeps_the_transaction_and_earlier_rows(self):
        with self.db.atomic() as conn:
            conn.execute(INSERT, ("ZZ9", 1, 1))
            with self.assertRaises(sqlite3.Error):
                conn.execute("INSERT INTO no_such_table_zz(a) VALUES (?)", (1,))
            conn.execute(INSERT, ("ZZ9", 2, 2))
            self.assertEqual(len(self.rows(conn, "ZZ9")), 2)

    def test_semicolon_and_percent_in_values_and_rowcount(self):
        with self.db.atomic() as conn:
            cursor = conn.execute(INSERT, ("Z;--%Z", 5, 5))
            self.assertEqual(cursor.rowcount, 1)
            self.assertEqual(self.rows(conn, "Z;--%Z"), [("Z;--%Z", 5, 5)])
            self.assertEqual(conn.execute("SELECT price FROM market_price_history WHERE asset_code=?", ("Z;--%Z",)).fetchone()[0], 5)
