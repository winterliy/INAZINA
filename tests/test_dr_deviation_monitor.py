import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.dr_deviation_monitor_manager import DrDeviationMonitorManager


class DrDeviationMonitorTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.companies.create_company("BASE", "Underlying", "test", 1_000_000,
                                         100_000, 100, None, "X")
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity) "
            "VALUES ('BASE_DR','BASE','Y','NWX',1,1000,1000)"
        )
        self.db.assets.register_asset("BASE_DR", "Underlying DR", "Y", 111,
                                      "depositary_receipt", 1000)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def test_bucket_dedup_and_premium_normal_discount_transitions(self):
        with self.db.atomic() as conn:
            first = DrDeviationMonitorManager.sample_conn(conn, now=300)
            duplicate = DrDeviationMonitorManager.sample_conn(conn, now=301)
        self.assertEqual(first["sampled"], 1)
        self.assertEqual(first["transitions"], 1)
        self.assertEqual(duplicate["sampled"], 0)

        self.db.assets.execute("UPDATE market_assets SET price=105 WHERE code='BASE_DR'")
        with self.db.atomic() as conn:
            recovered = DrDeviationMonitorManager.sample_conn(conn, now=600)
        self.assertEqual(recovered["transitions"], 1)

        self.db.assets.execute("UPDATE market_assets SET price=88 WHERE code='BASE_DR'")
        with self.db.atomic() as conn:
            discounted = DrDeviationMonitorManager.sample_conn(conn, now=900)
            history = DrDeviationMonitorManager.history_conn(conn, limit=10)
        self.assertEqual(discounted["transitions"], 1)
        self.assertEqual([row["state"] for row in history["samples"]],
                         ["discount", "normal", "premium"])
        self.assertEqual([row["to_state"] for row in history["events"]],
                         ["discount", "normal", "premium"])

    def test_alert_history_is_append_only_and_old_samples_are_pruned(self):
        with self.db.atomic() as conn:
            DrDeviationMonitorManager.sample_conn(conn, now=300)
            with self.assertRaises(Exception):
                conn.execute("UPDATE dr_deviation_alert_events SET to_state='normal'")
            DrDeviationMonitorManager.sample_conn(
                conn, now=DrDeviationMonitorManager.RETENTION_SECONDS + 601
            )
            count = conn.execute("SELECT COUNT(*) FROM dr_deviation_samples").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
