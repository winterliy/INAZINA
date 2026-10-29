"""Resting orders that the startup gap-fill prices reached execute against the listing treasury."""

import tempfile
import time
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.currency_manager import CurrencyManager
from managers.gap_order_settler import settle_gap_orders
from managers.order_book_manager import OrderBookManager
from managers.user_manager import UserManager

# Orders are placed at wall time; the restart (and its drawn prices) comes an hour later.
NOW = int(time.time()) + 3600


class GapOrderSettlerTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.system.set_setting("auto_target_enabled", "0")
        self.db.users.create_user("admin", "Admin", "낙원", 1000000, role="admin")
        self.db.users.create_user("alice", "Alice", "낙원", 1000000)
        self.db.users.create_user("bob", "Bob", "낙원", 1000000)
        self.db.companies.create_company("CO", "Company", "Industry", 100000, 10000, 10000, "admin", "EX")
        self.book = OrderBookManager(self.db, UserManager(self.db))
        with self.db.atomic(("assets",)) as conn:
            self.treasury = CurrencyManager.exchange_treasury(conn, "CO")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def drawn(self, *prices, filled_epoch=NOW):
        """Pretend the startup gap fill drew ``prices`` for CO just before ``filled_epoch``."""
        start = filled_epoch - len(prices) - 1
        with self.db.atomic(("assets",)) as conn:
            conn.executemany("INSERT INTO market_price_history(asset_code, price, recorded_epoch) VALUES ('CO', ?, ?)",
                             [(price, start + 1 + index) for index, price in enumerate(prices)])
            conn.execute("INSERT INTO market_price_gap_fills VALUES ('CO', ?, ?, ?, ?)",
                         (start, filled_epoch, len(prices), filled_epoch))

    def order(self, order_id):
        with self.db.atomic(("assets",)) as conn:
            return dict(conn.execute("SELECT * FROM market_orders WHERE order_id=?", (order_id,)).fetchone())

    def shares(self, user_id):
        with self.db.atomic(("assets",)) as conn:
            return int(conn.execute("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code='CO'",
                                    (user_id,)).fetchone()[0])

    def cash(self, user_id):
        return int(self.db.users.get_user(user_id)["cash"])

    def place(self, user_id, side, quantity, price):
        ok, message = self.book.place_limit_order(user_id, "CO", side, quantity, price)
        self.assertTrue(ok, message)
        with self.db.atomic(("assets",)) as conn:
            return int(conn.execute("SELECT MAX(order_id) FROM market_orders WHERE user_id=?", (user_id,)).fetchone()[0])

    def test_buy_reached_by_a_dip_fills_from_unissued_supply_at_its_limit(self):
        order_id = self.place("alice", "buy", 3, 9000)
        self.assertEqual(self.order(order_id)["status"], "open")
        treasury_before = self.cash(self.treasury)
        self.drawn(9800, 8900, 9500)
        self.assertEqual(settle_gap_orders(self.book, now=NOW), [(order_id, 3)])
        self.assertEqual(self.order(order_id)["status"], "filled")
        self.assertEqual(self.shares("alice"), 3)
        self.assertEqual(self.cash(self.treasury) - treasury_before, 27000)

    def test_sell_is_bought_by_the_treasury_and_resold_to_a_later_buy(self):
        self.place("alice", "buy", 5, 10000)
        self.assertEqual(self.shares("alice"), 5)
        sell_id = self.place("alice", "sell", 5, 12000)
        alice_before, treasury_before = self.cash("alice"), self.cash(self.treasury)
        self.drawn(11000, 12500, 11800)
        self.assertEqual(settle_gap_orders(self.book, now=NOW), [(sell_id, 5)])
        self.assertEqual(self.order(sell_id)["status"], "filled")
        self.assertEqual(self.shares("alice"), 0)
        self.assertEqual(self.shares(self.treasury), 5)
        received = self.cash("alice") - alice_before
        self.assertTrue(0 < received < 60000)                            # 5 × 12,000 less sale-gain tax
        # The tax goes back to the same treasury: no money is made or lost.
        self.assertEqual(treasury_before - self.cash(self.treasury), received)

        buy_id = self.place("bob", "buy", 7, 9000)
        self.drawn(9500, 8800, filled_epoch=NOW + 100)
        self.assertEqual(settle_gap_orders(self.book, now=NOW + 100), [(buy_id, 7)])
        self.assertEqual(self.shares(self.treasury), 0)                 # its own 5 first …
        self.assertEqual(self.shares("bob"), 7)                         # … then 2 newly issued

    def test_orders_the_drawn_prices_did_not_reach_stay_open(self):
        buy_id = self.place("alice", "buy", 1, 8000)
        self.drawn(9800, 8100, 9500)
        self.assertEqual(settle_gap_orders(self.book, now=NOW), [])
        self.assertEqual(self.order(buy_id)["status"], "open")

    def test_only_this_starts_fill_counts(self):
        buy_id = self.place("alice", "buy", 1, 9000)
        self.drawn(8500, filled_epoch=NOW - 500)   # an earlier start's fill
        self.assertEqual(settle_gap_orders(self.book, now=NOW), [])
        self.assertEqual(self.order(buy_id)["status"], "open")


if __name__ == "__main__":
    unittest.main()
