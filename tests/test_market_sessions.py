"""Persistent halts, restart-safe deadlines and bounded event-price paths."""

import math
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.admin_manager import AdminManager
from managers.futures_manager import FuturesManager
from managers.foreign_securities_manager import ForeignSecuritiesManager
from managers.investment_manager import InvestmentManager
from managers.market_manager import MarketManager
from managers.market_session_manager import MarketSessionManager
from managers.option_manager import OptionManager
from managers.order_book_manager import OrderBookManager
from managers.user_manager import UserManager


class MarketSessionTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.system.set_setting("auto_target_enabled", "0")
        self.db.users.create_user("admin", "Admin", "낙원", 1000000, role="admin")
        self.db.users.create_user("alice", "Alice", "낙원", 1000000)
        self.db.users.create_user("bob", "Bob", "낙원", 1000000)
        self.db.companies.create_company("CO", "Company", "Industry", 100000,
                                         10000, 10000, "admin", "EX")
        self.session = MarketSessionManager(self.db)
        self.market = MarketManager(self.db)
        self.users = UserManager(self.db)
        self.book = OrderBookManager(self.db, self.users)
        self.options = OptionManager(self.db, self.users)
        self.futures = FuturesManager(self.db, self.users)
        self.products = InvestmentManager(self.db, self.users)
        self.book.option_manager, self.book.futures_manager = self.options, self.futures

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def event(self, start=1000, goal=13000, percent=100):
        ok, message = AdminManager(self.db).create_market_event(
            "admin", "good" if goal >= 10000 else "bad", percent, "CO", 120,
            now=start, target_price=goal, target_percent=percent)
        self.assertTrue(ok, message)

    def test_unlisted_company_cannot_trade_and_is_no_exchange(self):
        self.db.companies.create_company("PRIV", "Private", "Industry", 100000,
                                         10000, 10000, "admin", MarketSessionManager.UNLISTED)
        self.assertIn("비상장", self.session.trading_error(now=1000, asset_code="PRIV"))
        self.assertIsNone(self.session.trading_error(now=1000, asset_code="CO"))
        with self.db.atomic(("system",)) as conn:
            self.assertNotIn(MarketSessionManager.UNLISTED, MarketSessionManager.exchange_ids(conn))
            self.assertIsNone(conn.execute("SELECT 1 FROM market_exchanges WHERE exchange_id='UNLISTED'").fetchone())

    def test_role_check_and_validation_preserve_state(self):
        before = self.session.get_state(now=1000)
        self.assertFalse(self.session.control("alice", "close", now=1000)[0])
        self.assertFalse(self.session.control("admin", "circuit", stage=True, now=1000)[0])
        self.assertEqual(self.session.get_state(now=1000), before)

    def test_invalid_session_epochs_are_rejected_before_market_state_changes(self):
        before = [tuple(row) for row in self.db.system.execute(
            "SELECT * FROM market_exchanges ORDER BY exchange_id"
        )]
        audit_count = self.db.system.execute_one(
            "SELECT COUNT(*) FROM admin_audit WHERE action LIKE 'market_%'"
        )[0]
        invalid_epochs = (-1, float("nan"), float("inf"), 1e300, True, "invalid")
        for invalid_epoch in invalid_epochs:
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "invalid_market_session_control_epoch"):
                    self.session.control("admin", "pause", now=invalid_epoch)
                with self.assertRaisesRegex(ValueError, "invalid_market_session_processing_epoch"):
                    self.session.process_due(now=invalid_epoch)
                with self.assertRaisesRegex(ValueError, "invalid_market_session_processing_epoch"):
                    self.session.get_state("UNREGISTERED", now=invalid_epoch)
                with self.assertRaisesRegex(ValueError, "invalid_market_session_check_epoch"):
                    self.session.trading_error(now=invalid_epoch, exchange_id="UNREGISTERED")
        self.assertEqual([tuple(row) for row in self.db.system.execute(
            "SELECT * FROM market_exchanges ORDER BY exchange_id"
        )], before)
        self.assertEqual(self.db.system.execute_one(
            "SELECT COUNT(*) FROM admin_audit WHERE action LIKE 'market_%'"
        )[0], audit_count)

    def test_invalid_scheduled_order_epoch_cannot_activate_or_refund_reservations(self):
        self.assertTrue(self.session.control("admin", "pause", now=1000)[0])
        self.assertTrue(self.book.place_limit_order("alice", "CO", "buy", 1, 100)[0])
        order_before = tuple(self.db.assets.execute_one(
            "SELECT status,reserved_cash,updated_epoch FROM market_orders WHERE user_id='alice'"
        ))
        cash_before = self.db.users.get_user("alice")["cash"]
        for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "invalid_scheduled_order_processing_epoch"):
                    self.book.process_scheduled_orders(now=invalid_epoch)
        self.assertEqual(tuple(self.db.assets.execute_one(
            "SELECT status,reserved_cash,updated_epoch FROM market_orders WHERE user_id='alice'"
        )), order_before)
        self.assertEqual(self.db.users.get_user("alice")["cash"], cash_before)

    def test_invalid_option_expiration_epoch_cannot_release_collateral(self):
        self.assertTrue(self.users.buy_asset("alice", "CO", 1)[0])
        self.assertTrue(self.options.create_and_write(
            "alice", "CO", "call", 10000, 1, 1, 100
        )[0])
        contract = self.db.assets.execute_one("SELECT * FROM option_contracts")
        contract_before = tuple(contract)
        collateral_before = [tuple(row) for row in self.db.assets.execute(
            "SELECT * FROM option_collateral_lots WHERE contract_id=?", (contract["contract_id"],)
        )]
        for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "invalid_option_expiration_epoch"):
                    self.options.expire_matured(now=invalid_epoch)
        self.assertEqual(tuple(self.db.assets.execute_one(
            "SELECT * FROM option_contracts WHERE contract_id=?", (contract["contract_id"],)
        )), contract_before)
        self.assertEqual([tuple(row) for row in self.db.assets.execute(
            "SELECT * FROM option_collateral_lots WHERE contract_id=?", (contract["contract_id"],)
        )], collateral_before)

    def test_invalid_futures_settlement_epoch_cannot_reprice_or_close_contract(self):
        ok, message = self.products.issue(
            "admin", "FUT", "Future", "stock_future", 10000, 100, "EX", "CO", 1
        )
        self.assertTrue(ok, message)
        before = tuple(self.db.products.execute_one(
            "SELECT status,unit_price,maturity_epoch FROM investment WHERE product_code='FUT'"
        ))
        for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "invalid_futures_settlement_epoch"):
                    self.futures.settle_positions(now=invalid_epoch)
        self.assertEqual(tuple(self.db.products.execute_one(
            "SELECT status,unit_price,maturity_epoch FROM investment WHERE product_code='FUT'"
        )), before)

    def test_no_due_session_poll_does_not_reserve_writer(self):
        with patch.object(self.db, "atomic", side_effect=AssertionError("unexpected writer")):
            states = self.session.process_due(now=1000)
        self.assertEqual(states["CURRENCY"]["status"], "open")

    def test_manual_halt_freezes_price_history_garch_and_event_then_shifts_once(self):
        self.event(percent=0)
        self.db.system.execute("INSERT INTO market_factor_state VALUES ('market', 1010, .5)")
        self.market.update_prices(now=1010)
        before_price = self.db.companies.get_company("CO")["share_price"]
        before_history = [tuple(r) for r in self.db.assets.execute("SELECT * FROM market_price_history")]
        before_model = dict(self.db.system.execute_one("SELECT * FROM market_simulation_state"))
        before_factor = dict(self.db.system.execute_one("SELECT * FROM market_factor_state"))
        self.assertTrue(self.session.control("admin", "close", now=1010)[0])
        restored = MarketManager(self.db)
        self.assertEqual(restored.update_prices(now=1080), 0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], before_price)
        self.assertEqual([tuple(r) for r in self.db.assets.execute("SELECT * FROM market_price_history")], before_history)
        self.assertEqual(dict(self.db.system.execute_one("SELECT * FROM market_simulation_state")), before_model)
        self.assertEqual(dict(self.db.system.execute_one("SELECT * FROM market_factor_state")), before_factor)
        self.assertTrue(self.session.control("admin", "open", now=1310)[0])
        target = dict(self.db.system.execute_one("SELECT * FROM market_event_targets"))
        self.assertEqual((target["start_epoch"], target["end_epoch"]), (1300, 1420))
        self.assertEqual(target["last_tick_epoch"], 1310)
        restored.update_prices(now=1310)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], before_price)
        self.session.control("admin", "resume", now=1310)
        self.assertEqual(dict(self.db.system.execute_one("SELECT * FROM market_event_targets")), target)
        restored.update_prices(now=1420)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 13000)

    def test_sidecar_persists_across_database_restart_and_uses_scheduled_deadline(self):
        self.event(percent=0)
        self.market.update_prices(now=1010)
        self.assertTrue(self.session.control("admin", "sidecar", now=1010)[0])
        self.db.close()
        self.db = DatabaseManager()
        restored = MarketSessionManager(self.db)
        self.assertEqual(restored.get_state('EX',now=1309)["status"], "paused")
        self.assertEqual(restored.get_state('EX',now=1400)["status"], "open")
        target = self.db.system.execute_one("SELECT * FROM market_event_targets")
        self.assertEqual((target["start_epoch"], target["end_epoch"]), (1300, 1420))
        audit = self.db.system.execute("SELECT * FROM admin_audit WHERE action='market_auto_resume' AND json_extract(payload,'$.exchange_id')='EX'")
        self.assertEqual(len(audit), 1)
        restored.process_due(now=1500)
        self.assertEqual(len(self.db.system.execute("SELECT * FROM admin_audit WHERE action='market_auto_resume' AND json_extract(payload,'$.exchange_id')='EX'")), 1)

    def test_event_created_during_pause_begins_at_reopen(self):
        self.session.control("admin", "pause", now=1000)
        self.event(start=1100, percent=0)
        self.session.control("admin", "resume", now=1400)
        target = self.db.system.execute_one("SELECT * FROM market_event_targets")
        self.assertEqual((target["start_epoch"], target["end_epoch"]), (1400, 1520))
        self.market.update_prices(now=1400)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 10000)
        self.market.update_prices(now=1520)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 13000)

    def test_automatic_targets_and_model_memory_resume_without_downtime_replay(self):
        self.db.system.set_setting("auto_target_enabled", "1")
        self.market.update_prices(now=1000)
        before_target = dict(self.db.system.execute_one("SELECT * FROM market_auto_targets"))
        before_model = dict(self.db.system.execute_one("SELECT * FROM market_simulation_state"))
        before_factor = dict(self.db.system.execute_one("SELECT * FROM market_factor_state"))
        self.session.control("admin", "pause", now=1000)
        self.session.control("admin", "resume", now=1300)
        after_target = dict(self.db.system.execute_one("SELECT * FROM market_auto_targets"))
        after_model = dict(self.db.system.execute_one("SELECT * FROM market_simulation_state"))
        after_factor = dict(self.db.system.execute_one("SELECT * FROM market_factor_state"))
        self.assertEqual(after_target, {**before_target,
            "start_epoch": before_target["start_epoch"]+300,
            "end_epoch": before_target["end_epoch"]+300})
        self.assertEqual(after_model, {**before_model, "last_epoch": before_model["last_epoch"]+300})
        self.assertEqual(after_factor, {**before_factor, "last_epoch": before_factor["last_epoch"]+300})
        self.market.update_prices(now=1300)
        self.assertEqual(dict(self.db.system.execute_one("SELECT * FROM market_simulation_state")), after_model)

    def test_daily_log_cleanup_continues_during_halt_without_changing_model(self):
        first = 1790866800  # A KST midnight, 2026-10-02.
        self.market.update_prices(now=first)
        model = dict(self.db.system.execute_one("SELECT * FROM market_simulation_state"))
        self.session.control("admin", "close", now=first+1)
        self.assertEqual(self.market.update_prices(now=first+86400), 0)
        self.assertEqual(self.db.assets.execute_one("SELECT COUNT(*) FROM market_price_history")[0], 0)
        self.assertEqual(dict(self.db.system.execute_one("SELECT * FROM market_simulation_state")), model)

    def test_circuit_stage_deadlines_and_manual_halt_cannot_be_accidentally_shortened(self):
        for stage in (1, 2):
            self.session.control("admin", "open", now=1000)
            ok, message = self.session.control("admin", "circuit", stage=stage, now=1000)
            self.assertTrue(ok, message)
            self.assertEqual(self.session.get_state('EX',now=1000)["resume_epoch"], 2200)
            self.assertFalse(self.session.control("admin", "sidecar", now=1100)[0])
            self.assertEqual(self.session.get_state('EX',now=2199)["status"], "paused")
            self.assertEqual(self.session.get_state('EX',now=2200)["status"], "open")
        now = 1791000000
        self.assertTrue(self.session.control("admin", "circuit", stage=3, now=now)[0])
        deadline = self.session.get_state('EX',now=now)["resume_epoch"]
        self.assertEqual(deadline, MarketSessionManager.next_day_epoch(now))
        self.assertEqual(self.session.get_state('EX',now=deadline)["status"], "open")
        self.session.control("admin", "close", now=deadline)
        self.assertFalse(self.session.control("admin", "sidecar", now=deadline)[0])
        self.assertEqual(self.session.get_state('EX',now=deadline+999999)["status"], "closed")

    def test_new_trades_are_blocked_without_debits_and_cancel_refunds_during_halt(self):
        cash = self.db.users.get_user("alice")["cash"]
        self.assertTrue(self.book.place_limit_order("alice", "CO", "buy", 10, 1)[0])
        order_id = self.book.get_user_orders("alice")[0]["order_id"]
        after_reserve = self.db.users.get_user("alice")["cash"]
        self.assertEqual(after_reserve, cash-10)
        self.session.control("admin", "pause")
        for trade in (
            lambda: self.users.buy_asset("alice", "CO", 1),
            lambda: self.users.sell_asset("alice", "CO", 1),
            lambda: self.options.create_and_write("alice", "CO", "put", 10000, 1, 1, 100),
        ):
            ok, message = trade()
            self.assertFalse(ok)
            self.assertIn("거래소", message)
        self.assertEqual(self.db.users.get_user("alice")["cash"], after_reserve)
        self.assertTrue(self.book.cancel_order("alice", order_id)[0])
        self.assertEqual(self.db.users.get_user("alice")["cash"], cash)
        self.assertEqual(self.db.assets.execute_one("SELECT COUNT(*) FROM user_assets")[0], 0)
        # Limit orders may be reserved during a halt, but cannot execute.
        self.assertTrue(self.book.place_limit_order('alice','CO','buy',1,10000)[0])
        scheduled = self.db.assets.execute_one("SELECT * FROM market_orders WHERE status='scheduled'")
        self.assertEqual(scheduled['remaining_quantity'],1)
        self.assertEqual(self.db.users.get_user('alice')['cash'],cash-10000)
        self.assertTrue(self.book.cancel_order('alice',scheduled['order_id'])[0])
        self.assertEqual(self.db.users.get_user('alice')['cash'],cash)

    def test_foreign_option_and_fund_native_entry_points_cannot_bypass_halt(self):
        self.assertTrue(self.products.issue("admin", "MF", "Fund", "mutual_fund",
            100, 1000, "EX", "EX", 0)[0])
        self.assertTrue(self.users.buy_asset("alice", "CO", 1)[0])
        self.assertTrue(self.options.create_and_write("alice", "CO", "call", 10000, 1, 1, 100)[0])
        code = self.db.assets.execute_one("SELECT contract_code FROM option_contracts")[0]
        cash = self.db.users.get_user("bob")["cash"]
        self.session.control("admin", "close")
        for action in (
            lambda: self.options.place_limit_order("bob", code, "buy", 1, 100),
            lambda: self.products.trade_fund("bob", "MF", 1),
            lambda: self.products.trade_fund("alice", "MF", 1, redeem=True),
        ):
            ok, message = action()
            self.assertFalse(ok, message)
            self.assertIn("거래소", message)
        self.assertEqual(self.db.users.get_user("bob")["cash"], cash)
        self.assertTrue(ForeignSecuritiesManager(self.book).order('bob','CO','buy',1,100)[0])
        self.assertEqual(self.db.assets.execute_one("SELECT status FROM market_orders WHERE user_id='bob'")[0],'scheduled')

    def test_option_maturity_release_and_bond_maturity_redemption_continue(self):
        self.assertTrue(self.users.buy_asset("alice", "CO", 1)[0])
        self.assertTrue(self.options.create_and_write("alice", "CO", "call", 10000, 1, 1, 100)[0])
        contract = self.db.assets.execute_one("SELECT * FROM option_contracts")
        self.assertTrue(self.users.bond_manager.issue("TB", "Bond", 0, 1, 100, 10000)[0])
        self.assertTrue(self.users.bond_manager.buy("alice", "TB", 100)[0])
        lot = self.db.accounts.execute_one("SELECT * FROM treasury_bond_holdings")
        self.session.control("admin", "pause")
        self.assertFalse(self.users.bond_manager.buy("bob", "TB", 100)[0])
        self.options.expire_matured(now=contract["expiration_epoch"]+1)
        self.assertEqual(self.db.assets.execute_one("SELECT status FROM option_contracts")[0], "expired")
        self.assertEqual(self.db.assets.execute_one("SELECT COUNT(*) FROM option_collateral_lots")[0], 0)
        self.assertEqual(self.db.assets.execute_one("SELECT quantity FROM user_assets WHERE user_id='alice' AND asset_code='CO'")[0], 1)
        self.users.bond_manager.redeem_matured(now=lot["maturity_epoch"]+1)
        self.assertEqual(self.db.accounts.execute_one("SELECT status FROM treasury_bond_holdings")[0], "redeemed")

    def test_invalid_bond_redemption_epoch_cannot_change_lot_or_cash(self):
        self.assertTrue(self.users.bond_manager.issue("TB", "Bond", 0, 1, 100, 10000)[0])
        self.assertTrue(self.users.bond_manager.buy("alice", "TB", 100)[0])
        lot_before = tuple(self.db.accounts.execute_one(
            "SELECT principal,status,last_coupon_epoch FROM treasury_bond_holdings"
        ))
        cash_before = self.db.users.get_user("alice")["cash"]
        for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "invalid_bond_redemption_epoch"):
                    self.users.bond_manager.redeem_matured("alice", now=invalid_epoch)
                self.assertEqual(tuple(self.db.accounts.execute_one(
                    "SELECT principal,status,last_coupon_epoch FROM treasury_bond_holdings"
                )), lot_before)
                self.assertEqual(self.db.users.get_user("alice")["cash"], cash_before)

    def test_nonexpired_futures_do_not_reprice_or_fill_resting_orders_while_halted(self):
        self.assertTrue(self.products.issue("admin", "FUT", "Future", "stock_future",
            10000, 100, "EX", "CO", 1)[0])
        self.assertTrue(self.futures.place_limit_order("alice", "FUT", "buy", 1, 1)[0])
        before = dict(self.db.assets.execute_one("SELECT * FROM market_orders WHERE asset_code='FUT'"))
        self.session.control("admin", "pause")
        self.db.companies.update_share_price("CO", 1)
        now = int(time.time())
        self.futures.settle_positions(now=now)
        self.assertEqual(dict(self.db.assets.execute_one("SELECT * FROM market_orders WHERE asset_code='FUT'")), before)
        self.assertFalse(self.futures.place_limit_order("alice", "FUT", "buy", 1, 1)[0])
        maturity = self.db.products.get_investment("FUT")["maturity_epoch"]
        self.futures.settle_positions(now=maturity)
        self.assertEqual(self.db.products.get_investment("FUT")["status"], "expired")
        self.assertEqual(self.db.assets.execute_one("SELECT status FROM market_orders WHERE asset_code='FUT'")[0], "expired")

    def test_event_tracks_mean_rate_with_bounded_deviation_and_exact_endpoints(self):
        for goal in (13000, 7000):
            self.db.companies.update_share_price("CO", 10000)
            self.event(goal=goal)
            previous = 10000
            observed = []
            with self.db.atomic(("system",)) as conn:
                for elapsed in range(1, 121):
                    now = 1000+elapsed
                    shock = 30 if elapsed % 10 < 5 else -30
                    price = self.market._event_target_price(conn, "CO", previous, now, (),
                                                             sample_noise=lambda target: (shock, 0))
                    baseline = 10000 + (goal-10000)*elapsed/120
                    radius = min(baseline*.10, max(abs(goal-10000)*.12, baseline*.02))
                    band = radius*math.sin(math.pi*elapsed/120)
                    self.assertLessEqual(abs(price-baseline), band+1)
                    self.assertLess(abs(price-previous), 250)
                    observed.append(price-baseline)
                    previous = price
                self.assertEqual(price, goal)
                self.assertTrue(any(offset > 0 for offset in observed))
                self.assertTrue(any(offset < 0 for offset in observed))

    def test_zero_event_noise_follows_mean_line(self):
        self.event(percent=0)
        self.market.update_prices(now=1060)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 11500)
        self.market.update_prices(now=1120)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 13000)


if __name__ == "__main__":
    unittest.main()
