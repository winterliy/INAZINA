import os
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager


class QueryPlanTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self._old_database_dir = DBManager.database_dir
        DBManager.database_dir = Path(self._temporary.name)
        DBManager._path_locks = {}
        self.db = DatabaseManager()

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self._old_database_dir
        DBManager._path_locks = {}
        self._temporary.cleanup()

    @staticmethod
    def _details(connection, sql, parameters):
        if os.environ.get("INAZINA_DB_BACKEND", "sqlite").lower() == "postgres":
            # The test database is nearly empty, so the planner would scan; ask what it does when it may not.
            connection.execute("SET LOCAL enable_seqscan = off")
            for value in parameters:  # EXPLAIN is passed through untranslated, so write the values in
                sql = sql.replace("?", f"'{value}'" if isinstance(value, str) else str(value), 1)
            return " ".join(str(row[0]) for row in connection.execute("EXPLAIN (FORMAT JSON) " + sql))
        return " ".join(
            str(row[3]) for row in connection.execute(
                "EXPLAIN QUERY PLAN " + sql, parameters
            )
        )

    def test_active_loan_total_uses_user_partial_index(self):
        with self.db.readonly() as connection:
            details = self._details(
                connection,
                "SELECT COALESCE(SUM(outstanding_balance),0) FROM loans "
                "WHERE user_id=? AND status='active'",
                ("user",),
            )
        self.assertIn("idx_loans_active_user", details)
        self.assertNotIn("SCAN loans", details)
        self.assertNotIn("Seq Scan", details)

    def test_recent_user_orders_use_updated_index(self):
        with self.db.readonly() as connection:
            details = self._details(
                connection,
                "SELECT order_id FROM market_orders WHERE user_id=? "
                "AND updated_epoch>=? ORDER BY updated_epoch DESC",
                ("user", 0),
            )
        self.assertIn("idx_market_orders_user_updated", details)


if __name__ == "__main__":
    unittest.main()
