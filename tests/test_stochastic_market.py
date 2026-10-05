"""GARCH model and price-loop regressions using isolated, disposable databases."""

import math
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.admin_manager import AdminManager
from managers.market_manager import MarketManager
from managers.movement_history_manager import MovementHistoryManager
from managers.stochastic_price_engine import (
    GarchConfig,
    next_variance,
    simulate_step,
    standardized_shock,
)


class GarchModelTests(unittest.TestCase):
    def test_default_scale_is_one_game_trading_minute_per_real_second(self):
        config = GarchConfig.from_settings({})
        self.assertEqual(config.alpha, .05)
        self.assertEqual(config.beta, .90)
        self.assertEqual(config.student_df, 6)
        self.assertEqual(config.step_seconds, 1)
        self.assertEqual(config.trading_minutes_per_step, 1)
        self.assertAlmostEqual(config.step_variance, .30 ** 2 / (252 * 390))
        self.assertAlmostEqual(config.omega, .05 * config.step_variance)
        stronger = GarchConfig.from_settings({}, volatility=.30)
        self.assertAlmostEqual(stronger.step_variance, 4 * config.step_variance)

    def test_variance_recurrence_uses_squared_last_innovation(self):
        config = GarchConfig.from_settings({})
        variance = 2 * config.step_variance
        innovation = 3 * math.sqrt(config.step_variance)
        expected = config.omega + config.alpha * innovation ** 2 + config.beta * variance
        self.assertAlmostEqual(next_variance(variance, innovation, config), expected)
        self.assertAlmostEqual(next_variance(variance, -innovation, config), expected)

    def test_large_shock_leaves_a_cluster_then_variance_reverts(self):
        config = GarchConfig.from_settings({})
        normal = config.step_variance
        variance = next_variance(normal, 5 * math.sqrt(normal), config)
        self.assertGreater(variance, normal)
        peak = variance
        for _ in range(100):
            # An innovation with squared size h is the conditional expected
            # shock energy. Its path should converge to the target variance.
            following = next_variance(variance, math.sqrt(variance), config)
            self.assertLess(following, variance)
            self.assertGreater(following, normal)
            variance = following
        self.assertLess(variance - normal, .01 * (peak - normal))

    def test_standardized_student_t_has_unit_variance_and_heavier_tails(self):
        rng = random.Random(76193)
        samples = [standardized_shock(rng=rng, df=6) for _ in range(50000)]
        mean = sum(samples) / len(samples)
        variance = sum((sample - mean) ** 2 for sample in samples) / len(samples)
        self.assertLess(abs(mean), .03)
        self.assertGreater(variance, .94)
        self.assertLess(variance, 1.06)
        self.assertGreater(sum(abs(sample) > 3 for sample in samples) / len(samples), .006)

    def test_full_market_loading_ignores_idiosyncratic_noise(self):
        config = GarchConfig.from_settings({"garch_market_correlation": "1"})
        first = simulate_step(1000, config.step_variance, 0, config,
                              market_shock=2, rng=random.Random(1))
        second = simulate_step(1000, config.step_variance, 0, config,
                               market_shock=2, rng=random.Random(999))
        self.assertEqual(first, second)
        self.assertEqual(first.shock, 2)
        self.assertAlmostEqual(first.price, 1000 * math.exp(first.log_return))
        self.assertAlmostEqual(first.innovation, math.sqrt(first.variance) * 2)

    def test_zero_market_loading_ignores_the_common_factor(self):
        config = GarchConfig.from_settings({"garch_market_correlation": "0"})
        first = simulate_step(1000, config.step_variance, 0, config,
                              market_shock=-100, rng=random.Random(77))
        second = simulate_step(1000, config.step_variance, 0, config,
                               market_shock=100, rng=random.Random(77))
        self.assertEqual(first, second)
        independent = simulate_step(1000, config.step_variance, 0, config,
                                    market_shock=100, rng=random.Random(78))
        self.assertNotEqual(second.shock, independent.shock)

    def test_single_jump_raises_subsequent_variance_without_repeated_jump_returns(self):
        config = GarchConfig.from_settings({"garch_market_correlation": "1"})
        initial = simulate_step(1000, config.step_variance, 0, config,
                                market_shock=0, jump=.05, rng=random.Random(1))
        normal = simulate_step(1000, config.step_variance, 0, config,
                               market_shock=0, rng=random.Random(1))
        self.assertAlmostEqual(initial.log_return - normal.log_return, .05)
        next_step = simulate_step(initial.price, initial.variance, initial.innovation,
                                  config, market_shock=0, rng=random.Random(2))
        self.assertGreater(next_step.variance, 100 * config.step_variance)
        self.assertLess(next_step.log_return, 0)
        self.assertAlmostEqual(next_step.innovation, 0)
        following = simulate_step(next_step.price, next_step.variance, next_step.innovation,
                                  config, market_shock=0, rng=random.Random(3))
        self.assertLess(following.variance, next_step.variance)
        self.assertGreater(following.variance, config.step_variance)

    def test_invalid_configuration_is_rejected(self):
        cases = (
            {"garch_alpha": "-0.1"},
            {"garch_alpha": "0.2", "garch_beta": "0.9"},
            {"garch_beta": "nan"},
            {"garch_annual_volatility": "inf"},
            {"garch_market_correlation": "1.01"},
            {"garch_step_seconds": "0"},
            {"garch_trading_minutes_per_step": "0"},
            {"garch_student_df": "2"},
        )
        for settings in cases:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                GarchConfig.from_settings(settings)


