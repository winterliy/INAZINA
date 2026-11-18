"""Liquidity companies: independent 법인 funded from the treasury that quote the order book algorithmically."""
import unittest

from managers.liquidity_manager import FIRMS, LiquidityManager, PROFILES, quote_prices
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
        placed = manager.run_cycle(now=10_000)
        self.assertTrue(sum(placed.values()) > 0)
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
