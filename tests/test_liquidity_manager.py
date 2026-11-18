"""Liquidity companies: independent 법인 funded from the treasury that quote the order book algorithmically."""
import unittest

from unittest import mock

from managers.liquidity_manager import FIRMS, LiquidityManager, PROFILES, STRATEGIES, quote_prices
from tests import test_market_features as fixtures


class QuoteMathTests(unittest.TestCase):
    def test_quotes_straddle_the_price_and_lean_against_inventory(self):
        for profile in PROFILES:
            bids, asks = quote_prices(10000, 0.0, profile)
            self.assertTrue(all(b < 10000 < a for b, a in zip(bids, asks)))
            self.assertEqual(bids, sorted(bids, reverse=True))
            empty_bid = bids[0]
            full_bids, full_asks = quote_prices(10000, 1.0, profile)
            self.assertLess(full_bids[0], empty_bid)               # holding a lot: buys cheaper
            self.assertLess(full_asks[0], asks[0])                 # ... and sells cheaper
        self.assertGreater(quote_prices(10000, 0, "low")[1][0] - quote_prices(10000, 0, "low")[0][0],
                           quote_prices(10000, 0, "high")[1][0] - quote_prices(10000, 0, "high")[0][0])


class LiquidityManagerTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def manager(self):
        return LiquidityManager(self.db, self.book)

    def test_twelve_independent_companies_are_created_and_funded(self):
        created = self.manager().provision_firms("admin", capital=50_000)
        self.assertEqual(len(created), 12)
        self.assertEqual(self.manager().provision_firms("admin", capital=50_000), [])      # idempotent
        with self.db.readonly() as conn:
            rows = conn.execute("SELECT c.company_code, c.legal_form, c.exchange_id, c.nationality, p.profile, a.user_id "
                                "FROM company c JOIN liquidity_providers p USING(company_code) "
                                "JOIN company_accounts a USING(company_code)").fetchall()
            self.assertEqual({r["legal_form"] for r in rows}, {"limited_company"})
            self.assertEqual({r["exchange_id"] for r in rows}, {None})
            self.assertEqual({r["nationality"] for r in rows}, {"낙원"})
            self.assertEqual(sorted(r["profile"] for r in rows).count("low"), 4)
            self.assertEqual(sorted(r["profile"] for r in rows).count("high"), 4)
            self.assertEqual(len(FIRMS), len({r["company_code"] for r in rows}))
        for row in self.manager().providers():
            self.assertEqual(row["cash"], 50_000)
            self.assertEqual(row["pnl"], 0)

    def test_a_cycle_posts_bids_below_and_asks_above_the_price_and_requoting_is_stable(self):
        manager = self.manager()
        manager.provision_firms("admin", capital=50_000)
        stay = mock.patch.dict(PROFILES["low"], {"take_prob": 0}), mock.patch.dict(PROFILES["medium"], {"take_prob": 0}), mock.patch.dict(PROFILES["high"], {"take_prob": 0})
        for patch in stay:
            patch.start()
            self.addCleanup(patch.stop)
        placed = manager.run_cycle(now=10_000)
        self.assertTrue(sum(placed.values()) > 0)
        for row in manager.providers():                      # cash reserved for resting bids is still the firm's
            self.assertEqual((row["equity"], row["pnl"]), (50_000, 0))
        with self.db.readonly() as conn:
            orders = conn.execute("SELECT side, limit_price_minor_units p FROM market_orders WHERE status='open' "
                                  "AND asset_code='CO' AND user_id LIKE 'company:%'").fetchall()
        self.assertTrue(orders)
        self.assertTrue(all(o["p"] < 100 for o in orders if o["side"] == "buy"))
        self.assertEqual(manager.run_cycle(now=10_001), {})                                     # not due yet
        again = manager.run_cycle(now=20_000)
        self.assertEqual(sum(again.values()), 0)                                                 # quotes still fit: nothing replaced

    def test_a_trade_against_a_provider_fills_and_removal_withdraws_quotes(self):
        manager = self.manager()
        manager.provision_firms("admin", capital=50_000)
        manager.run_cycle(now=10_000)
        self.db.assets.add_user_asset("alice", "CO", 5, unit_cost=100)
        self.assertTrue(self.book.place_limit_order("alice", "CO", "sell", 2, 50)[0])
        with self.db.readonly() as conn:
            filled = conn.execute("SELECT COUNT(*) FROM market_orders WHERE user_id LIKE 'company:%' AND status IN ('filled','partial')").fetchone()[0]
        self.assertGreater(filled, 0)
        code = FIRMS[0][1]
        manager.remove_provider(code)
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM liquidity_providers WHERE company_code=?", (code,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM market_orders WHERE user_id=? AND status IN ('open','partial')",
                                          ("company:" + code,)).fetchone()[0], 0)

    def test_a_firm_that_lost_half_its_capital_stops_quoting(self):
        manager = self.manager()
        manager.provision_firms("admin", capital=50_000)
        code = FIRMS[0][1]
        self.db.users.execute("UPDATE users SET cash=? WHERE user_id=?", (10_000, "company:" + code))
        manager.run_cycle(now=10_000)
        row = next(r for r in manager.providers() if r["companyCode"] == code)
        self.assertTrue(row["halted"])
        self.assertEqual(row["openOrders"], 0)
        manager.set_enabled(code, True)
        self.assertFalse(next(r for r in manager.providers() if r["companyCode"] == code)["halted"])

    def test_quotes_are_withdrawn_while_a_price_event_runs_and_when_the_price_runs_through_them(self):
        manager = self.manager()
        manager.provision_firms("admin", capital=50_000)
        manager.run_cycle(now=10_000)

        def resting(side=None):
            with self.db.readonly() as conn:
                return conn.execute("SELECT COUNT(*) FROM market_orders WHERE user_id LIKE 'company:%' AND asset_code='CO' "
                                    "AND status IN ('open','partial')" + (" AND side='%s'" % side if side else "")).fetchone()[0]
        self.assertGreater(resting(), 0)
        self.db.companies.update_share_price("CO", 160)           # a jump far above every ask
        manager.withdraw_stale(10_001)
        self.assertEqual(resting("sell"), 0)                      # stale asks are gone; bids far below stay
        self.db.companies.update_share_price("CO", 100)
        manager.run_cycle(now=20_000)
        self.assertGreater(resting(), 0)
        import time as clock
        from managers.admin_manager import AdminManager
        now = int(clock.time())
        self.assertTrue(AdminManager(self.db).create_market_event("admin", "good", 1, "CO", 600, now=now,
                                                                  target_price=500, target_percent=1)[0])
        manager.run_cycle(now=now + 100)
        self.assertEqual(resting(), 0)                            # nothing quoted on a stock with a running event

    def test_detail_lists_each_stock_with_its_quotes_and_state(self):
        manager = self.manager()
        manager.provision_firms("admin", capital=50_000)
        manager.run_cycle(now=10_000)
        code = next(r["companyCode"] for r in manager.providers() if r["openOrders"] > 0)
        rows = manager.detail(code)
        co = next(r for r in rows if r["assetCode"] == "CO")
        self.assertEqual(co["state"], "quoting")
        self.assertTrue(co["buys"] and all(price < co["price"] for price in co["buys"]))
        manager.set_enabled(code, False)
        self.assertEqual({r["state"] for r in manager.detail(code)}, {"off"})
        with self.assertRaises(ValueError):
            manager.detail("NOPE")


