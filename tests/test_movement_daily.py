import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from database.unified import UnifiedConnection
from managers.futures_manager import FuturesManager
from managers.investment_manager import InvestmentManager
from managers.market_manager import MarketManager
from managers.movement_history_manager import MovementHistoryManager
from managers.option_manager import OptionManager
from managers.order_book_manager import OrderBookManager
from managers.user_manager import UserManager


class MovementDailyTests(unittest.TestCase):
    def setUp(self):
        self.original_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.history = MovementHistoryManager(self.db)
        self.midnight = self.history.day_boundary(time.time())[1] + 86400
        self.db.users.create_user("admin", "Admin", "Nac", 1000000, role="admin")
        self.db.users.create_user("alice", "Alice", "Nac", 10000)
        self.db.users.create_user("bob", "Bob", "Nac", 10000)
        self.db.companies.create_company("CO", "Company", "Industry", 100000, 1000, 100, "admin", "EX")
        self.users = UserManager(self.db)
        self.investments = InvestmentManager(self.db, self.users)
        self.options = OptionManager(self.db, self.users)
        self.futures = FuturesManager(self.db, self.users)
        self.book = OrderBookManager(self.db, self.users)
        self.book.option_manager = self.options
        self.book.futures_manager = self.futures

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.original_dir
        self.tempdir.cleanup()

    def seed_histories(self, epoch):
        self.db.assets.execute(
            "INSERT INTO market_price_history (asset_code, price, recorded_epoch) VALUES ('CO', 120, ?)",
            (epoch,),
        )
        self.db.assets.execute(
            "INSERT INTO market_trade_history (asset_code, side, quantity, price, recorded_epoch) "
            "VALUES ('CO', 'buy', 1, 120, ?)", (epoch,),
        )
        self.db.assets.execute(
            "INSERT INTO market_exchange_index_history (exchange_id, index_value, recorded_epoch) "
            "VALUES ('EX', 1100, ?)", (epoch,),
        )

    def issue_future(self):
        self.db.system.set_setting("base_rate", "0")
        success, message = self.investments.issue(
            "admin", "FUT", "Future", "stock_future", 100, 100, "EX", "CO", 10
        )
        self.assertTrue(success, message)

    def test_korean_midnight_removes_previous_day_and_keeps_today_boundary(self):
        for epoch in (self.midnight - 1, self.midnight, self.midnight + 1):
            self.seed_histories(epoch)
        self.assertTrue(self.history.cleanup(self.midnight))
        for table in self.history.HISTORY_TABLES:
            rows = self.db.assets.execute(f"SELECT recorded_epoch FROM {table} ORDER BY recorded_epoch")
            self.assertEqual([row[0] for row in rows], [self.midnight, self.midnight + 1])
        self.assertEqual(
            self.db.system.get_setting("movement_history_date"),
            self.history.day_boundary(self.midnight)[0],
        )
        # KST midnight is still the previous UTC calendar date.
        self.assertEqual(self.midnight % 86400, 15 * 3600)

    def test_invalid_cleanup_epoch_cannot_delete_history_or_write_day_marker(self):
        self.seed_histories(self.midnight - 1)
        before = {
            table: [tuple(row) for row in self.db.assets.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            )]
            for table in self.history.HISTORY_TABLES
        }
        for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "invalid_movement_history_cleanup_epoch"):
                    self.history.cleanup(invalid_epoch)
                with self.assertRaisesRegex(ValueError, "invalid_movement_history_cleanup_epoch"):
                    self.history.day_boundary(invalid_epoch)
        self.assertEqual(self.db.system.get_setting("movement_history_date"), None)
        for table, rows in before.items():
            self.assertEqual([tuple(row) for row in self.db.assets.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            )], rows)

    def test_date_marker_survives_restart_and_skipped_days(self):
        self.seed_histories(self.midnight - 1)
        self.assertTrue(self.history.cleanup(self.midnight))
        self.seed_histories(self.midnight + 1)
        restarted = MovementHistoryManager(self.db)
        self.assertFalse(restarted.cleanup(self.midnight + 200))
        self.assertEqual(len(self.db.assets.execute("SELECT * FROM market_price_history")), 1)
        day_later = self.midnight + 3 * 86400
        self.seed_histories(day_later)
        self.assertTrue(restarted.cleanup(day_later))
        for table in restarted.HISTORY_TABLES:
            self.assertEqual(
                [row[0] for row in self.db.assets.execute(f"SELECT recorded_epoch FROM {table}")],
                [day_later],
            )
        self.assertFalse(MovementHistoryManager(self.db).cleanup(day_later + 1))

    def test_latest_trade_cache_follows_sql_writes_and_preserves_price_after_deletion(self):
        with closing(sqlite3.connect(self.db.assets.db_path, factory=UnifiedConnection)) as conn, conn:
            for epoch, price in ((self.midnight - 2, 80), (self.midnight - 10, 70), (self.midnight - 2, 90)):
                conn.execute(
                    "INSERT INTO market_trade_history (asset_code, side, quantity, price, recorded_epoch) "
                    "VALUES ('CO', 'buy', 1, ?, ?)", (price, epoch),
                )
        self.assertEqual(self.db.assets.get_last_trade("CO")["price"], 90)
        self.assertTrue(self.history.cleanup(self.midnight))
        self.assertEqual(self.db.assets.execute("SELECT * FROM market_trade_history"), [])
        self.assertEqual(self.db.assets.get_last_trade("CO")["price"], 90)
        self.db.assets._init_db()
        self.assertEqual(self.db.assets.get_last_trade("CO")["price"], 90)
        with closing(sqlite3.connect(self.db.assets.db_path, factory=UnifiedConnection)) as conn, conn:
            conn.execute(
                "INSERT INTO market_trade_history (asset_code, side, quantity, price, recorded_epoch) "
                "VALUES ('CO', 'sell', 2, 105, ?)", (self.midnight + 1,),
            )
        self.assertEqual(self.db.assets.get_last_trade("CO")["price"], 105)

    def test_migration_seeds_last_trade_from_existing_history(self):
        for epoch, price in ((self.midnight - 5, 80), (self.midnight - 2, 90), (self.midnight - 2, 95)):
            self.db.assets.execute(
                "INSERT INTO market_trade_history (asset_code, side, quantity, price, recorded_epoch) "
                "VALUES ('CO', 'buy', 1, ?, ?)", (price, epoch),
            )
        self.db.assets.execute("DELETE FROM market_last_trade")
        self.db.assets._init_db()
        self.assertEqual(self.db.assets.get_last_trade("CO")["price"], 95)

    def test_cleanup_keeps_positions_orders_settlements_and_current_etf_index_state(self):
        self.db.assets.add_user_asset("alice", "CO", 2, unit_cost=100)
        self.assertTrue(self.book.place_limit_order("bob", "CO", "buy", 1, 50)[0])
        self.db.assets.execute("INSERT INTO market_exchange_indices VALUES ('EX', 1100, ?)", (self.midnight - 1,))
        self.db.assets.execute("INSERT INTO market_exchange_index_components VALUES ('EX', 'CO', 100, 1000)")
        self.db.assets.execute("INSERT INTO leveraged_etf_state VALUES ('ETF', 'EX', 2, 86400, 1000, 100, ?)", (self.midnight - 1,))
        self.db.assets.execute(
            "INSERT INTO product_settlements (user_id, product_code, kind, quantity, gross_amount, recorded_epoch) "
            "VALUES ('alice', 'OLD', 'futures_maturity', 1, 30, ?)", (self.midnight - 1,),
        )
        protected = (
            "user_assets", "market_orders", "product_settlements", "market_exchange_indices",
            "market_exchange_index_components", "leveraged_etf_state",
        )
        before = {table: [tuple(row) for row in self.db.assets.execute(f"SELECT * FROM {table}")] for table in protected}
        self.seed_histories(self.midnight - 1)
        self.history.cleanup(self.midnight)
        for table in protected:
            self.assertEqual([tuple(row) for row in self.db.assets.execute(f"SELECT * FROM {table}")], before[table])

    def test_option_premium_and_collateral_survive_chart_reset_then_expire_correctly(self):
        self.db.assets.add_user_asset("alice", "CO", 5, unit_cost=100)
        with patch("time.time", return_value=self.midnight - 3600):
            self.assertTrue(self.options.create_and_write("alice", "CO", "call", 100, 1, 2, 10)[0])
            code = self.options.get_active_contracts()[0]["contract_code"]
            self.assertTrue(self.book.place_limit_order("bob", code, "buy", 2, 10)[0])
        before = self.users.get_total_assets("bob", settle=False)
        writer = self.options.get_user_written("alice")[0]
        cash = {uid: self.db.users.get_user(uid)["cash"] for uid in ("alice", "bob")}
        self.assertEqual(self.options.get_contract(code)["price"], 10)
        self.history.cleanup(self.midnight)
        self.assertEqual(self.db.assets.execute("SELECT * FROM market_trade_history"), [])
        self.assertEqual(self.options.get_contract(code)["price"], 10)
        self.assertEqual(self.users.get_total_assets("bob", settle=False)["stock_total"], before["stock_total"])
        self.assertEqual(self.options.get_user_written("alice")[0]["collateral_quantity"], writer["collateral_quantity"])
        self.assertEqual({uid: self.db.users.get_user(uid)["cash"] for uid in cash}, cash)
        self.db.companies.update_share_price("CO", 120)
        self.db.assets.execute("UPDATE option_contracts SET expiration_epoch = ?", (self.midnight + 1,))
        with patch("time.time", return_value=self.midnight + 2):
            results = self.options.expire_matured()
        self.assertTrue(any(item["user_id"] == "bob" and item["exercised"] == 2 for item in results))
        self.assertEqual(self.db.assets.get_user_portfolio("bob")[0]["quantity"], 2)

    def test_previous_day_future_expiry_uses_preserved_sample_for_cash_settlement(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 100)[0])
        self.db.products.execute("UPDATE investment SET maturity_epoch = ? WHERE product_code = 'FUT'", (self.midnight - 1,))
        self.db.assets.execute(
            "INSERT INTO market_price_history (asset_code, price, recorded_epoch) VALUES ('CO', 120, ?)",
            (self.midnight - 2,),
        )
        self.db.companies.update_share_price("CO", 500)
        self.history.cleanup(self.midnight)
        self.assertEqual(self.db.assets.execute("SELECT * FROM market_price_history"), [])
        cached = self.db.assets.execute_one("SELECT * FROM market_future_expiry_prices WHERE product_code = 'FUT'")
        self.assertEqual((cached["price"], cached["is_final"]), (120, 1))
        results = self.futures.settle_positions(now=self.midnight)
        self.assertEqual(results[0]["reason"], "futures_maturity")
        self.assertEqual(self.db.users.get_user("alice")["cash"], 10019)
        self.assertEqual(self.db.products.get_investment("FUT")["status"], "expired")
        self.assertEqual(self.futures.settle_positions(now=self.midnight + 1), [])

    def test_unexpired_future_prefers_newer_today_sample_then_keeps_final_price(self):
        self.issue_future()
        expiry = self.midnight + 10
        self.db.products.execute("UPDATE investment SET maturity_epoch = ? WHERE product_code = 'FUT'", (expiry,))
        self.db.assets.execute(
            "INSERT INTO market_price_history (asset_code, price, recorded_epoch) VALUES ('CO', 120, ?)",
            (self.midnight - 1,),
        )
        self.history.cleanup(self.midnight)
        self.assertEqual(self.db.assets.execute_one("SELECT is_final FROM market_future_expiry_prices")[0], 0)
        for epoch, price in ((self.midnight + 9, 130), (self.midnight + 11, 999)):
            self.db.assets.execute(
                "INSERT INTO market_price_history (asset_code, price, recorded_epoch) VALUES ('CO', ?, ?)",
                (price, epoch),
            )
        with self.db.atomic(("assets", "company", "product")) as conn:
            product = conn.execute("SELECT * FROM product.investment WHERE product_code = 'FUT'").fetchone()
            self.assertEqual(InvestmentManager.linked_price(conn, product, 0, expiry), 130)
            conn.execute("UPDATE assets.market_price_history SET price = 777 WHERE recorded_epoch = ?", (self.midnight + 9,))
            self.assertEqual(InvestmentManager.linked_price(conn, product, 0, expiry + 30), 130)

    def test_failed_deletion_rolls_back_date_marker_and_all_previous_deletions(self):
        self.seed_histories(self.midnight - 1)
        self.db.assets.execute(
            "CREATE TRIGGER fail_movement_delete BEFORE DELETE ON market_trade_history "
            "BEGIN SELECT RAISE(ABORT, 'test deletion failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.history.cleanup(self.midnight)
        self.assertIsNone(self.db.system.get_setting("movement_history_date"))
        for table in self.history.HISTORY_TABLES:
            self.assertEqual(len(self.db.assets.execute(f"SELECT * FROM {table}")), 1)
        self.db.assets.execute("DROP TRIGGER fail_movement_delete")
        self.assertTrue(self.history.cleanup(self.midnight))

    def test_external_write_lock_skips_quickly_and_retries_without_marking_date(self):
        self.seed_histories(self.midnight - 1)
        external = sqlite3.connect(self.db.assets.db_path, isolation_level=None)
        try:
            external.execute("BEGIN IMMEDIATE")
            started = time.perf_counter()
            self.assertFalse(self.history.cleanup(self.midnight))
            self.assertLess(time.perf_counter() - started, 1)
            self.assertIsNone(self.db.system.get_setting("movement_history_date"))
            self.assertEqual(len(self.db.assets.execute("SELECT * FROM market_price_history")), 1)
        finally:
            external.rollback()
            external.close()
        self.assertTrue(self.history.cleanup(self.midnight))
        self.assertEqual(self.db.assets.execute("SELECT * FROM market_price_history"), [])

    def test_market_tick_clears_previous_logs_even_without_active_assets(self):
        self.db.companies.execute("UPDATE company SET status = 'expired' WHERE company_code = 'CO'")
        self.seed_histories(self.midnight - 1)
        market = MarketManager(self.db)
        self.assertEqual(market.update_prices(volatility=0, now=self.midnight), 0)
        for table in self.history.HISTORY_TABLES:
            self.assertEqual(self.db.assets.execute(f"SELECT * FROM {table}"), [])
        self.assertEqual(
            self.db.system.get_setting("movement_history_date"),
            self.history.day_boundary(self.midnight)[0],
        )

    def test_each_chart_reader_removes_previous_day_on_restart_and_keeps_today_samples(self):
        market = MarketManager(self.db)
        readers = (
            ("price", lambda: market.get_price_history("CO", 0)),
            ("trade", lambda: market.get_trade_history("CO", 0)),
            ("index", lambda: market.get_exchange_index_history("EX", 0)),
            ("exchange volume", lambda: market.get_exchange_trade_history("EX", 0)),
        )
        for label, read in readers:
            with self.subTest(reader=label):
                self.db.system.execute("DELETE FROM settings WHERE key = 'movement_history_date'")
                for table in self.history.HISTORY_TABLES:
                    self.db.assets.execute(f"DELETE FROM {table}")
                self.seed_histories(self.midnight - 1)
                self.seed_histories(self.midnight + 1)
                with patch("time.time", return_value=self.midnight + 10):
                    rows = read()
                self.assertEqual([row[0] for row in rows], [self.midnight + 1])
                for table in self.history.HISTORY_TABLES:
                    remaining = self.db.assets.execute(f"SELECT recorded_epoch FROM {table}")
                    self.assertEqual([row[0] for row in remaining], [self.midnight + 1])
                self.assertEqual(
                    self.db.system.get_setting("movement_history_date"),
                    self.history.day_boundary(self.midnight)[0],
                )

    def test_tick_and_restart_read_rebalance_etf_using_old_boundaries_before_cleanup(self):
        self.db.products.register_investment(
            "ETF", "2x ETF", "ETF", 1000, 100, 0, "Admin", "EX",
            underlying_exchange_id="EX", leverage_multiplier=2, rebalance_cycle_seconds=3600,
        )
        for entry in ("market tick", "restart chart read"):
            with self.subTest(entry=entry):
                self.db.system.execute("DELETE FROM settings WHERE key = 'movement_history_date'")
                self.db.products.execute("UPDATE investment SET unit_price = 100 WHERE product_code = 'ETF'")
                self.db.assets.execute("DELETE FROM market_exchange_index_history")
                self.db.assets.execute(
                    "INSERT OR REPLACE INTO market_exchange_indices VALUES ('EX', 1050, ?)",
                    (self.midnight + 90,),
                )
                self.db.assets.execute(
                    "INSERT OR REPLACE INTO market_exchange_index_components VALUES ('EX', 'CO', 100, 1000)"
                )
                self.db.assets.execute(
                    "INSERT OR REPLACE INTO leveraged_etf_state VALUES ('ETF', 'EX', 2, 3600, 1000, 100, ?)",
                    (self.midnight - 7200,),
                )
                for epoch, index in (
                    (self.midnight - 3600, 1100),
                    (self.midnight, 1000),
                    (self.midnight + 90, 1050),
                ):
                    self.db.assets.execute(
                        "INSERT INTO market_exchange_index_history (exchange_id, index_value, recorded_epoch) "
                        "VALUES ('EX', ?, ?)", (index, epoch),
                    )
                market = MarketManager(self.db)
                if entry == "market tick":
                    market.update_prices(volatility=0, now=self.midnight + 100)
                else:
                    with patch("time.time", return_value=self.midnight + 100):
                        market.get_exchange_index_history("EX", 0)
                # Two completed resets compound 100 -> 120 -> 98.18, then the
                # current +5% index move gives NAV 108. Dropping yesterday's
                # 1100 boundary first would incorrectly produce NAV 110.
                self.assertEqual(self.db.products.get_investment("ETF")["unit_price"], 108)
                state = self.db.assets.execute_one("SELECT * FROM leveraged_etf_state WHERE product_code = 'ETF'")
                self.assertEqual(state["last_rebalance_epoch"], self.midnight)
                self.assertEqual(
                    self.db.assets.execute(
                        "SELECT * FROM market_exchange_index_history WHERE recorded_epoch < ?",
                        (self.midnight,),
                    ),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