class StochasticMarketTests(unittest.TestCase):
    def setUp(self):
        self.previous_random_state = random.getstate()
        random.seed(20261002)
        self.previous_directory = DBManager.database_dir
        self.directory = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.directory.name)
        self.db = DatabaseManager()
        # Keep these regressions focused on the original GARCH/event path;
        # recurring targets are exercised separately with the layer enabled.
        self.db.system.set_setting("auto_target_enabled", "0")
        self.market = MarketManager(self.db)
        self.db.users.create_user("admin", "Admin", "Nac", 1000000, role="admin")
        self.db.companies.create_company("CO", "Company", "Industry", 100000,
                                         1000, 1000000, "admin", "EX")

    def tearDown(self):
        self.db.close()
        random.setstate(self.previous_random_state)
        DBManager.database_dir = self.previous_directory
        self.directory.cleanup()

    def state(self, code="CO"):
        row = self.db.system.execute_one(
            "SELECT * FROM market_simulation_state WHERE asset_code = ?", (code,))
        return dict(row) if row else None

    def factor(self, epoch, shock):
        self.db.system.execute(
            "INSERT INTO market_factor_state (key, last_epoch, current_factor) VALUES ('market', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_epoch = excluded.last_epoch, "
            "current_factor = excluded.current_factor", (epoch, shock))

    def test_market_factor_is_shared_across_all_independent_price_sources(self):
        self.db.system.set_setting("garch_market_correlation", "1")
        self.db.assets.register_asset("SPOT", "Spot", "EX", 1000000, "commodity_spot")
        self.db.products.register_investment("REG", "Regular", "investment", 1000,
                                            1000000, 0, "admin", "EX")
        random.seed(2271)
        self.market.update_prices(now=1000)
        values = [self.state(code) for code in ("CO", "SPOT", "REG")]
        self.assertTrue(all(values))
        self.assertEqual(len({row["latent_price"] for row in values}), 1)
        self.assertEqual(len({row["innovation"] for row in values}), 1)
        self.assertEqual(self.db.system.execute_one(
            "SELECT COUNT(*) AS count FROM market_factor_state")["count"], 1)

    def test_independent_asset_shocks_differ_without_market_loading(self):
        self.db.system.set_setting("garch_market_correlation", "0")
        self.db.assets.register_asset("SPOT", "Spot", "EX", 1000000, "commodity_spot")
        random.seed(431)
        self.market.update_prices(now=1000)
        self.assertNotEqual(self.state("CO")["innovation"], self.state("SPOT")["innovation"])

    def test_price_profile_liquidity_changes_realized_chart_texture(self):
        self.db.system.set_setting("garch_market_correlation", "1")
        self.db.system.set_setting("auto_target_enabled", "0")
        self.db.assets.register_asset("DEEP", "Deep", "EX", 1000000, "commodity_spot")
        self.db.companies.execute("UPDATE company SET liquidity = 0.25 WHERE company_code = 'CO'")
        self.db.assets.execute("UPDATE market_assets SET liquidity = 4 WHERE code = 'DEEP'")
        self.factor(1000, 1)
        self.market.update_prices(now=1000)
        shallow_move = abs(self.state("CO")["latent_price"] - 1000000)
        deep_move = abs(self.state("DEEP")["latent_price"] - 1000000)
        self.assertGreater(shallow_move, deep_move)

    def test_fractional_prices_accumulate_and_duplicate_restart_does_not_resample(self):
        self.db.companies.update_share_price("CO", 1000)
        self.db.system.set_setting("garch_market_correlation", "1")
        self.db.system.set_setting("garch_annual_volatility", ".03")
        displayed = []
        for epoch in range(1000, 1020):
            self.factor(epoch, 1)
            self.market.update_prices(now=epoch)
            displayed.append(self.db.companies.get_company("CO")["share_price"])
        self.assertEqual(displayed[0], 1000)
        self.assertGreater(displayed[-1], displayed[0])
        self.assertNotEqual(self.state()["latent_price"], displayed[-1])
        before = self.state()
        restored = MarketManager(self.db)
        restored.update_prices(now=1019)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], displayed[-1])

    def test_configured_step_interval_does_not_multiply_returns_on_fast_calls(self):
        self.db.system.set_setting("garch_step_seconds", "5")
        self.market.update_prices(now=1000)
        initial = self.state()
        for epoch in (1001, 1002, 1004):
            self.market.update_prices(now=epoch)
            self.assertEqual(self.state(), initial)
        self.market.update_prices(now=1005)
        self.assertEqual(self.state()["last_epoch"], 1005)
        self.assertNotEqual(self.state(), initial)

    def test_offline_gap_advances_one_step_instead_of_amplifying_elapsed_time(self):
        self.db.system.set_setting("garch_market_correlation", "1")
        self.factor(1000, 1)
        self.market.update_prices(now=1000)
        before = self.state()
        config = GarchConfig.from_settings({"garch_market_correlation": "1"})
        expected = simulate_step(before["latent_price"], before["variance"],
                                 before["innovation"], config,
                                 market_shock=1, rng=random.Random(1))
        self.factor(100000, 1)
        self.market.update_prices(now=100000)
        after = self.state()
        self.assertAlmostEqual(after["latent_price"], expected.price)
        self.assertAlmostEqual(after["variance"], expected.variance)
        self.assertAlmostEqual(after["innovation"], expected.innovation)

    def test_sql_price_edits_reanchor_the_fractional_price(self):
        self.db.system.set_setting("garch_market_correlation", "1")
        self.factor(1000, 1)
        self.market.update_prices(now=1000)
        self.db.companies.update_share_price("CO", 123456)
        self.factor(1001, 0)
        self.market.update_prices(now=1001)
        self.assertLess(abs(self.state()["latent_price"] / 123456 - 1), .01)
        self.assertLess(abs(self.db.companies.get_company("CO")["share_price"] / 123456 - 1), .01)

    def test_zero_volatility_and_fixed_prices_remain_stationary(self):
        self.market.update_prices(now=1000, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 1000000)
        self.db.companies.execute("UPDATE company SET is_fixed = 1 WHERE company_code = 'CO'")
        self.db.companies.update_share_price("CO", 55555)
        self.market.update_prices(now=1001)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 55555)
        self.db.companies.execute("UPDATE company SET is_fixed = 0 WHERE company_code = 'CO'")
        self.market.update_prices(now=1002, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 55555)

    def test_derived_prices_do_not_have_independent_garch_state(self):
        self.db.products.register_investment("ETF", "ETF", "etf", 1000, 1000,
                                            0, "admin", "EX", underlying_exchange_id="EX")
        self.market.update_prices(now=1000)
        self.assertIsNotNone(self.state("CO"))
        self.assertIsNone(self.state("ETF"))
        self.assertEqual(self.db.products.get_investment("ETF")["unit_price"], 1000)

    def test_busy_price_database_rolls_back_its_stochastic_state(self):
        blocker = sqlite3.connect(self.db.companies.db_path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            self.market.update_prices(now=1000)
            self.assertIsNone(self.state())
            self.assertEqual(self.db.companies.get_company("CO")["share_price"], 1000000)
            blocker.rollback()
            self.market.update_prices(now=1000)
            self.assertIsNotNone(self.state())
        finally:
            blocker.close()

    def test_late_tick_failure_rolls_back_prices_indices_history_and_model(self):
        self.market.update_prices(now=1000)
        tables = ('company', 'market_price_history', 'market_exchange_indices',
                  'market_exchange_index_components', 'market_exchange_index_history',
                  'market_simulation_state', 'market_factor_state', 'market_auto_targets')
        def snapshot():
            return {table: [tuple(row) for row in self.db.assets.execute(f'SELECT * FROM {table}')]
                    for table in tables}
        before = snapshot()
        with patch.object(self.market, '_record_special_quotes', side_effect=RuntimeError('late failure')):
            with self.assertRaisesRegex(RuntimeError, 'late failure'):
                self.market.update_prices(now=1001)
        self.assertEqual(snapshot(), before)
        with patch.object(self.market, '_record_special_quotes', side_effect=sqlite3.OperationalError('database is locked')):
            self.assertEqual(self.market.update_prices(now=1001), 0)
        self.assertEqual(snapshot(), before)
        self.market.update_prices(now=1001)
        self.assertEqual(self.state()['last_epoch'], 1001)

    def test_daily_chart_cleanup_preserves_simulation_state(self):
        first = 1790866800  # 2026-10-02 00:00:00 Asia/Seoul.
        self.market.update_prices(now=first)
        before = self.state()
        factor = dict(self.db.system.execute_one("SELECT * FROM market_factor_state"))
        self.assertTrue(MovementHistoryManager(self.db).cleanup(first + 86400))
        self.assertEqual(self.state(), before)
        self.assertEqual(dict(self.db.system.execute_one("SELECT * FROM market_factor_state")), factor)
        self.assertEqual(self.db.assets.execute("SELECT * FROM market_price_history"), [])

    def test_event_preserves_initial_price_duplicate_and_exact_goal(self):
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "bad", 30, "CO", 60,
                                                now=1000, target_price=700000)[0])
        random.seed(991)
        self.market.update_prices(now=1000)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 1000000)
        self.market.update_prices(now=1010)
        before = self.state()
        self.assertIsNotNone(before)
        restored = MarketManager(self.db)
        restored.update_prices(now=1010)
        self.assertEqual(self.state(), before)
        restored.update_prices(now=1060)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 700000)
        self.assertEqual(self.state()["latent_price"], 700000)

    def test_event_jump_occurs_once_and_volatility_persists_after_the_goal(self):
        self.db.system.set_setting("garch_market_correlation", "1")
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "good", 50, "CO", 60, now=1000)[0])
        self.factor(1000, 0)
        self.market.update_prices(now=1000)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 1000000)
        self.factor(1001, 0)
        self.market.update_prices(now=1001)
        first = self.state()
        self.assertGreater(first["last_display_price"], 1000000)
        self.assertLessEqual(first["last_display_price"], 1500000)
        self.assertAlmostEqual(first["innovation"], math.log1p(.05))
        self.factor(1002, 0)
        self.market.update_prices(now=1002)
        following = self.state()
        self.assertGreaterEqual(following["last_display_price"], 1000000)
        self.assertLessEqual(following["last_display_price"], 1500000)
        self.assertAlmostEqual(following["innovation"], 0)
        self.assertGreater(following["variance"], 100 * GarchConfig().step_variance)
        self.market.update_prices(now=1060)
        endpoint = self.state()
        self.assertEqual(endpoint["latent_price"], 1500000)
        self.assertGreater(endpoint["variance"], 100 * GarchConfig().step_variance)
        self.factor(1061, 0)
        self.market.update_prices(now=1061)
        resumed = self.state()
        self.assertIsNone(resumed["event_id"])
        self.assertLess(resumed["variance"], endpoint["variance"])
        self.assertGreater(resumed["variance"], 100 * GarchConfig().step_variance)

    def test_pause_retains_variance_memory_without_moving_prices(self):
        self.factor(1000, 5)
        self.market.update_prices(now=1000)
        before = self.state()
        self.market.update_prices(now=1001, volatility=0)
        paused = self.state()
        self.assertEqual(paused["last_display_price"], before["last_display_price"])
        self.assertEqual(paused["variance"], before["variance"])
        self.assertEqual(paused["innovation"], before["innovation"])
        self.factor(1002, 0)
        self.market.update_prices(now=1002)
        self.assertGreater(self.state()["variance"], before["variance"])

    def test_event_waits_for_its_next_simulation_step_before_applying_one_jump(self):
        self.db.system.set_setting("garch_step_seconds", "5")
        self.db.system.set_setting("garch_market_correlation", "1")
        self.factor(1000, 0)
        self.market.update_prices(now=1000)
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "good", 50, "CO", 60, now=1001)[0])
        self.market.update_prices(now=1001)
        self.market.update_prices(now=1002)
        self.assertIsNone(self.db.system.execute_one("SELECT jump_applied_epoch FROM market_event_targets")[0])
        self.factor(1005, 0)
        self.market.update_prices(now=1005)
        self.assertEqual(self.db.system.execute_one("SELECT jump_applied_epoch FROM market_event_targets")[0], 1005)
        self.assertAlmostEqual(self.state()["innovation"], math.log1p(.05))
        self.factor(1010, 0)
        MarketManager(self.db).update_prices(now=1010)
        self.assertEqual(self.db.system.execute_one("SELECT jump_applied_epoch FROM market_event_targets")[0], 1005)
        self.assertAlmostEqual(self.state()["innovation"], 0)

    def test_busy_event_price_rolls_back_goal_noise_jump_marker_and_model_state(self):
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "good", 50, "CO", 60, now=1000)[0])
        target = dict(self.db.system.execute_one("SELECT * FROM market_event_targets"))
        blocker = sqlite3.connect(self.db.companies.db_path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            self.market.update_prices(now=1010)
            self.assertEqual(dict(self.db.system.execute_one("SELECT * FROM market_event_targets")), target)
            self.assertIsNone(self.state())
            self.assertEqual(self.db.companies.get_company("CO")["share_price"], 1000000)
            blocker.rollback()
            self.market.update_prices(now=1010)
            self.assertEqual(self.db.system.execute_one("SELECT jump_applied_epoch FROM market_event_targets")[0], 1010)
            self.assertIsNotNone(self.state())
        finally:
            blocker.close()
