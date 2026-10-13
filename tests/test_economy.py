import tempfile
import time
import unittest
import sqlite3
import discord
from types import SimpleNamespace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from database.user_db import TREASURY_USER_ID
from managers.bank_manager import BankManager
from managers.admin_manager import AdminManager
from managers.interest_manager import InterestManager
from managers.market_manager import MarketManager
from managers.user_manager import UserManager
from managers.order_book_manager import OrderBookManager
from main import NaconomicsBot
from functions.admin_access import BOOTSTRAP_ADMIN_IDS, resolve_user_role
from functions.discord_views import (
    AllAssetsView,
    HelpLinksView,
    InfoPanelView,
    MyOrdersView,
    PriceMovementView,
    RegistrationModal,
)
from functions.price_chart import render_price_chart
from functions.slash_commands import SlashCommandRegistry
from functions.registration import RegistrationRequired


class EconomyTests(unittest.TestCase):
    def setUp(self):
        self.original_database_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.users = UserManager(self.db)
        self.bank = BankManager(self.db)
        self.admin = AdminManager(self.db)
        self.db.users.create_user("citizen-1", "Citizen 1", "Nac", 10000)
        self.db.users.create_user("citizen-2", "Citizen 2", "Nac", 0)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.original_database_dir
        self.tempdir.cleanup()

    def test_buy_sell_tracks_funding_account_and_taxes_realized_gain(self):
        self.db.assets.register_asset("TEST", "Test Co", "EX", 10, "stock")
        self.assertTrue(self.users.buy_asset("citizen-1", "TEST", 10)[0])
        self.db.assets.update_price("TEST", 12)
        success, message = self.users.sell_asset("citizen-1", "TEST", 5)
        self.assertTrue(success, message)
        self.assertIn("과세", message)
        self.assertEqual(self.db.assets.get_user_portfolio("citizen-1")[0]["quantity"], 5)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 9960)
        tax = self.db.tax.execute_one("SELECT tax_amount FROM tax_transactions")
        self.assertEqual(tax["tax_amount"], 0)
        self.assertEqual(self.db.users.get_treasury_balance(), 0)

    def test_limit_order_matches_at_maker_price_reserves_and_refunds_cash(self):
        self.db.assets.register_asset("BOOK", "Order Book Co", "EX", 100, "stock")
        self.db.users.update_cash("citizen-2", 1000)
        self.assertTrue(self.users.buy_asset("citizen-2", "BOOK", 5)[0])
        order_book = OrderBookManager(self.db, self.users)

        success, ask_message = order_book.place_limit_order(
            "citizen-2", "BOOK", "sell", 2, 90
        )
        self.assertTrue(success, ask_message)
        success, bid_message = order_book.place_limit_order(
            "citizen-1", "BOOK", "buy", 2, 120
        )
        self.assertTrue(success, bid_message)
        self.assertIn("체결대금 180 NWX", bid_message)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 9820)
        self.assertEqual(self.db.users.get_user("citizen-2")["cash"], 680)
        holdings = {
            row["user_id"]: int(row["quantity"])
            for row in self.db.assets.execute(
                "SELECT user_id, SUM(quantity) AS quantity FROM user_assets "
                "WHERE asset_code = 'BOOK' GROUP BY user_id"
            )
        }
        self.assertEqual(holdings, {"citizen-1": 2, "citizen-2": 3})
        fill = self.db.assets.execute_one(
            "SELECT side, quantity, price FROM market_trade_history "
            "WHERE asset_code = 'BOOK' ORDER BY trade_id DESC LIMIT 1"
        )
        self.assertEqual((fill["quantity"], fill["price"]), (2, 90))

        success, pending = order_book.place_limit_order(
            "citizen-1", "BOOK", "buy", 1, 50
        )
        self.assertTrue(success, pending)
        pending_order = order_book.get_user_open_orders("citizen-1")[0]
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 9770)
        pending_assets = self.users.get_total_assets("citizen-1")
        self.assertEqual(pending_assets["order_reserved_total"], 50)
        self.assertEqual(pending_assets["grand_total"], 10000)
        self.assertTrue(order_book.cancel_order("citizen-1", pending_order["order_id"])[0])
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 9820)

        success, _ = order_book.place_limit_order("citizen-2", "BOOK", "sell", 2, 200)
        self.assertTrue(success)
        direct_success, direct_message = self.users.sell_asset("citizen-2", "BOOK", 2)
        self.assertFalse(direct_success)
        self.assertIn("보유 수량이 부족", direct_message)
        sell_order = order_book.get_user_open_orders("citizen-2")[0]
        seller_assets = self.users.get_total_assets("citizen-2")
        seller_position = next(row for row in seller_assets["stocks"] if row["code"] == "BOOK")
        self.assertEqual(seller_position["sell_reserved_qty"], 2)
        self.assertTrue(order_book.cancel_order("citizen-2", sell_order["order_id"])[0])

    def test_limit_buy_cannot_exceed_owned_plus_open_buy_supply_cap(self):
        self.db.companies.create_company(
            "ISSUE", "New Issue", "Industry", 500, 5, 100,
            "citizen-1", "EX-ISSUE",
        )
        self.db.assets.add_user_asset("citizen-1", "ISSUE", 5, unit_cost=100)
        order_book = OrderBookManager(self.db, self.users)

        success, message = order_book.place_limit_order(
            "citizen-1", "ISSUE", "buy", 1, 100
        )

        self.assertFalse(success)
        self.assertIn("총 발행량 한도", message)
        self.assertIn("추가 주문 가능 0개", message)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 10000)
        self.assertEqual(order_book.get_user_open_orders("citizen-1"), [])

    def test_limit_buy_reservations_count_toward_finite_supply_cap(self):
        self.db.companies.create_company(
            "ISSUE", "New Issue", "Industry", 500, 5, 100,
            "citizen-1", "EX-ISSUE",
        )
        self.db.assets.add_user_asset("citizen-1", "ISSUE", 3, unit_cost=100)
        order_book = OrderBookManager(self.db, self.users)

        success, first_message = order_book.place_limit_order(
            "citizen-1", "ISSUE", "buy", 2, 50
        )
        self.assertTrue(success, first_message)
        success, second_message = order_book.place_limit_order(
            "citizen-1", "ISSUE", "buy", 1, 50
        )

        self.assertFalse(success)
        self.assertIn("미체결 매수 2개", second_message)
        self.assertIn("추가 주문 가능 0개", second_message)
        self.assertEqual(len(order_book.get_user_open_orders("citizen-1")), 1)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 9900)

    def test_limit_buy_primary_issuance_still_respects_remaining_total_supply(self):
        self.db.companies.create_company(
            "ISSUE", "New Issue", "Industry", 500, 5, 100,
            "citizen-1", "EX-ISSUE",
        )
        self.db.assets.add_user_asset("citizen-1", "ISSUE", 4, unit_cost=100)
        self.db.users.update_cash("citizen-2", 1000)
        order_book = OrderBookManager(self.db, self.users)

        success, message = order_book.place_limit_order(
            "citizen-2", "ISSUE", "buy", 2, 100
        )

        self.assertTrue(success, message)
        self.assertIn("즉시 체결 1개", message)
        self.assertIn("미체결 1개", message)
        total_held = self.db.assets.execute_one(
            "SELECT SUM(quantity) AS quantity FROM user_assets WHERE asset_code = 'ISSUE'"
        )["quantity"]
        self.assertEqual(total_held, 5)
        self.assertEqual(self.db.users.get_treasury_balance(), 100)
        order = order_book.get_user_open_orders("citizen-2")[0]
        self.assertEqual(order["remaining_quantity"], 1)
        self.assertEqual(order["reserved_cash"], 100)
        self.assertTrue(order_book.cancel_order("citizen-2", order["order_id"])[0])
        self.assertEqual(self.db.users.get_user("citizen-2")["cash"], 900)

    def test_leveraged_and_inverse_etfs_follow_exchange_index_daily_base(self):
        self.db.companies.create_company(
            "LEVER-CO", "Leverage Underlying", "Industry", 10000, 100, 100,
            "citizen-1", "EX-LEVER",
        )
        self.db.products.register_investment(
            "ETF2", "2x ETF", "ETF", 1000, 100, 0, "Manager", "EX-LEVER",
            underlying_exchange_id="EX-LEVER", leverage_multiplier=2,
            rebalance_cycle_seconds=86400,
        )
        self.db.products.register_investment(
            "INVERSE", "Inverse ETF", "ETF", 1000, 100, 0, "Manager", "EX-LEVER",
            underlying_exchange_id="EX-LEVER", leverage_multiplier=-1,
            rebalance_cycle_seconds=86400,
        )
        market = MarketManager(self.db)
        now = int(time.time()) - 5
        market.update_prices(volatility=0, now=now)
        self.assertEqual(self.db.products.get_investment("ETF2")["unit_price"], 100)

        self.db.companies.update_share_price("LEVER-CO", 110)
        market.update_prices(volatility=0, now=now + 1)
        self.assertEqual(self.db.products.get_investment("ETF2")["unit_price"], 120)
        self.assertEqual(self.db.products.get_investment("INVERSE")["unit_price"], 90)

    def test_gamble_chance_is_sql_managed_percent(self):
        self.db.system.set_setting("gamble_chance", "100")
        success, message = self.users.gamble("citizen-1", 100)
        self.assertTrue(success, message)
        self.assertAlmostEqual(self.db.users.get_user("citizen-1")["cash"], 10000 + 97)
        self.assertEqual(self.db.users.get_treasury_balance(), -97)

    def test_gamble_return_percent_is_sql_managed(self):
        self.db.system.set_setting("gamble_chance", "100")
        self.db.system.set_setting("gamble_return", "50")

        success, message = self.users.gamble("citizen-1", 100)

        self.assertTrue(success, message)
        self.assertIn("지급률 50.00%", message)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 10048)
        self.assertEqual(self.db.users.get_treasury_balance(), -48)

    def test_gambling_loss_is_deposited_into_treasury(self):
        self.db.system.set_setting("gamble_chance", "0")

        success, message = self.users.gamble("citizen-1", 100)

        self.assertFalse(success)
        self.assertIn("국고에 입금", message)
        self.assertAlmostEqual(self.db.users.get_user("citizen-1")["cash"], 9900)
        self.assertAlmostEqual(self.db.users.get_treasury_balance(), 100)

    def test_government_bond_sale_funds_treasury_and_maturity_redeems_integer_nwx(self):
        bonds = self.users.bond_manager
        success, message = bonds.issue("GOV1", "시민 국채", 365, 1, 1000, 20000)
        self.assertTrue(success, message)
        success, message = bonds.buy("citizen-1", "GOV1", 1500)
        self.assertFalse(success)
        self.assertIn("액면가 1,000 NWX", message)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 10000)
        success, message = bonds.buy("citizen-1", "GOV1", 10000)
        self.assertTrue(success, message)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 0)
        self.assertEqual(self.db.users.get_treasury_balance(), 10000)
        holding = self.db.accounts.execute_one(
            "SELECT holding_id FROM treasury_bond_holdings WHERE user_id = 'citizen-1'"
        )
        now = int(time.time())
        self.db.accounts.execute(
            "UPDATE treasury_bond_holdings SET purchased_epoch = ?, maturity_epoch = ? "
            "WHERE holding_id = ?",
            (now - 86401, now - 1, holding["holding_id"]),
        )

        redeemed = bonds.redeem_matured("citizen-1", now=now)
        self.assertEqual(len(redeemed), 1)
        self.assertEqual(redeemed[0]["interest"], 100)
        self.assertEqual(redeemed[0]["tax"], 3)
        self.assertEqual(redeemed[0]["net"], 10097)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 10097)
        self.assertEqual(self.db.users.get_treasury_balance(), -97)
        self.assertEqual(
            self.db.accounts.execute_one(
                "SELECT status FROM treasury_bond_holdings WHERE holding_id = ?",
                (holding["holding_id"],),
            )["status"],
            "redeemed",
        )

    def test_bond_issue_requires_total_supply_to_match_fixed_face_value(self):
        success, message = self.users.bond_manager.issue(
            "GOV2", "단위 불일치 국채", 5, 365, 3000, 10000
        )
        self.assertFalse(success)
        self.assertIn("고정 액면가의 배수", message)
        self.assertIsNone(
            self.db.products.execute_one(
                "SELECT 1 FROM treasury_bond_products WHERE product_code = 'GOV2'"
            )
        )

    def test_sql_managed_bond_product_enforces_fixed_denomination(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.products.execute(
                "INSERT INTO treasury_bond_products "
                "(product_code, name, annual_rate, maturity_days, face_value, "
                "exchange_id, market_price, total_supply) "
                "VALUES ('SQLBAD', '잘못된 액면', 5, 365, 3000, 'BOND', 3000, 10000)"
            )

        self.db.products.execute(
            "INSERT INTO treasury_bond_products "
            "(product_code, name, annual_rate, maturity_days, face_value, "
            "exchange_id, market_price, total_supply) "
            "VALUES ('SQLGOOD', 'SQL 등록 국채', 5, 365, 2500, 'BOND', 2500, 10000)"
        )
        market_asset = MarketManager(self.db).get_asset_info("SQLGOOD")
        self.assertEqual((market_asset["type"], market_asset["price"]), ("국채", 2500))

    def test_government_bonds_trade_on_exchange_and_keep_maturity_claim(self):
        self.db.users.update_cash("citizen-2", 10000)
        bonds = self.users.bond_manager
        success, message = bonds.issue(
            "EXBOND", "거래소 국채", 365, 1, 1000, 2000, "EX-BOND"
        )
        self.assertTrue(success, message)

        order_book = OrderBookManager(self.db, self.users)
        success, message = order_book.place_limit_order("citizen-1", "EXBOND", "buy", 2, 1000)
        self.assertTrue(success, message)
        success, message = order_book.place_limit_order("citizen-1", "EXBOND", "sell", 1, 1200)
        self.assertTrue(success, message)
        success, message = order_book.place_limit_order("citizen-2", "EXBOND", "buy", 1, 1200)
        self.assertTrue(success, message)

        market = MarketManager(self.db)
        asset = market.get_asset_info("EXBOND")
        self.assertEqual((asset["exchange_id"], asset["price"], asset["type"]),
                         ("EX-BOND", 1200, "국채"))
        self.assertIn("EXBOND", {item["code"] for item in market.get_market_assets()})
        self.assertIn("EXBOND", {item["code"] for item in market.get_exchange_assets("EX-BOND")})
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 9194)
        self.assertEqual(self.db.users.get_user("citizen-2")["cash"], 8800)
        self.assertEqual(self.db.users.get_treasury_balance(), 2006)

        buyer_holding = self.db.accounts.execute_one(
            "SELECT holding_id FROM treasury_bond_holdings "
            "WHERE user_id = 'citizen-2' AND product_code = 'EXBOND' AND status = 'active'"
        )
        self.db.accounts.execute(
            "UPDATE treasury_bond_holdings SET maturity_epoch = ? WHERE holding_id = ?",
            (int(time.time()) - 1, buyer_holding["holding_id"]),
        )
        redeemed = bonds.redeem_matured("citizen-2")
        self.assertEqual(len(redeemed), 1)
        self.assertEqual(redeemed[0]["principal"], 1000)
        self.assertEqual(self.db.users.get_user("citizen-2")["cash"], 9810)

    def test_fractional_money_input_is_rejected_and_direct_sql_amounts_are_rounded(self):
        success, message = self.users.transfer("citizen-1", "citizen-2", "1.5")
        self.assertFalse(success)
        self.assertIn("정수 NWX", message)
        self.db.users.execute(
            "UPDATE users SET cash = 123.4 WHERE user_id = 'citizen-1'"
        )
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 123)

    def test_basic_income_can_drive_treasury_balance_negative(self):
        self.db.users.update_cash(TREASURY_USER_ID, 100)
        success, message = self.admin.distribute_basic_income(25)
        self.assertTrue(success, message)
        self.assertAlmostEqual(self.db.users.get_user("citizen-1")["cash"], 10025)
        self.assertAlmostEqual(self.db.users.get_user("citizen-2")["cash"], 25)
        self.assertEqual(self.db.users.get_treasury_balance(), 50)

        self.db.users.update_cash(TREASURY_USER_ID, -99)
        before = self.db.users.get_user("citizen-1")["cash"]
        success, message = self.admin.distribute_basic_income(1)
        self.assertTrue(success, message)
        self.assertIn("적자", message)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], before + 1)
        self.assertEqual(self.db.users.get_treasury_balance(), -51)

    def test_saving_and_loan_product_cannot_be_reused(self):
        self.db.products.register_saving("SAVE", "Saving", 3, 100, 365, 3600, 0, "Bank")
        self.assertTrue(self.bank.open_account("citizen-1", "SAVE", "1234", 100)[0])
        success, message = self.bank.open_account("citizen-1", "SAVE", "1234", 100)
        self.assertFalse(success)
        self.assertIn("이미", message)

        self.db.products.register_liability("LOAN", "Loan", 3, 5000, 365, 3600, 600, "Bank")
        self.assertTrue(self.bank.apply_loan("citizen-1", "LOAN", 1000)[0])
        success, message = self.bank.apply_loan("citizen-1", "LOAN", 1000)
        self.assertFalse(success)
        self.assertIn("이미", message)

    def test_loan_auto_creates_payout_account_and_is_repayable(self):
        self.db.products.register_liability("LOAN", "Loan", 3, 5000, 365, 3600, 600, "New Bank")
        success, message = self.bank.apply_loan("citizen-1", "LOAN", 1000)
        self.assertTrue(success, message)
        account = self.db.accounts.get_user_accounts("citizen-1")[0]
        self.assertEqual(account["account_type"], "loan_payout")
        self.assertEqual(account["balance"], 1000)
        self.assertTrue(self.bank.repay_loan("citizen-1", "LOAN", 250)[0])
        loan = self.db.accounts.execute_one("SELECT outstanding_balance FROM loans WHERE user_id='citizen-1'")
        self.assertEqual(loan["outstanding_balance"], 750)

    def test_account_close_requires_correct_password(self):
        self.db.products.register_saving("SAVE", "Saving", 3, 100, 365, 3600, 0, "Bank")
        self.assertTrue(self.bank.open_account("citizen-1", "SAVE", "1234", 1000)[0])
        account = self.db.accounts.get_user_accounts("citizen-1")[0]

        success, message = self.bank.close_account(
            "citizen-1", account["account_number"], password="wrong"
        )
        self.assertFalse(success)
        self.assertIn("비밀번호", message)
        self.assertIsNotNone(self.db.accounts.get_account(account["account_number"]))

        success, message = self.bank.close_account(
            "citizen-1", account["account_number"], password="1234"
        )
        self.assertTrue(success, message)
        self.assertIsNone(self.db.accounts.get_account(account["account_number"]))
        self.assertAlmostEqual(self.db.users.get_user("citizen-1")["cash"], 10000)

    def test_account_password_hash_verifies_the_exact_untrimmed_input(self):
        self.db.products.register_saving("EXACT", "Exact Password", 0, 0, 365, 3600, 0, "Bank")
        password = " 12 34 "
        self.assertTrue(self.bank.open_account("citizen-1", "EXACT", password, 0)[0])
        account = self.db.accounts.get_user_accounts("citizen-1")[0]
        stored_password = account["password"]

        self.assertTrue(stored_password.startswith("pbkdf2_sha256$"))
        self.assertTrue(self.bank._verify_password(password, stored_password))
        self.assertFalse(self.bank._verify_password(password.strip(), stored_password))
        self.assertFalse(self.bank.close_account(
            "citizen-1", account["account_number"], password=password.strip()
        )[0])
        self.assertTrue(self.bank.close_account(
            "citizen-1", account["account_number"], password=password
        )[0])

    def test_scheduled_loan_repayment_catches_up_due_installments(self):
        self.db.products.register_liability(
            "AUTO", "Auto Pay Loan", 0, 5000, 365, 3600, 600, "Bank",
            repayment_cycle_seconds=60, repayment_amount=250,
        )
        self.assertTrue(self.bank.apply_loan("citizen-1", "AUTO", 1000)[0])
        payout = self.db.accounts.get_user_accounts("citizen-1")[0]
        self.db.users.execute("UPDATE users SET cash = 0 WHERE user_id = 'citizen-1'")
        now = time.time()
        self.db.accounts.execute(
            "UPDATE loans SET last_repayment_epoch = ? WHERE user_id = 'citizen-1'",
            (now - 121,),
        )

        paid, missed, notifications = self.bank.process_scheduled_loan_payments(now=now)

        self.assertEqual((paid, missed), (2, 0))
        self.assertEqual(notifications[0]["paid_amount"], 500)
        self.assertEqual(notifications[0]["remaining"], 500)
        loan = self.db.accounts.execute_one(
            "SELECT outstanding_balance FROM loans WHERE user_id = 'citizen-1'"
        )
        self.assertEqual(loan["outstanding_balance"], 500)
        self.assertEqual(self.db.accounts.get_account(payout["account_number"])["balance"], 500)

    def test_missed_scheduled_loan_payment_reduces_credit_once_per_due_period(self):
        self.db.products.register_liability(
            "AUTO", "Auto Pay Loan", 0, 5000, 365, 3600, 600, "Bank",
            repayment_cycle_seconds=60, repayment_amount=250,
        )
        self.assertTrue(self.bank.apply_loan("citizen-1", "AUTO", 1000)[0])
        self.db.users.execute("UPDATE users SET cash = 0 WHERE user_id = 'citizen-1'")
        self.db.accounts.execute("UPDATE accounts SET balance = 0 WHERE user_id = 'citizen-1'")
        now = time.time()
        self.db.accounts.execute(
            "UPDATE loans SET last_repayment_epoch = ? WHERE user_id = 'citizen-1'",
            (now - 61,),
        )

        paid, missed, notifications = self.bank.process_scheduled_loan_payments(now=now)
        repeated_paid, repeated_missed, repeated_notifications = self.bank.process_scheduled_loan_payments(now=now)

        self.assertEqual((paid, missed), (0, 1))
        self.assertEqual(len(notifications), 1)
        self.assertEqual((repeated_paid, repeated_missed, repeated_notifications), (0, 0, []))
        self.assertEqual(self.db.users.get_user("citizen-1")["credit_score"], 699)

    def test_zero_loan_limit_means_unlimited(self):
        self.db.products.register_liability("OPEN", "No Limit Loan", 3, 0, 365, 3600, 600, "Open Bank")

        success, message = self.bank.apply_loan("citizen-1", "OPEN", 10000000)

        self.assertTrue(success, message)
        loan = self.db.accounts.execute_one(
            "SELECT principal FROM loans WHERE user_id = 'citizen-1' AND product_code = 'OPEN'"
        )
        self.assertEqual(loan["principal"], 10000000)

    def test_interest_accepts_legacy_text_values_and_uses_seconds(self):
        self.db.products.register_saving("SAVE", "Saving", "36", 0, 0, "1", 0, "Bank")
        self.assertTrue(self.bank.open_account("citizen-1", "SAVE", "1234", 1000)[0])
        account = self.db.accounts.get_user_accounts("citizen-1")[0]
        self.db.accounts.execute(
            "UPDATE accounts SET last_payment_epoch = ? WHERE account_number = ?",
            (time.time() - 4, account["account_number"]),
        )
        last = self.db.accounts.get_account(account["account_number"])["last_payment_epoch"]
        count, net_interest, loan_count = InterestManager(self.db).process_all_interests(
            now=last + 4
        )
        self.assertEqual((count, loan_count), (0, 0))
        self.assertEqual(net_interest, 0)
        updated = self.db.accounts.get_account(account["account_number"])
        self.assertEqual(updated["balance"], 1000)
        count, net_interest, loan_count = InterestManager(self.db).process_all_interests(
            now=last + InterestManager.YEAR_SECONDS / 2
        )
        self.assertEqual((count, loan_count), (1, 0))
        self.assertEqual(net_interest, 175)
        self.assertEqual(
            self.db.accounts.get_account(account["account_number"])["balance"], 1175
        )

    def test_interest_is_settled_lazily_when_assets_are_viewed(self):
        self.db.products.register_saving(
            "LAZY", "Lazy Interest", 876000, 0, 0, 3600, 0, "Bank"
        )
        self.assertTrue(self.bank.open_account("citizen-1", "LAZY", "1234", 1000)[0])
        account = self.db.accounts.get_user_accounts("citizen-1")[0]
        self.db.accounts.execute(
            "UPDATE accounts SET last_payment_epoch = ? WHERE account_number = ?",
            (time.time() - 7201, account["account_number"]),
        )

        viewed = self.users.get_total_assets("citizen-1")
        balance_after_first_view = self.db.accounts.get_account(account["account_number"])["balance"]
        self.users.get_total_assets("citizen-1")
        balance_after_second_view = self.db.accounts.get_account(account["account_number"])["balance"]

        self.assertGreater(viewed["account_total"], 1000)
        self.assertAlmostEqual(balance_after_first_view, balance_after_second_view)
        self.assertEqual(
            self.db.tax.execute_one("SELECT COUNT(*) AS n FROM tax_transactions WHERE source='saving_interest'")["n"],
            1,
        )

    def test_read_only_asset_snapshot_does_not_settle_interest(self):
        self.db.products.register_saving(
            "SNAPSHOT", "Snapshot Savings", 876000, 0, 0, 3600, 0, "Bank"
        )
        self.assertTrue(self.bank.open_account("citizen-1", "SNAPSHOT", "1234", 1000)[0])
        account = self.db.accounts.get_user_accounts("citizen-1")[0]
        self.db.accounts.execute(
            "UPDATE accounts SET last_payment_epoch = ? WHERE account_number = ?",
            (time.time() - 7201, account["account_number"]),
        )

        snapshot = self.users.get_total_assets("citizen-1", settle=False)

        self.assertEqual(snapshot["account_total"], 1000)
        self.assertEqual(
            self.db.accounts.get_account(account["account_number"])["balance"], 1000
        )
        self.assertEqual(
            self.db.tax.execute_one(
                "SELECT COUNT(*) AS n FROM tax_transactions WHERE source='saving_interest'"
            )["n"],
            0,
        )

    def test_fractional_interest_cycles_count_due_ticks_without_float_loss(self):
        self.assertEqual(InterestManager._due_periods(0.3, 0.1), 3)

    def test_saving_interest_tax_is_deposited_into_treasury(self):
        self.db.users.execute("UPDATE users SET cash = 2000000 WHERE user_id = 'citizen-1'")
        self.db.products.register_saving("TAXSAVE", "Tax Saving", 525600, 0, 0, 1, 0, "Bank")
        self.assertTrue(self.bank.open_account("citizen-1", "TAXSAVE", "1234", 1000000)[0])
        account = self.db.accounts.get_user_accounts("citizen-1")[0]
        self.db.accounts.execute(
            "UPDATE accounts SET last_payment_epoch = ? WHERE account_number = ?",
            (time.time() - 2, account["account_number"]),
        )

        count, _net_interest, loan_count = InterestManager(self.db).process_all_interests()

        self.assertEqual((count, loan_count), (1, 0))
        tax = self.db.tax.execute_one("SELECT tax_amount FROM tax_transactions WHERE source='saving_interest'")
        self.assertIsNotNone(tax)
        self.assertGreater(tax["tax_amount"], 0)
        self.assertEqual(self.db.users.get_treasury_balance(), tax["tax_amount"])

    def test_global_code_collision_blocks_trade(self):
        self.db.assets.register_asset("DUPE", "Market", "EX", 10, "stock")
        self.db.products.register_saving("DUPE", "Savings", 3, 0, 0, 3600, 0, "Bank")
        success, message = self.users.buy_asset("citizen-1", "DUPE", 1)
        self.assertFalse(success)
        self.assertIn("중복 등록", message)

    def test_simultaneous_transfers_cannot_overspend(self):
        self.db.users.execute("UPDATE users SET cash = 10000 WHERE user_id = 'citizen-1'")
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(
                lambda _index: self.users.transfer("citizen-1", "citizen-2", 1000),
                range(20),
            ))
        successful = sum(1 for success, _message in results if success)
        self.assertEqual(successful, 10)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 0)
        self.assertEqual(self.db.users.get_user("citizen-2")["cash"], 10000)

    def test_overlapping_atomic_database_work_is_serialized_in_process(self):
        from threading import Event

        second_started = Event()
        second_entered = Event()

        def second_transaction():
            second_started.set()
            with self.db.atomic(("users", "accounts")):
                second_entered.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.db.atomic(("accounts", "users")):
                future = pool.submit(second_transaction)
                self.assertTrue(second_started.wait(timeout=1))
                self.assertFalse(second_entered.wait(timeout=0.05))
            self.assertTrue(second_entered.wait(timeout=1))
            future.result(timeout=1)

    def test_transfer_can_deposit_to_another_users_account_number(self):
        self.db.users.update_cash("citizen-2", 2000)
        self.db.products.register_saving("RECIPIENT", "Recipient Savings", 0, 0, 0, 3600, 0, "Bank")
        self.assertTrue(self.bank.open_account("citizen-2", "RECIPIENT", "1234", 1000)[0])
        account = self.db.accounts.execute_one(
            "SELECT account_number FROM accounts WHERE user_id = 'citizen-2'"
        )

        success, message = self.users.transfer(
            "citizen-1", account["account_number"], 250, "__wallet__"
        )

        self.assertTrue(success, message)
        self.assertAlmostEqual(self.db.users.get_user("citizen-1")["cash"], 9750)
        self.assertAlmostEqual(
            self.db.accounts.get_account(account["account_number"])["balance"], 1250
        )

    def test_transfer_can_deposit_to_treasury(self):
        starting_treasury = self.db.users.get_treasury_balance()

        success, message = self.users.transfer(
            "citizen-1", TREASURY_USER_ID, 750
        )

        self.assertTrue(success, message)
        self.assertIn("국고", message)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 9250)
        self.assertEqual(self.db.users.get_treasury_balance(), starting_treasury + 750)

    def test_transfer_splits_payment_across_sender_accounts_when_selected_account_is_short(self):
        self.db.products.register_saving("SRC_A", "Source A", 0, 0, 0, 3600, 0, "Bank")
        self.db.products.register_saving("SRC_B", "Source B", 0, 0, 0, 3600, 0, "Bank")
        self.assertTrue(self.bank.open_account("citizen-1", "SRC_A", "1234", 300)[0])
        self.assertTrue(self.bank.open_account("citizen-1", "SRC_B", "5678", 400)[0])
        accounts = self.db.accounts.execute(
            "SELECT product_code, account_number FROM accounts WHERE user_id = 'citizen-1'"
        )
        account_numbers = {row["product_code"]: row["account_number"] for row in accounts}

        success, message = self.users.transfer(
            "citizen-1", "citizen-2", 600, account_numbers["SRC_A"]
        )

        self.assertTrue(success, message)
        self.assertIn(account_numbers["SRC_A"], message)
        self.assertIn(account_numbers["SRC_B"], message)
        self.assertEqual(self.db.accounts.get_account(account_numbers["SRC_A"])["balance"], 0)
        self.assertEqual(self.db.accounts.get_account(account_numbers["SRC_B"])["balance"], 100)
        self.assertEqual(self.db.users.get_user("citizen-2")["cash"], 600)

    def test_transfer_between_own_accounts_and_account_to_wallet(self):
        self.db.products.register_saving("SOURCE", "Source Savings", 0, 0, 0, 3600, 0, "Bank")
        self.db.products.register_saving("DEST", "Destination Savings", 0, 0, 0, 3600, 0, "Bank")
        self.assertTrue(self.bank.open_account("citizen-1", "SOURCE", "1234", 1000)[0])
        self.assertTrue(self.bank.open_account("citizen-1", "DEST", "1234", 500)[0])
        accounts = self.db.accounts.execute(
            "SELECT account_number, product_code FROM accounts WHERE user_id = 'citizen-1' "
            "ORDER BY product_code"
        )
        source = next(row["account_number"] for row in accounts if row["product_code"] == "SOURCE")
        destination = next(row["account_number"] for row in accounts if row["product_code"] == "DEST")

        success, message = self.users.transfer("citizen-1", destination, 200, source)

        self.assertTrue(success, message)
        self.assertEqual(self.db.accounts.get_account(source)["balance"], 800)
        self.assertEqual(self.db.accounts.get_account(destination)["balance"], 700)
        self.assertEqual(self.db.accounts.get_account(destination)["principal"], 700)

        success, message = self.users.transfer("citizen-1", "citizen-1", 100, source)

        self.assertTrue(success, message)
        self.assertEqual(self.db.accounts.get_account(source)["balance"], 700)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], 8600)

    def test_transfer_rejects_same_source_and_destination_account(self):
        self.db.products.register_saving("ONLY", "Only Savings", 0, 0, 0, 3600, 0, "Bank")
        self.assertTrue(self.bank.open_account("citizen-1", "ONLY", "1234", 1000)[0])
        account = self.db.accounts.execute_one(
            "SELECT account_number FROM accounts WHERE user_id = 'citizen-1'"
        )

        success, message = self.users.transfer(
            "citizen-1", account["account_number"], 100, account["account_number"]
        )

        self.assertFalse(success)
        self.assertIn("같은 계좌", message)
        self.assertEqual(self.db.accounts.get_account(account["account_number"])["balance"], 1000)

    def test_discord_nickname_sync_updates_existing_users(self):
        self.db.users.update_nickname("citizen-1", "Updated Display Name")
        self.db.users.update_nicknames([("citizen-2", "Another Display Name")])

        self.assertEqual(
            self.db.users.get_user("citizen-1")["nickname"], "Updated Display Name"
        )
        self.assertEqual(
            self.db.users.get_user("citizen-2")["nickname"], "Another Display Name"
        )

        self.admin.setup_user("citizen-1", "Setup Display Name")
        self.assertEqual(
            self.db.users.get_user("citizen-1")["nickname"], "Setup Display Name"
        )

    def test_market_reads_latest_sql_price_for_all_asset_tables(self):
        self.db.assets.register_asset("MKT", "Market", "EX", 10, "stock")
        self.db.companies.create_company("CO", "Company", "Industry", 1000, 100, 20, "citizen-1", "EX")
        self.db.products.register_investment("ETF", "Fund", "ETF", 100, 30, 0, "Manager", "EX")
        market = MarketManager(self.db)
        self.db.assets.update_price("MKT", 77)
        self.db.companies.update_share_price("CO", 88)
        self.db.products.execute("UPDATE investment SET unit_price=99 WHERE product_code='ETF'")
        market.update_prices(volatility=0)
        self.assertEqual(market.get_asset_info("MKT")["price"], 77)
        self.assertEqual(market.get_asset_info("CO")["price"], 88)
        self.assertEqual(market.get_asset_info("ETF")["price"], 99)
        history = self.db.assets.execute(
            "SELECT asset_code, price FROM market_price_history ORDER BY asset_code"
        )
        self.assertEqual({row["asset_code"] for row in history}, {"CO", "ETF", "MKT"})

    def test_fixed_price_can_be_edited_in_sql_while_the_loop_runs(self):
        self.db.assets.register_asset("FIXED", "Fixed Price", "EX", 100, "stock")
        self.db.assets.execute(
            "UPDATE market_assets SET price = 4321, is_fixed = 1 WHERE code = 'FIXED'"
        )

        MarketManager(self.db).update_prices(now=1000)

        self.assertEqual(self.db.assets.get_asset_info("FIXED")["price"], 4321)
        history = self.db.assets.execute_one(
            "SELECT price FROM market_price_history WHERE asset_code = 'FIXED'"
        )
        self.assertEqual(history["price"], 4321)

        self.db.assets.execute("UPDATE market_assets SET is_fixed = 0 WHERE code = 'FIXED'")
        self.db.assets.update_price("FIXED", 1234)
        MarketManager(self.db).update_prices(volatility=0, now=1001)
        self.assertEqual(self.db.assets.get_asset_info("FIXED")["price"], 1234)

    def test_movement_view_has_period_components_and_generates_png(self):
        self.db.assets.register_asset("CHART", "Chart Asset", "EX", 10, "stock")
        market = MarketManager(self.db)
        market.update_prices(volatility=0)
        view = PriceMovementView(
            SimpleNamespace(market_mgr=market), "citizen-1", "CHART"
        )

        embed, png = view.render()

        self.assertEqual(
            [option.label for option in view.period_select.options],
            ["1분", "5분", "10분", "30분", "1시간", "6시간", "12시간", "1일"],
        )
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(embed.image.url, "attachment://movement.png")
        self.assertGreater(len(render_price_chart([(1, 10), (2, 12)], 60)), 100)

    def test_movement_exchange_index_tracks_market_cap_weighted_company_returns(self):
        self.db.companies.create_company(
            "INDEX-CO", "Index Company", "Industry", 10000, 100, 100,
            "citizen-1", "EXCHANGE-A",
        )
        market = MarketManager(self.db)
        now = int(time.time()) - 5

        market.update_prices(volatility=0, now=now)
        self.assertEqual(market.get_exchange_index_info("EXCHANGE-A")["index_value"], 1000)

        self.db.companies.update_share_price("INDEX-CO", 110)
        market.update_prices(volatility=0, now=now + 1)
        self.assertEqual(market.get_exchange_index_info("EXCHANGE-A")["index_value"], 1100)
        self.assertEqual(len(market.get_exchange_index_history("EXCHANGE-A", now)), 2)

        view = PriceMovementView(
            SimpleNamespace(market_mgr=market),
            "citizen-1",
            market.exchange_index_code("EXCHANGE-A"),
        )
        with patch("functions.discord_views.render_price_chart", return_value=b"chart"):
            embed, png = view.render()
        self.assertIn("거래소 지수", embed.title)
        self.assertIn("1,100.00", embed.description)
        self.assertEqual(png, b"chart")

    def test_all_assets_view_pages_every_citizen_without_duplicates(self):
        for index in range(3, 24):
            self.db.users.create_user(f"citizen-{index}", f"Citizen {index}", "Nac", 0)
        users = self.db.users.execute(
            "SELECT user_id, nickname FROM users WHERE status = 'active' ORDER BY nickname"
        )
        view = AllAssetsView(
            SimpleNamespace(user_mgr=self.users), "viewer", users
        )

        shown = []
        for page in range(3):
            view.page = page
            embed = view.build_embed()
            shown.extend(field.name for field in embed.fields)
            self.assertIn(f"{page + 1}/3 페이지", embed.footer.text)
            self.assertLessEqual(len(embed), 6000)
        self.assertEqual(len(shown), len(users))
        self.assertEqual(len(set(shown)), len(users))

    def test_my_orders_view_has_interactive_cancel_components(self):
        self.db.assets.register_asset("ORDVIEW", "주문 보기", "EX", 100, "stock")
        order_book = OrderBookManager(self.db, self.users)
        success, message = order_book.place_limit_order(
            "citizen-1", "ORDVIEW", "buy", 2, 100
        )
        self.assertTrue(success, message)
        bot = SimpleNamespace(market_mgr=MarketManager(self.db), order_book=order_book)
        view = MyOrdersView(
            bot,
            "citizen-1",
            order_book.get_user_open_orders("citizen-1"),
        )

        embed = view.build_embed(order_book.get_user_orders("citizen-1"))

        self.assertEqual(len(view.children), 2)
        self.assertTrue(view.cancel_button.disabled)
        self.assertIn("ORDVIEW", embed.description)

    def test_help_links_view_points_to_nakwon_bot_policies(self):
        view = HelpLinksView()

        self.assertEqual(len(view.children), 1)
        self.assertEqual(
            view.children[0].url,
            "https://dramatic-bread-b4a.notion.site/NAKWON-BOTS-3ea27b50c7bf80e0904ef783e0e5dfc4",
        )
        self.assertEqual(view.children[0].style, discord.ButtonStyle.link)
        self.assertEqual(view.children[0].label, "도움말 바로가기")

    def test_help_command_sends_only_the_link_component(self):
        import asyncio

        sent = []

        class FakeResponse:
            done = False

            def is_done(self):
                return self.done

            async def defer(self, **_kwargs):
                self.done = True

            async def send_message(self, **kwargs):
                self.done = True
                sent.append(kwargs)

        class FakeFollowup:
            async def send(self, **kwargs):
                sent.append(kwargs)

        async def ensure_user(_user_id, _nickname):
            return False

        bot = SimpleNamespace(
            ensure_user=ensure_user,
            db=self.db,
        )
        interaction = SimpleNamespace(
            response=FakeResponse(),
            followup=FakeFollowup(),
            user=SimpleNamespace(id="citizen-1", display_name="Citizen 1", name="citizen1"),
            command=SimpleNamespace(name="도움말"),
        )
        asyncio.run(SlashCommandRegistry(bot).help(interaction))

        self.assertEqual(len(sent), 1)
        self.assertNotIn("embed", sent[0])
        self.assertNotIn("content", sent[0])
        self.assertEqual(sent[0]["view"].children[0].label, "도움말 바로가기")

    def test_registration_notice_uses_private_scrollable_modal_and_end_code(self):
        text = RegistrationModal.load_notice_text()
        self.assertTrue(text)
        self.assertIn("개인정보", text)

        modal = RegistrationModal(SimpleNamespace(), "citizen-new", "New Citizen", text)
        self.assertEqual(modal.title, "Inazina 등록 안내")
        self.assertEqual(len(modal.children), 2)
        self.assertIsInstance(modal.notice_display, discord.ui.TextDisplay)
        self.assertEqual(modal.notice_display.content, modal.display_content)
        self.assertIn(text, modal.display_content)
        self.assertIn(
            f"[📚 상세 약관 바로가기]({HelpLinksView.DOCUMENT_URL})",
            modal.display_content,
        )
        self.assertIn(modal.confirmation_code, modal.display_content)
        self.assertEqual(modal.confirmation_input.max_length, 6)
        components = modal.to_components()
        self.assertEqual(components[0]["type"], 10)  # Read-only Text Display
        self.assertIn(HelpLinksView.DOCUMENT_URL, components[0]["content"])

    def test_registration_modal_requires_end_code_then_registers(self):
        import asyncio

        calls = []

        class FakeResponse:
            def __init__(self):
                self.kwargs = None

            async def send_message(self, content=None, **kwargs):
                if content is not None:
                    kwargs["content"] = content
                self.kwargs = kwargs

        async def register_user(user_id, nickname):
            calls.append((user_id, nickname))
            return True

        modal = RegistrationModal(
            SimpleNamespace(register_user=register_user),
            "citizen-new",
            "New Citizen",
            RegistrationModal.load_notice_text(),
        )
        modal.confirmation_input._value = modal.confirmation_code.lower()
        interaction = SimpleNamespace(response=FakeResponse())

        asyncio.run(modal.on_submit(interaction))

        self.assertEqual(calls, [("citizen-new", "New Citizen")])
        self.assertTrue(interaction.response.kwargs["ephemeral"])
        self.assertIn("등록이 완료", interaction.response.kwargs["content"])

    def test_registration_command_opens_modal_as_initial_response(self):
        import asyncio

        async def ensure_user(_user_id, _nickname):
            return False

        class FakeResponse:
            def __init__(self):
                self.modal = None
                self.message = None

            async def send_modal(self, modal):
                self.modal = modal

            async def send_message(self, **kwargs):
                self.message = kwargs

        bot = SimpleNamespace(ensure_user=ensure_user, db=self.db)
        self.db.currency.execute(
            "INSERT OR REPLACE INTO guild_countries(guild_id,nationality) VALUES (?,?)",
            ("123456", "낙원"),
        )
        interaction = SimpleNamespace(
            response=FakeResponse(),
            guild=SimpleNamespace(id="123456"),
            user=SimpleNamespace(
                id="citizen-not-registered",
                display_name="New Citizen",
                name="new-citizen",
            ),
        )

        asyncio.run(SlashCommandRegistry(bot).self_register(interaction))

        self.assertIsInstance(interaction.response.modal, RegistrationModal)
        self.assertIsNone(interaction.response.message)

    def test_slash_command_schema(self):
        class FakeTree:
            def __init__(self):
                self.commands = []

            def add_command(self, command):
                self.commands.append(command)

        tree = FakeTree()
        registry = SlashCommandRegistry(SimpleNamespace(tree=tree))
        registry.register()

        names = {command.name for command in tree.commands}
        self.assertIn("등록", names)
        self.assertIn("내지갑", names)
        self.assertTrue({"setup", "register", "사건", "상품등록", "기본소득"}.isdisjoint(names))
        self.assertTrue({"내옵션", "외환계좌", "국적", "내외환", "환율", "내자산"}.isdisjoint(names))
        self.assertTrue({"회생신청", "파산신청"}.issubset(names))
        self.assertTrue({"기준금리", "법정최고이자", "펀드가입", "펀드환매", "선물증거금"}.issubset(names))
        for name in ("회생신청", "파산신청"):
            command = next(command for command in tree.commands if command.name == name)
            self.assertEqual(command.parameters, [])
        self_register = next(command for command in tree.commands if command.name == "등록")
        self.assertEqual(self_register.parameters, [])
        self.assertIn("검색", names)
        self.assertIn("그래프", names)
        self.assertTrue({"search", "movement"}.isdisjoint(names))
        self.assertNotIn("register", names)
        self.assertTrue({"호가", "내주문", "매수", "매도"}.issubset(names))
        self.assertNotIn("국채", names)
        self.assertTrue({"국채매수", "내국채", "주문취소", "국채발행"}.isdisjoint(names))
        self.assertNotIn("info", names)
        self.assertNotIn("setupall", names)
        buy = next(command for command in tree.commands if command.name == "매수")
        self.assertNotIn("신용확인", names)
        asset = next(parameter for parameter in buy.parameters if parameter.name == "자산")
        self.assertFalse(asset.autocomplete)
        self.assertTrue(any(parameter.name == "지정가" for parameter in buy.parameters))
        sell = next(command for command in tree.commands if command.name == "매도")
        self.assertFalse(next(parameter for parameter in sell.parameters if parameter.name == "자산").autocomplete)
        transfer = next(command for command in tree.commands if command.name == "송금")
        self.assertTrue(next(parameter for parameter in transfer.parameters if parameter.name == "수신자").autocomplete)
        self.assertTrue(next(parameter for parameter in transfer.parameters if parameter.name == "출금계좌").autocomplete)
        movement = next(command for command in tree.commands if command.name == "그래프")
        movement_asset = next(parameter for parameter in movement.parameters if parameter.name == "종목")
        self.assertFalse(movement_asset.autocomplete)
        self.assertTrue(movement_asset.required)
        for command_name, argument_name in {
            "선물증거금": "선물", "펀드가입": "펀드", "펀드환매": "펀드"
        }.items():
            parameter = next(
                parameter for parameter in next(command for command in tree.commands if command.name == command_name).parameters
                if parameter.name == argument_name
            )
            self.assertTrue(parameter.required)
            self.assertFalse(parameter.autocomplete)
        for name in ("기준금리", "법정최고이자"):
            parameter = next(command for command in tree.commands if command.name == name).parameters[0]
            self.assertEqual(parameter.name, "국가")
            self.assertTrue(parameter.required)
            self.assertTrue(parameter.autocomplete)

    def test_financial_relief_commands_dm_admins_with_asset_page_balances_only(self):
        import asyncio

        admin_id = "222222222222222222"
        self.db.users.create_user(admin_id, "Admin", "Nac", 0, role="admin")
        original_cash = self.db.users.get_user("citizen-1")["cash"]
        balances = {
            "cash": 500,
            "account_total": 1000,
            "stock_total": 2000,
            "bond_total": 500,
            "order_reserved_total": 100,
            "grand_total": 4100,
            "loan_total": 700,
            "net_worth": 3400,
        }

        class FakeResponse:
            def __init__(self):
                self.done = False
                self.messages = []

            def is_done(self):
                return self.done

            async def defer(self, **_kwargs):
                self.done = True

            async def send_message(self, **kwargs):
                self.done = True
                self.messages.append(kwargs)

        class FakeFollowup:
            def __init__(self):
                self.messages = []

            async def send(self, **kwargs):
                self.messages.append(kwargs)

        class FakeAdmin:
            def __init__(self):
                self.messages = []

            async def send(self, content, **kwargs):
                self.messages.append((content, kwargs))

        admin_by_id = {
            admin_id: FakeAdmin(),
            next(iter(BOOTSTRAP_ADMIN_IDS)): FakeAdmin(),
        }
        looked_up_ids = []
        asset_snapshot_calls = []

        async def ensure_user(_user_id, _nickname):
            return False

        async def fetch_user(user_id):
            return admin_by_id[str(user_id)]

        def get_user(user_id):
            looked_up_ids.append(str(user_id))
            return admin_by_id.get(str(user_id))

        def get_total_assets(user_id, *, settle=True):
            asset_snapshot_calls.append((str(user_id), settle))
            return balances

        bot = SimpleNamespace(
            ensure_user=ensure_user,
            db=self.db,
            user_mgr=SimpleNamespace(get_total_assets=get_total_assets),
            get_user=get_user,
            fetch_user=fetch_user,
        )
        response = FakeResponse()
        followup = FakeFollowup()
        interaction = SimpleNamespace(
            response=response,
            followup=followup,
            user=SimpleNamespace(
                id="citizen-1", display_name="Citizen One", name="citizen-one"
            ),
            command=SimpleNamespace(name="회생신청"),
        )

        asyncio.run(SlashCommandRegistry(bot).apply_rehabilitation(interaction))

        expected_admin_ids = {admin_id, *BOOTSTRAP_ADMIN_IDS}
        self.assertEqual(set(looked_up_ids), expected_admin_ids, repr(looked_up_ids))
        self.assertEqual(asset_snapshot_calls, [("citizen-1", False)])
        self.assertEqual(len(admin_by_id[admin_id].messages), 1)
        self.assertEqual(len(admin_by_id[next(iter(BOOTSTRAP_ADMIN_IDS))].messages), 1)
        message = admin_by_id[admin_id].messages[0][0]
        self.assertTrue(message.startswith("[Citizen One (citizen-1)가 회생을 신청했습니다."))
        self.assertIn("현금: 500 NWX", message)
        self.assertIn("투자자산: 2,500 NWX", message)
        self.assertIn("매수 주문 예치금: 100 NWX", message)
        self.assertIn("총 자산: 4,100 NWX", message)
        self.assertIn("대출 잔액: 700 NWX", message)
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], original_cash)
        self.assertIn("관리자 2명", response.messages[0]["content"])

        # Bankruptcy is the same notification-only workflow, with no financial action.
        for admin in admin_by_id.values():
            admin.messages.clear()
        followup.messages.clear()
        interaction.response = FakeResponse()
        interaction.command = SimpleNamespace(name="파산신청")
        asyncio.run(SlashCommandRegistry(bot).apply_bankruptcy(interaction))
        self.assertEqual(asset_snapshot_calls, [("citizen-1", False), ("citizen-1", False)])
        self.assertTrue(
            admin_by_id[admin_id].messages[0][0].startswith(
                "[Citizen One (citizen-1)가 파산을 신청했습니다."
            )
        )
        self.assertEqual(self.db.users.get_user("citizen-1")["cash"], original_cash)

    def test_slash_reply_omits_missing_file_attachment(self):
        import asyncio

        class FakeResponse:
            def __init__(self):
                self.kwargs = None
                self.done = False

            def is_done(self):
                return self.done

            async def send_message(self, **kwargs):
                self.kwargs = kwargs

        class FakeFollowup:
            async def send(self, **kwargs):
                raise AssertionError("A first response should use response.send_message")

        interaction = SimpleNamespace(response=FakeResponse())
        interaction.followup = FakeFollowup()
        asyncio.run(SlashCommandRegistry._reply(interaction, content="hello"))

        self.assertNotIn("file", interaction.response.kwargs)
        self.assertNotIn("view", interaction.response.kwargs)
        self.assertNotIn("embed", interaction.response.kwargs)
        self.assertEqual(interaction.response.kwargs["content"], "hello")

    def test_slash_prepare_defers_only_when_requested(self):
        import asyncio

        calls = []

        class FakeResponse:
            done = False

            def is_done(self):
                return self.done

            async def defer(self, **kwargs):
                calls.append(("defer", kwargs))
                self.done = True

        async def ensure_user(user_id, nickname):
            calls.append(("ensure_user", user_id, nickname))

        bot = SimpleNamespace(
            ensure_user=ensure_user,
            db=SimpleNamespace(users=SimpleNamespace(get_user=lambda _user_id: {"role": "user"})),
        )
        interaction = SimpleNamespace(
            response=FakeResponse(),
            user=SimpleNamespace(id=123, display_name="Tester", name="tester"),
        )
        asyncio.run(SlashCommandRegistry(bot)._prepare(interaction))
        self.assertEqual([call[0] for call in calls], ["ensure_user"])

        calls.clear()
        interaction.response.done = False
        asyncio.run(SlashCommandRegistry(bot)._prepare(interaction, defer=True))
        self.assertEqual([call[0] for call in calls], ["defer", "ensure_user"])

    def test_unregistered_slash_command_is_redirected_to_registration(self):
        import asyncio

        sent = []

        class FakeResponse:
            done = False

            def is_done(self):
                return self.done

            async def defer(self, **_kwargs):
                self.done = True

            async def send_message(self, **kwargs):
                self.done = True
                sent.append(kwargs)

        class FakeFollowup:
            async def send(self, **kwargs):
                sent.append(kwargs)

        async def ensure_user(_user_id, _nickname):
            return False

        bot = SimpleNamespace(
            ensure_user=ensure_user,
            db=SimpleNamespace(users=SimpleNamespace(get_user=lambda _user_id: None)),
        )
        interaction = SimpleNamespace(
            response=FakeResponse(),
            followup=FakeFollowup(),
            user=SimpleNamespace(id=123, display_name="New", name="new"),
            command=SimpleNamespace(name="내자산"),
        )

        with self.assertRaises(RegistrationRequired):
            asyncio.run(SlashCommandRegistry(bot)._prepare(interaction))
        self.assertIn("/등록", sent[0]["content"])
        self.assertTrue(sent[0]["ephemeral"])

    def test_unknown_user_is_not_auto_registered_but_explicit_registration_creates_user(self):
        import asyncio

        bot = object.__new__(NaconomicsBot)
        bot.db = self.db
        asyncio.run(bot.ensure_user("citizen-new", "New Citizen"))
        self.assertIsNone(self.db.users.get_user("citizen-new"))

        created = asyncio.run(bot.register_user("citizen-new", "New Citizen"))
        self.assertTrue(created)
        user = self.db.users.get_user("citizen-new")
        self.assertEqual(user["nickname"], "New Citizen")
        self.assertEqual(user["cash"], 1000000)

    def test_transfer_autocomplete_includes_accounts_and_treasury(self):
        import asyncio

        self.db.users.update_cash("citizen-2", 1000)
        self.db.products.register_saving("RECIPIENT", "Recipient Savings", 0, 0, 0, 3600, 0, "Bank")
        self.assertTrue(self.bank.open_account("citizen-2", "RECIPIENT", "1234", 100)[0])
        recipient_account = self.db.accounts.execute_one(
            "SELECT account_number FROM accounts WHERE user_id = 'citizen-2'"
        )["account_number"]

        class FakeTree:
            def __init__(self):
                self.commands = []

            def add_command(self, command):
                self.commands.append(command)

        bot = SimpleNamespace(tree=FakeTree(), db=self.db, market_mgr=MarketManager(self.db))
        registry = SlashCommandRegistry(bot)
        registry.register()
        interaction = SimpleNamespace(user=SimpleNamespace(id="citizen-1"))
        recipient_choices = asyncio.run(registry.recipient_autocomplete(interaction, ""))

        self.assertIn(recipient_account, {choice.value for choice in recipient_choices})
        treasury_choice = next(choice for choice in recipient_choices if choice.value == TREASURY_USER_ID)
        self.assertIn("국고", treasury_choice.name)

    def test_hardcoded_bootstrap_user_is_always_admin_and_database_role_is_live(self):
        self.assertEqual(BOOTSTRAP_ADMIN_IDS, {"1076861147560542278"})
        self.assertEqual(resolve_user_role("1076861147560542278", {"role": "user"}), "admin")
        self.assertEqual(resolve_user_role("citizen-1", {"role": " ADMIN "}), "admin")
        self.assertEqual(resolve_user_role("citizen-1", {"role": "user"}), "user")
        self.db.users.update_role("citizen-1", "admin")
        self.assertEqual(
            resolve_user_role("citizen-1", self.db.users.get_user("citizen-1")),
            "admin",
        )

        bot = object.__new__(NaconomicsBot)
        bot.db = self.db
        import asyncio
        asyncio.run(bot.ensure_user("1076861147560542278", "Bootstrap Admin"))
        self.assertEqual(self.db.users.get_user("1076861147560542278")["role"], "admin")

        self.db.users.update_role("1076861147560542278", "user")
        asyncio.run(bot.ensure_user("1076861147560542278", "Bootstrap Admin"))
        self.assertEqual(self.db.users.get_user("1076861147560542278")["role"], "admin")

    def test_company_largest_shareholder_is_refreshed_on_trade(self):
        self.db.companies.create_company("CO", "Company", "Industry", 1000, 100, 10, None, "EX")
        self.assertTrue(self.users.buy_asset("citizen-1", "CO", 20)[0])
        company = self.db.companies.get_company("CO")
        self.assertEqual(company["max_shareholder_id"], "citizen-1")
        success, message = self.users.sell_asset("citizen-1", "CO", 20)
        self.assertTrue(success, message)
        self.assertIsNone(self.db.companies.get_company("CO")["max_shareholder_id"])

    def test_matured_investment_cannot_be_bought_but_can_be_sold(self):
        self.db.products.register_investment("FUT", "Future", "futures", 100, 10, 1, "Manager", "EX")
        self.assertTrue(self.users.buy_asset("citizen-1", "FUT", 10)[0])
        self.db.products.execute("UPDATE investment SET created_at='2020-01-01 00:00:00' WHERE product_code='FUT'")
        success, message = self.users.buy_asset("citizen-1", "FUT", 1)
        self.assertFalse(success)
        self.assertIn("거래가 중지", message)
        self.assertTrue(self.users.sell_asset("citizen-1", "FUT", 10)[0])


if __name__ == "__main__":
    unittest.main()
