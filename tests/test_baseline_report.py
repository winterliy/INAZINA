"""One list of every declared baseline, and wallet balances rebuilt for a past time."""
import time
import unittest

from database import pg_backend
from managers.baseline_report import BaselineReport
from tests import test_market_features as fixtures


class BaselineReportTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def test_every_baseline_table_is_listed_with_its_row_count(self):
        with self.db.readonly() as conn:
            rows = {row["table"]: row for row in BaselineReport.collect(conn)}
        self.assertIn("customer_wallet_baseline_adjustments", rows)
        if not pg_backend.enabled():                      # the snapshot tables are not carried to PostgreSQL
            self.assertIn("financial_amount_snapshot_retirements", rows)
        self.assertEqual(rows["customer_wallet_baseline_adjustments"]["rows"], 0)
        self.assertTrue(all(row["label"] for row in rows.values()))
        self.assertGreaterEqual(len(rows), 10)

    def test_a_wallet_balance_can_be_rebuilt_for_a_past_moment(self):
        now = int(time.time())
        with self.db.readonly() as conn:
            before = BaselineReport.wallet_as_of(conn, "alice", "NWX", now - 100_000)
            after = BaselineReport.wallet_as_of(conn, "alice", "NWX", now + 100)
            current = int(conn.execute("SELECT cash_minor FROM users WHERE user_id='alice'").fetchone()[0])
        self.assertEqual(before, {"balance": 0, "baselined": False})
        self.assertEqual(after["balance"], current)
        self.assertEqual(current, 10_000)
