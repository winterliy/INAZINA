"""The price guard inside the market tick: a long-run anchor keeps automatic movement from wandering to the floor."""
import random
import unittest

import test_auto_target_market as base


class PriceGuardMarketTests(base.AutoTargetMarketTests):
    # reuse the fixture (temporary DBs, company CO at 1,000,000) without re-running the parent's own tests
    test_initial_target_is_persisted_and_duplicate_restart_keeps_the_cycle = None
    test_expired_cycle_draws_a_new_target_relative_to_the_then_current_quote = None
    test_price_reverts_toward_a_target_without_putting_target_drift_into_garch_shocks = None

    def anchor(self, code="CO"):
        row = self.db.system.execute_one("SELECT * FROM market_price_anchor WHERE asset_code = ?", (code,))
        return dict(row) if row else None

    def seed_anchor(self, value, code="CO"):
        self.db.system.execute(
            "INSERT INTO market_price_anchor (asset_code, anchor_price, updated_epoch) VALUES (?, ?, 1000) "
            "ON CONFLICT(asset_code) DO UPDATE SET anchor_price = excluded.anchor_price", (code, value))

    def run_ticks(self, count, start=1000):
        prices = []
        for epoch in range(start, start + count):
            self.factor(epoch)
            self.market.update_prices(now=epoch)
            prices.append(self.price())
        return prices

    def test_the_first_tick_starts_the_anchor_at_the_quote_and_it_then_follows_slowly(self):
        self.run_ticks(1)
        first = self.anchor()
        self.assertIsNotNone(first)
        self.assertAlmostEqual(first["anchor_price"], 1000000, delta=60000)
        prices = self.run_ticks(40, start=1001)
        later = self.anchor()["anchor_price"]
        self.assertLess(abs(later / first["anchor_price"] - 1), 0.01, "a 12 h half-life barely moves in 40 seconds")

    def test_a_price_far_below_a_remembered_anchor_cannot_fall_any_further(self):
        self.run_ticks(1)
        floor = self.price()
        self.seed_anchor(floor * 4)                       # the quote is 75% below where it has been
        guarded = self.run_ticks(60, start=1001)
        self.assertGreaterEqual(min(guarded), floor, "no step may go further down than where it already was")

    def test_without_the_guard_the_same_run_does_go_lower(self):
        self.db.system.set_setting("price_guard_enabled", "0")
        random.seed(20261002)
        self.run_ticks(1)
        floor = self.price()
        self.seed_anchor(floor * 4)
        free = self.run_ticks(60, start=1001)
        self.assertLess(min(free), floor, "control: the unguarded walk does dip below the start")

    def test_an_edited_price_restarts_the_anchor_instead_of_being_pulled_back(self):
        self.run_ticks(1)
        self.seed_anchor(4000000)
        self.db.companies.execute("UPDATE company SET share_price = 1500000 WHERE company_code = 'CO'")
        self.run_ticks(1, start=1001)
        self.assertAlmostEqual(self.anchor()["anchor_price"], 1500000, delta=100000)
        self.assertGreater(self.price(), 1000000)

    # ---- the 100-won floor -------------------------------------------------------------------------------------------------------------

    def set_price(self, value, code="CO"):
        self.db.companies.execute("UPDATE company SET share_price = ? WHERE company_code = ?", (value, code))

    def floor_events(self):
        return [dict(row) for row in self.db.system.execute("SELECT * FROM price_floor_events ORDER BY event_id")]

    def test_a_stock_below_100_is_lifted_to_100_and_the_console_is_told(self):
        self.set_price(40)
        self.run_ticks(1)
        self.assertEqual(self.price(), 100)
        events = self.floor_events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["asset_code"], events[0]["to_price"]), ("CO", 100))
        self.assertAlmostEqual(events[0]["from_price"], 40, delta=10)      # the price when the sweep found it (it moved a little in that tick)
        recent = self.market.recent_floor_actions()
        self.assertEqual(recent[0]["code"], "CO")
        self.assertEqual(recent[0]["to"], 100)
        self.assertLess(recent[0]["from"], 100)
        self.assertEqual(recent[0]["name"], "Company")

    def test_it_never_falls_below_100_afterwards_and_is_not_reported_again_each_second(self):
        self.set_price(60)
        prices = self.run_ticks(120)
        self.assertGreaterEqual(min(prices), 100)
        self.assertEqual(len(self.floor_events()), 1, "lifting once is one notice, however many ticks follow")

    def test_a_stock_at_or_above_the_floor_is_left_alone(self):
        self.run_ticks(5)                                 # CO starts at 1,000,000
        self.assertEqual(self.floor_events(), [])
        self.set_price(100)
        self.run_ticks(1, start=1006)
        self.assertEqual(self.floor_events(), [], "exactly 100 is not below the floor")

    def test_the_floor_can_be_turned_off_with_a_setting(self):
        self.db.system.set_setting("price_floor_won", "0")
        self.set_price(40)
        self.run_ticks(1)
        self.assertLess(self.price(), 100)
        self.assertEqual(self.floor_events(), [])

    def test_a_fixed_stock_is_also_lifted_once_but_not_moved_afterwards(self):
        self.db.companies.execute("UPDATE company SET is_fixed = 1 WHERE company_code = 'CO'")
        self.set_price(30)
        self.run_ticks(3)
        self.assertEqual(self.price(), 100)
        self.assertEqual(len(self.floor_events()), 1)

    # ---- the floor yields to an administrator event -----------------------------------------------------------------------------------

    def test_an_event_target_below_the_floor_is_kept_while_the_event_runs_and_the_floor_returns_after_it(self):
        from managers.admin_manager import AdminManager
        self.db.users.create_user("admin", "Admin", "Nac", 1000000, role="admin")
        self.set_price(100)
        self.assertTrue(AdminManager(self.db).create_market_event("admin", "bad", 70, "CO", 60, now=1000, target_price=30)[0])
        self.factor(1000)
        self.market.update_prices(volatility=0, now=1030)
        self.assertTrue(30 <= self.price() < 100, "mid-bridge the event's own price (below 100) is not lifted")
        self.assertEqual(self.floor_events(), [])
        self.factor(1060)
        self.market.update_prices(volatility=0, now=1060)
        self.assertEqual(self.price(), 30, "still 30 in the tick the event completes")
        self.factor(1061)
        self.market.update_prices(volatility=0, now=1061)
        self.assertEqual(self.price(), 100, "afterwards the floor applies as before")
        self.assertEqual(len(self.floor_events()), 1)

    def test_a_stock_without_an_event_is_still_lifted_while_another_stock_has_one(self):
        from managers.admin_manager import AdminManager
        self.db.users.create_user("admin", "Admin", "Nac", 1000000, role="admin")
        self.db.companies.create_company("OTHER", "Other", "Industry", 100000, 1000, 100, "admin", "EX")
        self.set_price(100)
        self.set_price(40, "OTHER")
        self.assertTrue(AdminManager(self.db).create_market_event("admin", "bad", 70, "CO", 60, now=1000, target_price=30)[0])
        self.factor(1030)
        self.market.update_prices(volatility=0, now=1030)
        self.assertTrue(30 <= self.price() < 100)
        other = self.db.companies.get_company("OTHER")["share_price"]
        self.assertEqual(other, 100)
        self.assertEqual([e["asset_code"] for e in self.floor_events()], ["OTHER"])


if __name__ == "__main__":
    unittest.main()
