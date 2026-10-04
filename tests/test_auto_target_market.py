"""Automatic target cycles over the existing GARCH model, using temporary DBs."""

import math
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.admin_manager import AdminManager
from managers.market_manager import MarketManager
from managers.movement_history_manager import MovementHistoryManager
from managers.auto_target_engine import (
    AutoTargetConfig, TargetCycle, cycle_bounds, sample_target, target_pull,
)


class AutoTargetModelTests(unittest.TestCase):
    def test_targets_are_relative_to_the_current_price_and_periods_are_bounded(self):
        config = AutoTargetConfig.from_settings({})
        self.assertTrue(config.enabled)
        self.assertEqual(config.noise_multiplier, 6)
        rng = random.Random(8142)
        cycles = [sample_target(1000000, 1000, config, rng=rng) for _ in range(200)]
        self.assertTrue(all(900000 <= cycle.target_price <= 1100000 for cycle in cycles))
        self.assertTrue(all(60 <= cycle.end_epoch - cycle.start_epoch <= 180 for cycle in cycles))
        self.assertTrue(all(cycle.anchor_price == 1000000 for cycle in cycles))
        self.assertTrue(any(cycle.target_price < 1000000 for cycle in cycles))
        self.assertTrue(any(cycle.target_price > 1000000 for cycle in cycles))

    def test_pull_is_signed_log_distance_and_is_zero_at_the_target(self):
        config = AutoTargetConfig.from_settings({})
        cycle = TargetCycle(1000000, 1100000, 1000, 1060)
        self.assertAlmostEqual(target_pull(1000000, cycle, config), .08 * math.log(1.1))
        self.assertGreater(target_pull(1000000, cycle, config), 0)
        self.assertLess(target_pull(1200000, cycle, config), 0)
        self.assertEqual(target_pull(1100000, cycle, config), 0)
        self.assertAlmostEqual(target_pull(1000000, cycle, config, elapsed_steps=2),
                               2 * target_pull(1000000, cycle, config))
        self.assertEqual(cycle_bounds(cycle, config), (750000, 1375000))

    def test_invalid_settings_are_rejected_instead_of_storing_unsafe_targets(self):
        for key, value in (
            ("auto_target_enabled", "2"), ("auto_target_range_percent", "51"),
            ("auto_target_range_percent", "nan"), ("auto_target_min_seconds", "0"),
            ("auto_target_min_seconds", "180.5"), ("auto_target_max_seconds", "inf"),
            ("auto_target_pull", "-0.1"), ("auto_target_pull", "1.01"),
            ("auto_target_noise_multiplier", "0"), ("auto_target_noise_multiplier", "31"),
            ("auto_target_band_percent", "101"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                AutoTargetConfig.from_settings({key: value})
        with self.assertRaises(ValueError):
            AutoTargetConfig.from_settings({"auto_target_min_seconds": "181", "auto_target_max_seconds": "180"})


class AutoTargetMarketTests(unittest.TestCase):
    def setUp(self):
        self.random_state = random.getstate()
        random.seed(20261002)
        self.previous_directory = DBManager.database_dir
        self.directory = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.directory.name)
        self.db = DatabaseManager()
        self.market = MarketManager(self.db)
        self.db.system.set_setting("auto_target_enabled", "1")
        self.db.system.set_setting("garch_market_correlation", "1")
        self.db.users.create_user("admin", "Admin", "Nac", 1000000, role="admin")
        self.db.companies.create_company("CO", "Company", "Industry", 100000,
                                         1000, 1000000, "admin", "EX")

    def tearDown(self):
        self.db.close()
        random.setstate(self.random_state)
        DBManager.database_dir = self.previous_directory
        self.directory.cleanup()

    def price(self, code="CO"):
        return self.db.companies.get_company(code)["share_price"]

    def state(self, code="CO"):
        row = self.db.system.execute_one(
            "SELECT * FROM market_simulation_state WHERE asset_code = ?", (code,))
        return dict(row) if row else None

    def target(self, code="CO"):
        row = self.db.system.execute_one(
            "SELECT * FROM market_auto_targets WHERE asset_code = ?", (code,))
        return dict(row) if row else None

    def factor(self, epoch, shock=0):
        self.db.system.execute(
            "INSERT INTO market_factor_state (key, last_epoch, current_factor) VALUES ('market', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_epoch = excluded.last_epoch, "
            "current_factor = excluded.current_factor", (epoch, shock))

    def seed_target(self, anchor=1000000, target=1100000, start=1000, end=1180):
        self.db.system.execute(
            "INSERT INTO market_auto_targets (asset_code, anchor_price, target_price, start_epoch, end_epoch) "
            "VALUES ('CO', ?, ?, ?, ?) "
            "ON CONFLICT(asset_code) DO UPDATE SET anchor_price=excluded.anchor_price, "
            "target_price=excluded.target_price, start_epoch=excluded.start_epoch, end_epoch=excluded.end_epoch",
            (anchor, target, start, end))

    def test_initial_target_is_persisted_and_duplicate_restart_keeps_the_cycle(self):
        self.market.update_prices(now=1000)
        cycle, state = self.target(), self.state()
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle["anchor_price"], 1000000)
        self.assertLessEqual(abs(cycle["target_price"] / 1000000 - 1), .10)
        self.assertGreaterEqual(cycle["end_epoch"] - cycle["start_epoch"], 60)
        self.assertLessEqual(cycle["end_epoch"] - cycle["start_epoch"], 180)
        MarketManager(self.db).update_prices(now=1000)
        self.assertEqual(self.target(), cycle)
        self.assertEqual(self.state(), state)

    def test_expired_cycle_draws_a_new_target_relative_to_the_then_current_quote(self):
        self.db.system.set_setting("auto_target_min_seconds", "60")
        self.db.system.set_setting("auto_target_max_seconds", "60")
        self.market.update_prices(now=1000)
        first = self.target()
        current = self.price()
        self.factor(1060)
        MarketManager(self.db).update_prices(now=1060)
        following = self.target()
        self.assertEqual(following["start_epoch"], 1060)
        self.assertEqual(following["end_epoch"], 1120)
        self.assertAlmostEqual(following["anchor_price"], current, delta=1)
        self.assertLessEqual(abs(following["target_price"] / current - 1), .10)
        self.assertNotEqual(following, first)

    def test_price_reverts_toward_a_target_without_putting_target_drift_into_garch_shocks(self):
        self.seed_target()
        distances = []
        for epoch in range(1000, 1020):
            self.factor(epoch)
            self.market.update_prices(now=epoch)
            state = self.state()
            distances.append(abs(math.log(state["latent_price"] / 1100000)))
            self.assertAlmostEqual(state["innovation"], 0, places=12)
        self.assertLess(distances[-1], distances[0] / 2)
        self.assertGreater(self.price(), 1000000)
        self.assertLess(self.price(), 1100000)

    def test_noise_is_larger_than_original_garch_and_still_moves_both_directions(self):
        self.db.system.set_setting("auto_target_range_percent", "0")
        self.db.system.set_setting("auto_target_enabled", "0")
        self.factor(1000, 2)
        self.market.update_prices(now=1000)
        ordinary_return = math.log(self.state()["latent_price"] / 1000000)
        ordinary_variance = self.state()["variance"]
        self.db.companies.update_share_price("CO", 1000000)
        self.db.system.execute("DELETE FROM market_simulation_state")
        self.db.system.set_setting("auto_target_enabled", "1")
        self.market.update_prices(now=1000)
        stronger = self.state()
        self.assertGreater(math.log(stronger["latent_price"] / 1000000), 5 * ordinary_return)
        self.assertAlmostEqual(stronger["variance"], 36 * ordinary_variance)
        current = self.price()
        self.factor(1001, -2)
        self.market.update_prices(now=1001)
        self.assertLess(self.price(), current)

    def test_extreme_shocks_remain_inside_the_cycle_band(self):
        self.db.system.set_setting("auto_target_range_percent", "0")
        self.db.system.set_setting("auto_target_min_seconds", "180")
        self.db.system.set_setting("auto_target_max_seconds", "180")
        for epoch in range(1000, 1060):
            self.factor(epoch, 100 if epoch < 1030 else -100)
            self.market.update_prices(now=epoch)
            self.assertGreaterEqual(self.state()["latent_price"], 750000)
            self.assertLessEqual(self.state()["latent_price"], 1250000)
            self.assertGreaterEqual(self.price(), 750000)
            self.assertLessEqual(self.price(), 1250000)
        self.assertEqual(self.target()["anchor_price"], 1000000)

    def test_sql_price_edit_reanchors_and_replaces_the_previous_target(self):
        self.market.update_prices(now=1000)
        self.db.companies.update_share_price("CO", 123456)
        self.factor(1001)
        self.market.update_prices(now=1001)
        cycle = self.target()
        self.assertEqual(cycle["anchor_price"], 123456)
        self.assertEqual(cycle["start_epoch"], 1001)
        self.assertLessEqual(abs(cycle["target_price"] / 123456 - 1), .10)
        self.assertLess(abs(self.price() / 123456 - 1), .02)

    def test_fixed_then_released_price_has_a_fresh_cycle_from_its_new_quote(self):
        self.market.update_prices(now=1000)
        self.db.companies.execute("UPDATE company SET is_fixed = 1 WHERE company_code = 'CO'")
        self.db.companies.update_share_price("CO", 123456)
        self.market.update_prices(now=1001)
        self.assertIsNone(self.target())
        self.assertEqual(self.price(), 123456)
        self.db.companies.execute("UPDATE company SET is_fixed = 0 WHERE company_code = 'CO'")
        self.factor(1002)
        self.market.update_prices(now=1002)
        self.assertEqual(self.target()["anchor_price"], 123456)
        self.assertEqual(self.target()["start_epoch"], 1002)

    def test_zero_asset_volatility_freezes_even_with_a_pending_target(self):
        self.seed_target()
        self.db.companies.execute("UPDATE company SET market_volatility = 0 WHERE company_code = 'CO'")
        self.market.update_prices(now=1000)
        self.market.update_prices(now=1001)
        self.assertEqual(self.price(), 1000000)
        self.assertEqual(self.state()["latent_price"], 1000000)

    def test_manual_event_takes_priority_then_automatic_cycle_restarts_from_endpoint(self):
        self.db.system.set_setting("auto_target_range_percent", "0")
        self.factor(1000)
        self.market.update_prices(now=1000)
        self.assertIsNotNone(self.target())
        self.assertTrue(AdminManager(self.db).create_market_event(
            "admin", "good", 0, "CO", 10, now=1001,
            target_price=1200000, target_percent=0)[0])
        self.market.update_prices(now=1001)
        self.assertIsNone(self.target())
        self.market.update_prices(now=1011)
        self.assertEqual(self.price(), 1200000)
        self.assertIsNone(self.target())
        self.factor(1012)
        self.market.update_prices(now=1012)
        self.assertEqual(self.target()["anchor_price"], 1200000)
        self.assertEqual(self.target()["start_epoch"], 1012)

    def test_busy_price_database_rolls_back_cycle_creation_and_stochastic_state(self):
        blocker = sqlite3.connect(self.db.companies.db_path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            self.market.update_prices(now=1000)
            self.assertIsNone(self.target())
            self.assertIsNone(self.state())
            self.assertIsNone(self.db.system.execute_one("SELECT * FROM market_factor_state"))
            self.assertEqual(self.price(), 1000000)
            blocker.rollback()
            self.market.update_prices(now=1000)
            self.assertIsNotNone(self.target())
            self.assertIsNotNone(self.state())
        finally:
            blocker.close()

    def test_linked_products_do_not_get_independent_targets(self):
        for code, product_type in (("ETF", "etf"), ("FUND", "mutual_fund"), ("INDEX", "index_fund")):
            self.db.products.register_investment(code, code, product_type, 1000, 1000,
                                                0, "admin", "EX", underlying_exchange_id="EX")
        self.market.update_prices(now=1000)
        self.assertIsNotNone(self.target("CO"))
        for code in ("ETF", "FUND", "INDEX"):
            self.assertIsNone(self.target(code))
            self.assertIsNone(self.state(code))

    def test_daily_movement_cleanup_preserves_pending_cycles(self):
        epoch = 1790866800
        self.market.update_prices(now=epoch)
        cycle = self.target()
        self.assertTrue(MovementHistoryManager(self.db).cleanup(epoch + 86400))
        self.assertEqual(self.target(), cycle)

    def test_live_period_setting_keeps_pending_cycle_and_applies_to_the_next_one(self):
        self.db.system.set_setting("auto_target_min_seconds", "60")
        self.db.system.set_setting("auto_target_max_seconds", "60")
        self.market.update_prices(now=1000)
        first = self.target()
        self.db.system.set_setting("auto_target_min_seconds", "120")
        self.db.system.set_setting("auto_target_max_seconds", "120")
        self.factor(1001)
        self.market.update_prices(now=1001)
        self.assertEqual(self.target(), first)
        self.factor(1060)
        self.market.update_prices(now=1060)
        self.assertEqual(self.target()["end_epoch"] - self.target()["start_epoch"], 120)

    def test_disabled_auto_targets_leave_original_garch_active(self):
        self.market.update_prices(now=1000)
        self.db.system.set_setting("auto_target_enabled", "0")
        self.factor(1001, 2)
        self.market.update_prices(now=1001)
        self.assertIsNone(self.target())
        self.assertIsNotNone(self.state())
        self.assertEqual(self.state()["last_epoch"], 1001)


if __name__ == "__main__":
    unittest.main()
