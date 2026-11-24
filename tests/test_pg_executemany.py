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


class PgCatalogShortcutTests(PgExecuteManyTests):
    """Catalog answers that were made faster must stay the same."""

    def test_row_names_are_case_insensitive_first_match_and_index_error(self):
        from database.pg_backend import Row
        row = Row(["A", "b", "a"], (1, 2, 3))
        self.assertEqual((row["a"], row["B"], row[2]), (1, 2, 3))
        with self.assertRaises(IndexError):
            row["zz"]

    def test_table_existence_question(self):
        with self.db.atomic() as conn:
            self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", ("market_orders",)).fetchone())
            self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='saving'").fetchone())
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='no_such_table_zz'").fetchone())

    def test_integer_keys_come_from_the_catalog(self):
        from database import pg_backend
        keys = pg_backend._integer_keys()
        self.assertEqual(keys.get("market_orders"), "order_id")
        self.assertNotIn("users", keys)

    def test_foreign_key_list_answers_from_the_catalog_and_auth_table_is_not_rebuilt(self):
        from database import pg_backend
        from web_api import server
        with pg_backend.connect() as conn:
            server.ensure_auth_table(conn)
            keys = conn.execute("PRAGMA foreign_key_list(web_auth_credentials)").fetchall()
            self.assertTrue(any(row[2] == "users" and row[3] == "user_id" and row[4] == "user_id" for row in keys))
            self.assertEqual([row[5:7] for row in keys if row[2] == "users"], [("NO ACTION", "CASCADE")])
            before = conn.execute("SELECT COUNT(*) FROM web_auth_credentials").fetchone()[0]
            seen = []
            conn.set_trace_callback(seen.append)
            server.ensure_auth_table(conn)
            conn.set_trace_callback(None)
            self.assertFalse([sql for sql in seen if sql.lstrip().upper().startswith(("ALTER", "DROP", "INSERT"))])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM web_auth_credentials").fetchone()[0], before)
            composite = conn.execute("PRAGMA foreign_key_list(currency_cash_wallet_links)").fetchall()
            pairs = {(row[0], row[3], row[4]) for row in composite if row[2] == "currency_cash_postings"}
            self.assertEqual(len({pair[0] for pair in pairs}), 1)
            self.assertEqual(len(pairs), 3)

    def test_currency_precision_memo_follows_writes_and_transaction_edges(self):
        from database import pg_backend
        from functions.money import currency_precision
        with pg_backend.connect() as conn:
            conn.execute("BEGIN")
            code = "ZQT"
            conn.execute("INSERT INTO currencies(code,name,asset_code,decimal_places) VALUES (?,?,?,?)", (code, "test", "ZQT_ASSET", 2))
            conn.commit()
            conn.execute("BEGIN")
            seen = []
            conn.set_trace_callback(seen.append)
            self.assertEqual(currency_precision(conn, code), 2)
            self.assertEqual(currency_precision(conn, code), 2)
            self.assertEqual(len([q for q in seen if "decimal_places FROM currencies" in q]), 1)  # the second answer came from the memo
            # (a) a change inside the same transaction is seen at once
            conn.execute("UPDATE currencies SET decimal_places=1 WHERE code=?", (code,))
            self.assertEqual(currency_precision(conn, code), 1)
            # (b) rolling back to a savepoint brings the old value back
            conn.execute("SAVEPOINT before_change")
            conn.execute("UPDATE currencies SET decimal_places=0 WHERE code=?", (code,))
            self.assertEqual(currency_precision(conn, code), 0)
            conn.execute("ROLLBACK TO before_change")
            self.assertEqual(currency_precision(conn, code), 1)
            # a failed statement's own savepoint also forgets (the failed write may have named the table)
            conn.rollback()
            self.assertEqual(currency_precision(conn, code), 2)
            # outside a transaction nothing is kept
            conn.set_trace_callback(None)
            self.assertEqual(conn._memo, {})
            # an unknown currency is an error every time, never remembered
            conn.execute("BEGIN")
            for _ in range(2):
                with self.assertRaises(ValueError):
                    currency_precision(conn, "ZZZ")
            conn.rollback()
            conn.rollback()
            conn.execute("DELETE FROM currencies WHERE code=?", (code,))
            conn.commit()