class LiquidityStrategyTests(unittest.TestCase):
    """The four personas (inventory, noise, value, momentum) that trade against the book."""
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    NOW = 1_800_000_000

    def firm(self, code, strategy):
        manager = LiquidityManager(self.db, self.book)
        manager.provision_firms("admin", capital=50_000)
        manager.set_strategy(code, strategy)
        manager.set_enabled(code, True)
        return manager

    def history(self, prices):
        """Price history for CO, one point per 30 s ending at NOW; ``prices`` lists oldest to newest."""
        with self.db.atomic() as conn:
            for index, price in enumerate(prices):
                conn.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch,price_minor_units) VALUES ('CO',?,?,?)",
                             (price, self.NOW - 30 * (len(prices) - 1 - index), price))

    def orders(self, code, side=None):
        with self.db.readonly() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT side,limit_price_minor_units FROM market_orders WHERE user_id=? AND asset_code='CO' AND status IN ('open','partial','filled')"
                + (" AND side=?" if side else ""), ("company:" + code,) + ((side,) if side else ()))]

    def cycle(self, manager, now=None):
        with mock.patch.dict(PROFILES["high"], {"take_prob": 1.0}):
            return manager.run_cycle(now=self.NOW if now is None else now)

    def test_the_strategy_list_and_defaults(self):
        self.assertEqual(set(STRATEGIES), {"inventory", "noise", "value", "momentum"})
        self.assertEqual({row[4] for row in FIRMS}, set(STRATEGIES))
        manager = LiquidityManager(self.db, self.book)
        manager.provision_firms("admin", capital=50_000)
        self.assertEqual({row["strategy"] for row in manager.providers()}, set(STRATEGIES))
        with self.assertRaises(ValueError):
            manager.set_strategy(FIRMS[0][1], "gambler")

    def test_a_value_firm_buys_through_the_market_when_the_price_is_far_below_its_average(self):
        code = next(row[1] for row in FIRMS if row[3] == "high" and row[4] == "value")
        manager = self.firm(code, "value")
        self.history([120] * 50 + [100] * 10)                           # the 30 minute average is 117, the last five minutes sit at the price 100
        self.cycle(manager)
        self.assertTrue(any(limit >= 101 for _, limit in self.orders(code, "buy")), self.orders(code))

    def test_a_value_firm_sells_when_the_price_is_far_above_its_average_and_it_holds_shares(self):
        code = next(row[1] for row in FIRMS if row[3] == "high" and row[4] == "value")
        manager = self.firm(code, "value")
        self.db.assets.add_user_asset("company:" + code, "CO", 100, unit_cost=100)
        self.history([80] * 50 + [100] * 10)
        self.cycle(manager)
        self.assertTrue(any(limit <= 99 for _, limit in self.orders(code, "sell")), self.orders(code))

    def test_a_momentum_firm_follows_a_rising_trend(self):
        code = next(row[1] for row in FIRMS if row[3] == "high" and row[4] == "momentum")
        manager = self.firm(code, "momentum")
        self.db.companies.update_share_price("CO", 110)
        self.history([90] * 50 + [110] * 10)                                                 # the short average is above the long one
        self.cycle(manager, self.NOW + 60)
        self.assertTrue(any(limit >= 111 for _, limit in self.orders(code, "buy")), self.orders(code))

    def test_a_momentum_firm_stays_out_of_a_flat_market(self):
        code = next(row[1] for row in FIRMS if row[3] == "high" and row[4] == "momentum")
        manager = self.firm(code, "momentum")
        self.history([100] * 60)
        self.cycle(manager, self.NOW + 60)
        self.assertFalse(any(limit >= 101 for _, limit in self.orders(code, "buy")), self.orders(code))

    def test_a_noise_firm_trades_inside_its_budget(self):
        code = next(row[1] for row in FIRMS if row[3] == "high" and row[4] == "noise")
        manager = self.firm(code, "noise")
        self.db.assets.add_user_asset("company:" + code, "CO", 50, unit_cost=100)
        for step in range(8):
            self.cycle(manager, self.NOW + step * 100)
        row = next(item for item in manager.providers() if item["companyCode"] == code)
        self.assertGreaterEqual(row["cash"], 0)
        self.assertFalse(row["halted"])

    def test_two_firms_trade_with_each_other_when_one_holds_shares_and_the_other_is_a_buyer(self):
        seller = next(row[1] for row in FIRMS if row[3] == "high" and row[4] == "inventory")
        buyer = next(row[1] for row in FIRMS if row[3] == "high" and row[4] == "value")
        manager = self.firm(seller, "inventory")
        manager.set_strategy(buyer, "value")
        self.db.assets.add_user_asset("company:" + seller, "CO", 400, unit_cost=100)     # the seller carries a large inventory
        self.history([130] * 50 + [100] * 10)                                              # the buyer sees the price as cheap
        self.cycle(manager)
        with self.db.readonly() as conn:
            held = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code='CO'",
                                ("company:" + buyer,)).fetchone()[0]
        self.assertGreater(float(held), 0)

    def test_a_sweep_is_a_small_slice_of_the_company_and_pauses_while_the_price_has_already_moved(self):
        code = next(row[1] for row in FIRMS if row[3] == "high" and row[4] == "value")
        manager = self.firm(code, "value")
        self.history([120] * 50 + [100] * 10)                           # cheap against the average: a buy sweep would follow
        self.cycle(manager)
        sweeps = [limit for side, limit in self.orders(code, "buy") if limit >= 101]
        self.assertTrue(sweeps)
        with self.db.readonly() as conn:
            total_shares = int(conn.execute("SELECT total_shares FROM company WHERE company_code='CO'").fetchone()[0])
            sweep = conn.execute("SELECT MAX(remaining_quantity) FROM market_orders WHERE user_id=? AND asset_code='CO' AND side='buy' "
                                 "AND limit_price_minor_units>=101", ("company:" + code,)).fetchone()[0]
        self.assertLessEqual(float(sweep), 0.002 * total_shares + 1e-9)

    def test_no_persona_trades_while_the_price_is_far_from_its_five_minute_average(self):
        code = next(row[1] for row in FIRMS if row[3] == "high" and row[4] == "value")
        manager = self.firm(code, "value")
        self.history([120] * 50 + [100] * 10)                           # the last five minutes: 100 -> not moved, but the 30 min average is 117
        self.cycle(manager)
        self.assertTrue(any(limit >= 101 for _, limit in self.orders(code, "buy")))
        # now the price is 20% away from its last five-minute average (115): every persona rests
        manager2 = LiquidityManager(self.db, self.book)
        self.db.companies.update_share_price("CO", 140)
        with mock.patch.dict(PROFILES["high"], {"take_prob": 1.0}):
            manager2.run_cycle(now=self.NOW + 200)
        swept = [limit for side, limit in self.orders(code, "buy") if limit >= 141]
        self.assertEqual(swept, [])
