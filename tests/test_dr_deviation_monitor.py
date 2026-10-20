import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.dr_deviation_monitor_manager import DrDeviationMonitorManager
from managers.market_manager import MarketManager
from functions.dr_pricing import convergence_drift


class DrDeviationMonitorTests(unittest.TestCase):
    def test_reference_parity_creates_directional_but_unforced_price_pull(self):
        discount = convergence_drift(90, 100, .01)
        premium = convergence_drift(110, 100, .01)
        self.assertGreater(discount, 0)
        self.assertLess(premium, 0)
        self.assertLess(abs(discount), .01)
        self.assertEqual(convergence_drift(0, 100, .01), 0)

    def test_market_manager_reads_current_dr_underlying_parity(self):
        with self.db.atomic() as conn:
            value = MarketManager._dr_reference_value(conn, "BASE_DR")
        self.assertEqual(value, 100)

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
        self.assertEqual(first["transitions"], 0)
        self.assertEqual(duplicate["sampled"], 0)

        with self.db.atomic() as conn:
            confirmed = DrDeviationMonitorManager.sample_conn(conn, now=600)
        self.assertEqual(confirmed["transitions"], 1)

        self.db.assets.execute("UPDATE market_assets SET price=105 WHERE code='BASE_DR'")
        with self.db.atomic() as conn:
            first_normal = DrDeviationMonitorManager.sample_conn(conn, now=900)
        self.assertEqual(first_normal["transitions"], 0)
        with self.db.atomic() as conn:
            recovered = DrDeviationMonitorManager.sample_conn(conn, now=1200)
        self.assertEqual(recovered["transitions"], 1)

        self.db.assets.execute("UPDATE market_assets SET price=88 WHERE code='BASE_DR'")
        with self.db.atomic() as conn:
            first_discount = DrDeviationMonitorManager.sample_conn(conn, now=1500)
        self.assertEqual(first_discount["transitions"], 0)
        with self.db.atomic() as conn:
            discounted = DrDeviationMonitorManager.sample_conn(conn, now=1800)
            history = DrDeviationMonitorManager.history_conn(conn, limit=10)
        self.assertEqual(discounted["transitions"], 1)
        self.assertEqual([row["state"] for row in history["samples"]],
                         ["discount", "discount", "normal", "normal", "premium", "premium"])
        self.assertEqual([row["to_state"] for row in history["events"]],
                         ["discount", "normal", "premium"])
        self.assertEqual([row["dr_price_minor_units"] for row in history["samples"]],
                         [88, 88, 105, 105, 111, 111])
        self.assertEqual([row["reference_value_minor_units"] for row in history["samples"]],
                         [100, 100, 100, 100, 100, 100])
        self.assertEqual([row["dr_price_minor_units"] for row in history["events"]],
                         [88, 105, 111])

    def test_invalid_sample_epoch_does_not_create_observation_or_alert(self):
        before_samples = self.db.assets.execute_one(
            "SELECT COUNT(*) FROM dr_deviation_samples"
        )[0]
        before_events = self.db.assets.execute_one(
            "SELECT COUNT(*) FROM dr_deviation_alert_events"
        )[0]
        for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "invalid_monitor_epoch"):
                    with self.db.atomic() as conn:
                        DrDeviationMonitorManager.sample_conn(conn, now=invalid_epoch)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM dr_deviation_samples"
        )[0], before_samples)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM dr_deviation_alert_events"
        )[0], before_events)

    def test_alert_history_is_append_only_and_old_samples_are_pruned(self):
        with self.db.atomic() as conn:
            DrDeviationMonitorManager.sample_conn(conn, now=300)
            DrDeviationMonitorManager.sample_conn(conn, now=600)
            with self.assertRaises(Exception):
                conn.execute("UPDATE dr_deviation_alert_events SET to_state='normal'")
            DrDeviationMonitorManager.sample_conn(
                conn, now=DrDeviationMonitorManager.RETENTION_SECONDS + 601
            )
            count = conn.execute("SELECT COUNT(*) FROM dr_deviation_samples").fetchone()[0]
        self.assertEqual(count, 1)

    def test_transition_requires_adjacent_buckets_and_missing_price_does_not_clear_alert(self):
        with self.db.atomic() as conn:
            DrDeviationMonitorManager.sample_conn(conn, now=300)
            gap = DrDeviationMonitorManager.sample_conn(conn, now=900)
        self.assertEqual(gap["transitions"], 0)
        with self.db.atomic() as conn:
            confirmed = DrDeviationMonitorManager.sample_conn(conn, now=1200)
        self.assertEqual(confirmed["transitions"], 1)

        self.db.assets.execute("UPDATE market_assets SET status='inactive' WHERE code='BASE_DR'")
        with self.db.atomic() as conn:
            missing = DrDeviationMonitorManager.sample_conn(conn, now=1500)
            still_missing = DrDeviationMonitorManager.sample_conn(conn, now=1800)
            history = DrDeviationMonitorManager.history_conn(conn, limit=10)
        self.assertEqual(missing["transitions"], 0)
        self.assertEqual(still_missing["transitions"], 0)
        self.assertEqual([row["to_state"] for row in history["events"]], ["premium"])


if __name__ == "__main__":
    unittest.main()
