"""The statistics page's numbers: built from existing records plus the realized-result ledger, at today's rates."""
import unittest
from decimal import Decimal

from managers.pnl_ledger import PnlLedger
from managers.stats_manager import StatsManager, period_start
from tests import test_market_features as fixtures


class StatsManagerTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue
    issue_future = fixtures.MarketFeatureTests.issue_future

    def _stats(self, user, period="all", now=None):
        with self.db.readonly() as conn:
            return StatsManager.build(conn, user, period, now)

    def test_sales_taxes_and_trade_counts_add_up(self):
        self.db.assets.add_user_asset("alice", "CO", 4, unit_cost=100)
        self.assertTrue(self.book.place_limit_order("alice", "CO", "sell", 2, 90)[0])
        self.assertTrue(self.book.place_limit_order("bob", "CO", "buy", 2, 90)[0])
        self.db.companies.update_share_price("CO", 140)
        self.assertTrue(self.book.place_limit_order("alice", "CO", "sell", 2, 130)[0])
        self.assertTrue(self.book.place_limit_order("bob", "CO", "buy", 2, 130)[0])
        stats = self._stats("alice")
        trade = next(item for item in stats["sources"] if item["key"] == "stock_sale")
        self.assertEqual((trade["income"], trade["loss"], trade["net"], trade["count"]), ("60", "20", "40", 2))
        tax = Decimal(stats["totals"]["tax"])
        self.assertGreater(tax, 0)
        self.assertEqual(Decimal(stats["totals"]["net"]), Decimal("40") - tax)
        self.assertEqual([item["label"] for item in stats["taxes"]], ["주식·상품 양도세"])
        self.assertEqual((stats["trades"]["wins"], stats["trades"]["results"], stats["trades"]["winRate"]), (1, 2, 50.0))
        self.assertEqual((stats["trades"]["maxGain"], stats["trades"]["maxLoss"]), ("60", "-20"))
        self.assertEqual(stats["trades"]["sells"], 2)
        self.assertEqual(self._stats("bob")["trades"]["buys"], 2)

    def test_the_period_limits_what_is_counted(self):
        with self.db.atomic() as conn:
            PnlLedger.record(conn, "alice", "stock_sale", "old", "NWX", 0, 500, 100, now=1_000)
            PnlLedger.record(conn, "alice", "stock_sale", "new", "NWX", 0, 300, 100, now=10_000_000)
        self.assertEqual(self._stats("alice", "all", now=10_000_100)["totals"]["income"], "600")
        recent = self._stats("alice", "7d", now=10_000_100)
        self.assertEqual((recent["totals"]["income"], recent["since"]), ("200", 10_000_100 - 7 * 86400))
        with self.assertRaises(ValueError):
            period_start("decade", 1)

    def test_amounts_follow_the_live_exchange_rate_into_the_users_own_currency(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','Dollar','1500',2)")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('US','USD')")
        self.db.users.create_user("us-user", "Us", "US", 0)
        with self.db.atomic() as conn:
            PnlLedger.record(conn, "us-user", "stock_sale", "r1", "NWX", 0, 150_000, 0, now=5)
        first = self._stats("us-user")
        self.assertEqual((first["home"], first["totals"]["income"]), ("USD", "100.00"))   # 150,000 NWX at 1 USD = 1,500 NWX
        self.db.currency.execute("UPDATE currencies SET reference_rate='3000' WHERE code='USD'")
        self.assertEqual(self._stats("us-user")["totals"]["income"], "50.00")             # a new rate gives new figures

    def test_a_loss_converts_to_the_mirror_image_of_the_same_gain(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','Dollar','1499.9',2)")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('US','USD')")
        self.db.users.create_user("us-user", "Us", "US", 0)
        with self.db.atomic() as conn:
            PnlLedger.record(conn, "us-user", "stock_sale", "win", "NWX", 0, 60_000, 0, now=5)
            PnlLedger.record(conn, "us-user", "stock_sale", "lose", "NWX", 0, 0, 60_000, now=6)
        trades = self._stats("us-user")["trades"]
        self.assertEqual(trades["maxGain"], trades["maxLoss"].lstrip("-"))

    def test_holdings_not_yet_sold_show_their_unrealized_result(self):
        self.db.assets.add_user_asset("alice", "CO", 5, unit_cost=80)
        stats = self._stats("alice")
        item = next(row for row in stats["unrealized"]["items"] if row["code"] == "CO")
        self.assertEqual(Decimal(item["pnl"]), Decimal("100") * 5 - Decimal("80") * 5)
        self.assertEqual(Decimal(stats["unrealized"]["total"]), Decimal(item["pnl"]))


if __name__ == "__main__":
    unittest.main()
