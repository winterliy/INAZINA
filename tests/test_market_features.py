import asyncio
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from functions.amount_limits import MAX_NATIVE
from database import DatabaseManager
from database.db_manager import DBManager
from database.system_db import SystemDB
from database.user_db import TREASURY_USER_ID
from functions.admin_views import MarketEventModal
from functions.discord_views import PriceMovementView
from functions.price_chart import render_price_chart
from functions.slash_commands import SlashCommandRegistry
from functions.technical_indicators import UPPER_INDICATORS, LOWER_INDICATORS, build_candles, calculate_indicators
from managers.admin_manager import AdminManager
from managers.admin_control_manager import AdminControlManager
from managers.bank_manager import BankManager
from managers.futures_manager import FuturesManager
from managers.interest_manager import InterestManager
from managers.investment_manager import InvestmentManager
from managers.market_manager import MarketManager
from managers.option_manager import OptionManager
from managers.order_book_manager import OrderBookManager
from managers.rate_manager import RateManager
from managers.user_manager import UserManager


class MarketFeatureTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.system.set_setting("auto_target_enabled", "0")
        self.db.users.create_user("admin", "Admin", "Nac", 1000000, role="admin")
        self.db.users.create_user("alice", "Alice", "Nac", 10000)
        self.db.users.create_user("bob", "Bob", "Nac", 10000)
        self.users = UserManager(self.db)
        self.market = MarketManager(self.db)
        self.rates = RateManager(self.db)
        self.products = InvestmentManager(self.db, self.users)
        self.options = OptionManager(self.db, self.users)
        self.futures = FuturesManager(self.db, self.users)
        self.book = OrderBookManager(self.db, self.users)
        self.book.option_manager, self.book.futures_manager = self.options, self.futures
        self.db.companies.create_company("CO", "Company", "Industry", 100000, 1000, 100, "admin", "EX")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def issue(self, code, kind, price=100, supply=100, underlying="CO", days=10, rate=0):
        success, message = self.products.issue("admin", code, code, kind, price, supply, "EX", underlying, days, rate)
        self.assertTrue(success, message)

    def issue_future(self, code="FUT"):
        self.assertTrue(self.rates.set_rate("base_rate", 0, "admin")[0])
        self.issue(code, "stock_future")

    def test_option_chain_and_both_position_readers_attach_accounts_database(self):
        self.db.assets.add_user_asset("alice", "CO", 5, unit_cost=100)
        success, message = self.options.create_and_write("alice", "CO", "call", 100, 1, 2, 10)
        self.assertTrue(success, message)
        chain = self.options.get_active_contracts("CO")
        self.assertEqual(len(chain), 1)
        code = chain[0]["contract_code"]
        self.assertEqual(chain[0]["exchange_id"], "EX")
        self.assertIn(code, {asset["code"] for asset in self.market.get_exchange_assets("EX")})
        self.assertIn(code, {asset["code"] for asset in self.market.get_market_assets(active_only=True)})
        self.assertEqual(chain[0]["price"], self.market.get_asset_info(code)["price"])
        self.assertTrue(self.book.place_limit_order("bob", code, "buy", 2, 10)[0])
        self.assertEqual(self.options.get_user_positions("bob")[0]["quantity"], 2)
        self.assertEqual(self.options.get_user_written("alice")[0]["collateral_quantity"], 2)
        self.assertGreater(self.users.get_total_assets("bob")["stock_total"], 0)

    def test_open_option_order_uses_canonical_market_price_and_reservation_units(self):
        from managers.accounting_journal import AccountingJournal
        from functions.money import base_currency_code

        self.db.assets.add_user_asset("alice", "CO", 5, unit_cost=100)
        self.assertTrue(self.options.create_and_write("alice", "CO", "call", 100, 1, 2, 10)[0])
        code = self.options.get_active_contracts()[0]["contract_code"]
        self.assertTrue(self.options.place_limit_order("bob", code, "buy", 1, 5)[0])
        order = self.db.assets.execute_one(
            "SELECT quote_currency,limit_price_minor_units,limit_price_decimal_places,"
            "reserved_cash_minor_units FROM market_orders WHERE order_kind='option' AND side='buy'"
        )
        with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
            self.assertEqual(order["quote_currency"], base_currency_code(conn))
        self.assertEqual(order["limit_price_minor_units"], 5)
        self.assertEqual(order["limit_price_decimal_places"], 0)
        self.assertEqual(order["reserved_cash_minor_units"], 5)
        with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
            check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                         if item["ledger_scope"] == "market_order_reservations")
        self.assertEqual(check["status"], "matched")
        self.assertEqual(check["order_count"], 2)
        order_id = self.db.assets.execute_one(
            "SELECT order_id FROM market_orders WHERE order_kind='option' AND side='buy'"
        )[0]
        self.assertTrue(self.options.cancel_order("bob", order_id)[0])
        self.assertEqual(self.db.assets.execute_one(
            "SELECT reserved_cash_minor_units FROM market_orders WHERE order_id=?", (order_id,)
        )[0], 0)
        self.assertEqual(self.db.users.get_user("bob")["cash"], 10000)

    def test_covered_call_expiry_delivers_shares_and_never_repeats(self):
        self.db.assets.add_user_asset("alice", "CO", 5, unit_cost=100)
        self.assertTrue(self.options.create_and_write("alice", "CO", "call", 100, 1, 2, 10)[0])
        code = self.options.get_active_contracts()[0]["contract_code"]
        self.assertTrue(self.book.place_limit_order("bob", code, "buy", 2, 10)[0])
        self.db.companies.update_share_price("CO", 120)
        self.db.assets.execute("UPDATE option_contracts SET expiration_epoch = ?", (int(time.time()) - 1,))
        results = self.options.expire_matured()
        self.assertTrue(any(r["user_id"] == "bob" and r["exercised"] == 2 for r in results))
        self.assertEqual(sum(r["quantity"] for r in self.db.assets.get_user_portfolio("bob")), 2)
        self.assertEqual(self.options.expire_matured(), [])

    def test_call_exercise_records_settlement_and_reconciles(self):
        from managers.accounting_journal import AccountingJournal
        self.db.assets.add_user_asset('alice', 'CO', 2, unit_cost=100)
        self.assertTrue(self.options.create_and_write('alice', 'CO', 'call', 100, 1, 1, 10)[0])
        code = self.options.get_active_contracts()[0]['contract_code']
        self.assertTrue(self.options.place_limit_order('bob', code, 'buy', 1, 10)[0])
        self.db.companies.update_share_price('CO', 120)
        self.db.assets.execute('UPDATE option_contracts SET expiration_epoch=? WHERE contract_code=?', (int(time.time())-1, code))
        self.assertTrue(self.options.expire_matured())
        with self.db.readonly() as conn:
            event = conn.execute('SELECT * FROM option_exercise_events').fetchone()
            self.assertEqual((event['contract_code'], event['holder_id'], event['gross_minor_units']), (code, 'bob', 100))
            links = conn.execute("SELECT role,SUM(delta_minor_units) FROM option_exercise_cash_links GROUP BY role ORDER BY role").fetchall()
            self.assertEqual([tuple(row) for row in links], [('payer', -100), ('recipient', 100)])
            moves = conn.execute("SELECT role,quantity FROM option_exercise_asset_movements ORDER BY role").fetchall()
            self.assertEqual([tuple(row) for row in moves], [('collateral', '-1'), ('recipient', '1')])
            self.assertEqual(AccountingJournal.reconcile_option_exercises(conn)['status'], 'matched')
        self.assertEqual(self.options.expire_matured(), [])

    def test_put_exercise_records_settlement_and_reconciles(self):
        from managers.accounting_journal import AccountingJournal
        self.db.assets.add_user_asset('bob', 'CO', 1, unit_cost=90)
        self.assertTrue(self.options.create_and_write('alice', 'CO', 'put', 100, 1, 1, 10)[0])
        code = self.options.get_active_contracts()[0]['contract_code']
        self.assertTrue(self.options.place_limit_order('bob', code, 'buy', 1, 10)[0])
        self.db.companies.update_share_price('CO', 80)
        self.db.assets.execute('UPDATE option_contracts SET expiration_epoch=? WHERE contract_code=?', (int(time.time())-1, code))
        self.assertTrue(self.options.expire_matured())
        with self.db.readonly() as conn:
            row = conn.execute('SELECT option_type,gross_minor_units FROM option_exercise_events').fetchone()
            self.assertEqual(tuple(row), ('put', 100))
            links = conn.execute("SELECT role,SUM(delta_minor_units) FROM option_exercise_cash_links GROUP BY role").fetchall()
            self.assertEqual([tuple(link) for link in links], [('recipient', 100)])
            moves = conn.execute("SELECT role,quantity FROM option_exercise_asset_movements ORDER BY role").fetchall()
            self.assertEqual([tuple(move) for move in moves], [('collateral', '-100'), ('recipient', '1'), ('seller', '-1')])
            self.assertEqual(AccountingJournal.reconcile_option_exercises(conn)['status'], 'matched')

    def test_exercise_reconciliation_detects_removed_cash_link(self):
        from managers.accounting_journal import AccountingJournal
        self.db.assets.add_user_asset('alice', 'CO', 1, unit_cost=100)
        self.assertTrue(self.options.create_and_write('alice', 'CO', 'call', 100, 1, 1, 10)[0])
        code = self.options.get_active_contracts()[0]['contract_code']
        self.assertTrue(self.options.place_limit_order('bob', code, 'buy', 1, 10)[0])
        self.db.companies.update_share_price('CO', 120)
        self.db.assets.execute('UPDATE option_contracts SET expiration_epoch=? WHERE contract_code=?', (int(time.time())-1, code))
        self.assertTrue(self.options.expire_matured())
        with self.db.atomic() as conn:
            conn.execute('DROP TRIGGER trg_option_exercise_cash_links_delete')
            conn.execute("DELETE FROM option_exercise_cash_links WHERE role='recipient'")
            result = AccountingJournal.reconcile_option_exercises(conn)
            self.assertEqual(result['status'], 'mismatch')
            self.assertTrue(any(row['status'] == 'cash_amount_mismatch' for row in result['gaps']))

    def test_exercise_cash_link_lookup_uses_event_index(self):
        with self.db.readonly() as conn:
            plan = conn.execute('EXPLAIN QUERY PLAN SELECT * FROM option_exercise_cash_links WHERE event_key=?', ('example',)).fetchall()
        self.assertTrue(any('INDEX' in row[3].upper() and 'EVENT' in row[3].upper() for row in plan), plan)

    def test_exercise_reconciliation_detects_missing_asset_movement(self):
        from managers.accounting_journal import AccountingJournal
        self.db.assets.add_user_asset('alice', 'CO', 1, unit_cost=100)
        self.assertTrue(self.options.create_and_write('alice', 'CO', 'call', 100, 1, 1, 10)[0])
        code = self.options.get_active_contracts()[0]['contract_code']
        self.assertTrue(self.options.place_limit_order('bob', code, 'buy', 1, 10)[0])
        self.db.companies.update_share_price('CO', 120)
        self.db.assets.execute('UPDATE option_contracts SET expiration_epoch=? WHERE contract_code=?', (int(time.time())-1, code))
        self.assertTrue(self.options.expire_matured())
        with self.db.atomic() as conn:
            conn.execute('DROP TRIGGER trg_option_exercise_asset_movements_delete')
            conn.execute("DELETE FROM option_exercise_asset_movements WHERE role='collateral'")
            result = AccountingJournal.reconcile_option_exercises(conn)
            self.assertEqual(result['status'], 'mismatch')
            self.assertTrue(any(row['status'] == 'missing_asset_movements' for row in result['gaps']))

    def test_exercise_post_failure_rolls_back_the_whole_expiry(self):
        from managers.accounting_journal import AccountingJournal
        self.db.assets.add_user_asset('alice', 'CO', 1, unit_cost=100)
        self.assertTrue(self.options.create_and_write('alice', 'CO', 'call', 100, 1, 1, 10)[0])
        code = self.options.get_active_contracts()[0]['contract_code']
        self.assertTrue(self.options.place_limit_order('bob', code, 'buy', 1, 10)[0])
        self.db.companies.update_share_price('CO', 120)
        self.db.assets.execute('UPDATE option_contracts SET expiration_epoch=? WHERE contract_code=?', (int(time.time())-1, code))
        before = self.db.users.get_user('bob')['cash']
        with patch.object(AccountingJournal, 'post_option_exercise', side_effect=ValueError('posting failure')):
            self.assertEqual(self.options.expire_matured(), [])
        self.assertEqual(self.db.users.get_user('bob')['cash'], before)
        self.assertEqual(self.options.get_contract(code)['status'], 'active')
        self.assertEqual(self.options.get_user_positions('bob')[0]['quantity'], 1)
        self.assertTrue(self.options.expire_matured())

    def test_option_premium_fill_reconciles_recipient_and_common_journal(self):
        from managers.accounting_journal import AccountingJournal

        self.db.assets.add_user_asset("alice", "CO", 5, unit_cost=100)
        self.assertTrue(self.options.create_and_write("alice", "CO", "call", 100, 1, 2, 10)[0])
        code = self.options.get_active_contracts()[0]["contract_code"]
        for price in (12, 10):
            ok, message = self.options.place_limit_order("bob", code, "buy", 1, price)
            self.assertTrue(ok, message)
        with self.db.readonly() as conn:
            rows = conn.execute("""SELECT e.buyer_id,e.seller_id,e.premium_minor_units,
                w.delta_minor_units,j.source_key FROM option_premium_events e
                JOIN customer_wallet_ledger w ON w.entry_id=e.wallet_entry_id
                JOIN accounting_entries j ON j.entry_id=e.journal_entry_id
                ORDER BY e.buy_order_id""").fetchall()
            self.assertEqual([tuple(r)[:4] for r in rows], [("bob", "alice", 10, 10)] * 2)
            self.assertEqual(len({r[4] for r in rows}), 2)
            result = AccountingJournal.reconcile_option_premiums(conn)
            self.assertEqual((result["event_count"], result["status"]), (2, "matched"))
        self.assertEqual(self.db.users.get_user("bob")["cash"], 9980)

    def test_option_premium_posting_failure_rolls_back_cash_and_fill(self):
        from functions.money import base_currency_code
        from managers.accounting_journal import AccountingJournal

        self.db.assets.add_user_asset("alice", "CO", 5, unit_cost=100)
        self.assertTrue(self.options.create_and_write("alice", "CO", "call", 100, 1, 2, 10)[0])
        code = self.options.get_active_contracts()[0]["contract_code"]
        now = int(time.time())
        with self.db.atomic() as conn:
            AccountingJournal.close_period(
                conn, ledger_scope="option", entity_id=code, currency_code=base_currency_code(conn),
                period_start_epoch=now-60, period_end_epoch=now+60,
                actor_id="admin", closed_epoch=now+60,
            )
        ok, message = self.options.place_limit_order("bob", code, "buy", 1, 12)
        self.assertFalse(ok)
        self.assertIn("마감된 회계기간", message)
        self.assertEqual(self.db.users.get_user("alice")["cash"], 10000)
        self.assertEqual(self.db.users.get_user("bob")["cash"], 10000)
        self.assertEqual(self.options.get_user_positions("bob"), [])
        self.assertEqual(self.db.assets.execute_one("SELECT COUNT(*) FROM option_premium_events")[0], 0)
        self.assertEqual(self.db.assets.execute_one("SELECT COUNT(*) FROM option_order_cash_links")[0], 0)

    def test_cash_secured_put_is_available_to_ordinary_user(self):
        self.assertTrue(self.options.create_and_write("alice", "CO", "put", 100, 1, 2, 10)[0])
        written = self.options.get_user_written("alice")[0]
        self.assertEqual(written["collateral_cash"], 200)
        self.assertEqual(self.db.users.get_user("alice")["cash"], 9800)

    def test_option_cash_sources_exclude_other_currencies_and_managed_accounts(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('TST','Test','1',2)")
        self.db.accounts.execute("INSERT INTO accounts(account_number,user_id,quote_currency,managed_by) VALUES ('foreign','alice','TST',NULL)")
        self.db.accounts.execute("INSERT INTO accounts(account_number,user_id,quote_currency,managed_by) VALUES ('managed','alice',base_currency_code(),'manager')")
        self.db.accounts.execute("UPDATE accounts SET balance_units=50000000000 WHERE account_number='foreign'")
        self.db.accounts.execute("UPDATE accounts SET balance_units=30000000000 WHERE account_number='managed'")
        with self.db.atomic() as conn:
            self.assertEqual(self.options._available_cash(conn, 'alice'), 10000)
            self.assertFalse(self.options._debit_available_cash(conn, 'alice', 10001))
        self.assertEqual(self.db.users.get_user('alice')['cash'], 10000)

    def test_foreign_home_cash_cannot_fund_base_currency_put_collateral(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('TST','Test','1',2)")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('Foreign','TST')")
        self.db.users.execute("UPDATE users SET nationality='Foreign' WHERE user_id='alice'")
        with self.db.atomic() as conn:
            self.assertEqual(self.options._available_cash(conn, 'alice'), 0)
        ok, _ = self.options.create_and_write('alice', 'CO', 'put', 100, 1, 1, 10)
        self.assertFalse(ok)
        self.assertEqual(self.db.users.get_user('alice')['cash'], 10000)

    def test_foreign_writer_receives_call_exercise_in_option_currency(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('TST','Test','1',2)")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('Foreign','TST')")
        self.db.users.execute("UPDATE users SET nationality='Foreign' WHERE user_id='alice'")
        self.db.accounts.execute("INSERT INTO accounts(account_number,user_id,quote_currency) VALUES ('base-alice','alice',base_currency_code())")
        self.db.assets.add_user_asset('alice', 'CO', 2, unit_cost=100)
        self.assertTrue(self.options.create_and_write('alice', 'CO', 'call', 100, 1, 1, 10)[0])
        code = self.options.get_active_contracts()[0]['contract_code']
        self.assertTrue(self.options.place_limit_order('bob', code, 'buy', 1, 10)[0])
        self.db.companies.update_share_price('CO', 120)
        self.db.assets.execute("UPDATE option_contracts SET expiration_epoch=? WHERE contract_code=?", (int(time.time())-1, code))
        self.assertTrue(self.options.expire_matured())
        self.assertEqual(self.db.accounts.get_account('base-alice')['balance_minor_units'], 110)
        self.assertEqual(self.db.users.get_user('alice')['cash'], 10000)

    def test_put_collateral_returns_to_base_account_after_nationality_change(self):
        self.assertTrue(self.options.create_and_write('alice', 'CO', 'put', 100, 1, 1, 10)[0])
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('TST','Test','1',2)")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('Foreign','TST')")
        self.db.users.execute("UPDATE users SET nationality='Foreign' WHERE user_id='alice'")
        self.db.accounts.execute("INSERT INTO accounts(account_number,user_id,quote_currency) VALUES ('base-alice','alice',base_currency_code())")
        order = self.db.assets.execute_one("SELECT order_id FROM market_orders WHERE user_id='alice' AND order_kind='option'")
        ok, message = self.options.cancel_order('alice', order['order_id'])
        self.assertTrue(ok, message)
        self.assertEqual(self.db.accounts.get_account('base-alice')['balance_minor_units'], 100)
        self.assertEqual(self.db.users.get_user('alice')['cash'], 9900)

    def test_default_rates_permissions_and_option_rate_sensitivity(self):
        self.assertEqual(self.rates.get_rate("base_rate"), 3)
        self.assertEqual(self.rates.get_rate("legal_max_rate"), 20)
        self.assertFalse(self.rates.set_rate("base_rate", 6, "alice")[0])
        self.assertTrue(self.rates.set_rate("base_rate", -1, "admin")[0])
        for bad in (-101, 101, float("nan"), float("inf"), True):
            self.assertFalse(self.rates.set_rate("base_rate", bad, "admin")[0])
        self.assertGreater(OptionManager.theoretical_price(1000, 1000, 1, .2, "call", .03),
                           OptionManager.theoretical_price(1000, 1000, 1, .2, "call", 0))
        self.assertLess(OptionManager.theoretical_price(1000, 1000, 1, .2, "put", .03),
                        OptionManager.theoretical_price(1000, 1000, 1, .2, "put", 0))

    def test_country_rates_and_zero_legal_maximum(self):
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code,base_rate,legal_max_rate) "
            "VALUES ('자유국','NWX',0,0)"
        )
        self.assertEqual(self.rates.get_rate("base_rate", "자유국"), 0)
        self.assertEqual(self.rates.get_rate("legal_max_rate", "자유국"), 0)
        self.db.system.execute(
            "INSERT INTO market_exchanges(exchange_id,status,updated_epoch,nationality) "
            "VALUES ('FREE','open',0,'자유국')"
        )
        with self.db.readonly() as conn:
            self.assertEqual(RateManager.for_exchange(conn, "base_rate", "FREE"), 0)
            with self.assertRaisesRegex(ValueError, "법정최고이자"):
                RateManager.require_legal_rate(conn, 1, nationality="자유국")
        with self.assertRaisesRegex(ValueError, "국가 ID"):
            self.rates.get_rate("base_rate", "없는국가")

    def test_country_legal_max_rate_change_is_independent(self):
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code,base_rate,legal_max_rate) "
            "VALUES ('별도국','NWX',3,12)"
        )
        self.assertTrue(self.rates.set_country_rate(
            "legal_max_rate", "별도국", 10, "admin"
        )[0])
        self.assertEqual(self.rates.get_rate("legal_max_rate", "별도국"), 10)
        self.assertEqual(self.rates.get_rate("legal_max_rate", "낙원"), 20)

    def test_database_browser_rate_edits_apply_live(self):
        connection = sqlite3.connect(self.db.system.db_path)
        try:
            with connection:
                connection.execute("UPDATE settings SET value = '3.5' WHERE key = 'base_rate'")
                connection.execute("UPDATE settings SET value = '10' WHERE key = 'legal_max_rate'")
        finally:
            connection.close()
        self.assertEqual(self.rates.get_rate("base_rate"), 3.5)
        self.assertEqual(self.rates.get_rate("legal_max_rate"), 10)

    def test_database_browser_rejects_invalid_rates(self):
        connection = sqlite3.connect(self.db.system.db_path)
        try:
            invalid = {
                "base_rate": ("-101", "101", "NaN", "Infinity", "3%", "abc", "", "null", "true", '"3"', "1e999", "[]", "{}"),
                "legal_max_rate": ("-1", "101", "NaN", "Infinity", "3%", "abc", "", "null", "true", '"3"', "1e999", "[]", "{}"),
            }
            for key, values in invalid.items():
                for value in values:
                    with self.subTest(value=value, key=key):
                        with self.assertRaises(sqlite3.IntegrityError), connection:
                            connection.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
        finally:
            connection.close()
        self.assertEqual(self.rates.get_rate("base_rate"), 3)
        self.assertEqual(self.rates.get_rate("legal_max_rate"), 20)

    def test_equivalent_rate_and_other_settings_remain_valid(self):
        connection = sqlite3.connect(self.db.system.db_path)
        try:
            with connection:
                connection.execute("UPDATE settings SET value = '3.000' WHERE key = 'base_rate'")
                connection.execute("UPDATE settings SET value = '0.8' WHERE key = 'gamble_win_rate'")
        finally:
            connection.close()
        self.assertEqual(self.rates.get_rate("base_rate"), 3)
        self.assertEqual(self.db.system.get_setting("gamble_win_rate"), "0.8")

    def test_system_reinitialization_preserves_browser_rates(self):
        connection = sqlite3.connect(self.db.system.db_path)
        try:
            with connection:
                connection.execute("UPDATE settings SET value = '3.5' WHERE key = 'base_rate'")
                connection.execute("UPDATE settings SET value = '8' WHERE key = 'legal_max_rate'")
        finally:
            connection.close()
        restored = SystemDB()
        try:
            self.assertEqual(restored.get_setting("base_rate"), "3.5")
            self.assertEqual(restored.get_setting("legal_max_rate"), "8")
        finally:
            restored.close()

    def test_internal_base_rate_change_applies(self):
        self.assertTrue(self.rates.set_rate("base_rate", 4, "admin", now=12345)[0])
        self.assertEqual(self.rates.get_rate("base_rate"), 4)

    def test_overdue_loan_does_not_accrue_statutory_interest(self):
        now = int(time.time())
        due = now - 365 * 86400
        created = due - 86400
        self.db.products.register_liability("LOAN", "Loan", 0, 0, 1, 86400, 0, "Bank")
        self.db.accounts.execute("INSERT INTO loans (user_id, product_code, principal, outstanding_balance, created_at, last_payment_epoch) "
            "VALUES ('alice', 'LOAN', 100000, 100000, datetime(?, 'unixepoch'), ?)", (created, due))
        manager = InterestManager(self.db)
        manager.process_user_interests("alice", now=now)
        self.assertEqual(self.db.accounts.execute_one("SELECT outstanding_balance FROM loans WHERE user_id = 'alice'")["outstanding_balance"], 100000)
        manager.process_user_interests("alice", now=now)
        self.assertEqual(self.db.accounts.execute_one("SELECT outstanding_balance FROM loans WHERE user_id = 'alice'")["outstanding_balance"], 100000)

    def test_event_permissions_and_strength_bounds_cover_0_to_100(self):
        admin = AdminManager(self.db)
        self.assertFalse(admin.create_market_event("alice", "good", 10)[0])
        for severity in (-1, 101, 3.5, True):
            self.assertFalse(admin.create_market_event("admin", "good", severity)[0])
        self.assertTrue(admin.create_market_event("admin", "good", 0, "CO", now=1000)[0])
        self.assertTrue(admin.create_market_event("admin", "good", 100, "CO", now=1000)[0])

    def event_tick_without_noise(self, now, manager=None, **kwargs):
        """Isolate the endpoint bridge from its separately tested random path."""
        with patch("managers.market_manager.standardized_shock", return_value=0), \
             patch("managers.stochastic_price_engine.standardized_shock", return_value=0), \
             patch.object(MarketManager, "_event_jump_fraction", return_value=0):
            (manager or self.market).update_prices(now=now, **kwargs)

    def test_event_percentage_target_bridge_reaches_exact_endpoint_and_resumes_normal_prices(self):
        self.db.assets.register_asset("SPOT", "Spot", "EX", 100, "commodity_spot")
        self.db.assets.register_asset("FIX", "Fixed", "EX", 1000, "stock")
        self.db.assets.execute("UPDATE market_assets SET is_fixed = 1 WHERE code = 'FIX'")
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "good", 100, "SPOT", 60, now=1000)[0])
        for now, expected in ((1000, 100), (1015, 125), (1030, 150), (1045, 175), (1060, 200)):
            self.event_tick_without_noise(now, volatility=0)
            self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], expected)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 100)
        self.assertEqual(self.db.assets.get_asset_info("FIX")["price"], 1000)
        self.event_tick_without_noise(1061, volatility=0)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 200)
        with patch("managers.market_manager.standardized_shock", return_value=10), \
             patch("managers.stochastic_price_engine.standardized_shock", return_value=10):
            self.market.update_prices(volatility=.1, market_condition="bull", now=1062)
        state = self.db.system.execute_one("SELECT latent_price FROM market_simulation_state WHERE asset_code = 'SPOT'")
        self.assertGreater(state["latent_price"], 200)

    def test_explicit_target_price_bridge_reaches_goal_when_random_noise_is_zero(self):
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "bad", 10, "CO", 60, now=1000, target_price=40)[0])
        observed = []
        for now, expected in ((1000, 100), (1015, 85), (1030, 70), (1045, 55), (1060, 40)):
            self.event_tick_without_noise(now, volatility=.15, market_condition="bull")
            observed.append(self.db.companies.get_company("CO")["share_price"])
            self.assertEqual(observed[-1], expected)
        self.assertEqual(observed, sorted(observed, reverse=True))

    def test_explicit_percentage_is_signed_by_event_type_and_supports_fractional_percent(self):
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event(
            "admin", "good", 10, "CO", 40, now=1000, target_percent=25.5)[0])
        self.event_tick_without_noise(1020, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 113)
        self.event_tick_without_noise(1040, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 126)
        self.assertTrue(admin.create_market_event(
            "admin", "bad", 10, "CO", 40, now=1041, target_percent=50)[0])
        self.event_tick_without_noise(1061, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 95)
        self.event_tick_without_noise(1081, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 63)

    def test_event_progress_survives_new_manager_and_duplicate_ticks_do_not_accumulate(self):
        self.db.assets.register_asset("SPOT", "Spot", "EX", 100, "commodity_spot")
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "good", 100, "SPOT", 60, now=1000)[0])
        self.event_tick_without_noise(1015, volatility=0)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 125)
        restored = MarketManager(self.db)
        self.event_tick_without_noise(1030, manager=restored, volatility=.2)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 150)
        self.event_tick_without_noise(1030, manager=restored, volatility=.2)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 150)
        self.event_tick_without_noise(1060, manager=restored, volatility=0)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 200)

    def test_event_random_path_moves_up_and_down_before_reaching_price_target(self):
        admin = AdminManager(self.db)
        for start, kind, target in ((1000, "bad", 7000), (2000, "good", 13000)):
            with self.subTest(kind=kind):
                self.db.companies.update_share_price("CO", 10000)
                self.assertTrue(admin.create_market_event(
                    "admin", kind, 100, "CO", 120, now=start, target_price=target, target_percent=100)[0])
                observed = [10000]
                for index, now in enumerate(range(start + 5, start + 61, 5)):
                    shock = 1 if index % 2 else -1
                    with patch("managers.market_manager.standardized_shock", return_value=shock), \
                         patch("managers.stochastic_price_engine.standardized_shock", return_value=shock):
                        self.market.update_prices(volatility=0, now=now)
                    observed.append(self.db.companies.get_company("CO")["share_price"])
                changes = [right - left for left, right in zip(observed, observed[1:])]
                self.assertTrue(any(change > 0 for change in changes), observed)
                self.assertTrue(any(change < 0 for change in changes), observed)
                self.assertTrue(all(price >= 1 for price in observed))
                self.market.update_prices(volatility=0, now=start + 120)
                self.assertEqual(self.db.companies.get_company("CO")["share_price"], target)

    def test_event_random_state_survives_new_manager_without_resampling_duplicate_tick(self):
        self.db.companies.update_share_price("CO", 10000)
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event(
            "admin", "bad", 100, "CO", 120, now=1000, target_price=7000, target_percent=100)[0])
        with patch("managers.market_manager.standardized_shock", return_value=1), \
             patch("managers.stochastic_price_engine.standardized_shock", return_value=1):
            self.market.update_prices(volatility=0, now=1030)
        halfway_price = self.db.companies.get_company("CO")["share_price"]
        restored = MarketManager(self.db)
        with patch("managers.market_manager.standardized_shock", return_value=-1) as market_shock, \
             patch("managers.stochastic_price_engine.standardized_shock", return_value=-1) as asset_shock:
            restored.update_prices(volatility=0, now=1030)
            self.assertEqual(self.db.companies.get_company("CO")["share_price"], halfway_price)
            market_shock.assert_not_called()
            asset_shock.assert_not_called()
            restored.update_prices(volatility=0, now=1060)
            self.assertNotEqual(self.db.companies.get_company("CO")["share_price"], halfway_price)
        restored.update_prices(volatility=0, now=1120)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 7000)

    def test_event_price_target_with_zero_percent_has_no_event_noise(self):
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event(
            "admin", "bad", 0, "CO", 60, now=1000, target_price=40, target_percent=0)[0])
        with patch("managers.market_manager.standardized_shock", return_value=1), \
             patch("managers.stochastic_price_engine.standardized_shock", return_value=1):
            self.market.update_prices(volatility=.15, market_condition="bull", now=1030)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 70)
        self.market.update_prices(volatility=.15, market_condition="bull", now=1060)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 40)

    def test_event_asset_code_and_display_name_resolve_without_invalid_asset_error(self):
        admin = AdminManager(self.db)
        for code in (" CO ", "co", " Company "):
            with self.subTest(code=code):
                success, message = admin.create_market_event("admin", "good", 30, code, 60, now=1000)
                self.assertTrue(success, message)
                self.assertEqual(self.db.system.execute_one(
                    "SELECT asset_code FROM market_events ORDER BY event_id DESC LIMIT 1")["asset_code"], "CO")

    def test_event_delayed_tick_applies_target_even_when_end_was_missed(self):
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "bad", 30, "CO", 60, now=1000, target_price=30)[0])
        MarketManager(self.db).update_prices(volatility=.2, market_condition="bull", now=1120)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 30)
        self.market.update_prices(volatility=0, now=1121)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 30)

    def test_event_maximum_discord_integer_target_remains_exact_in_database_and_market_reader(self):
        target = 2 ** 53 - 1
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event(
            "admin", "good", 10, "CO", 60, now=1000, target_price=target)[0])
        self.market.update_prices(volatility=0, now=1060)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], target)
        self.assertEqual(self.market.get_asset_info("CO")["price"], target)

    def test_legacy_event_resumes_remaining_effect_without_immediate_price_jump(self):
        self.db.system.execute("INSERT INTO market_events "
            "(asset_code, event_type, severity, starts_epoch, ends_epoch, created_by) "
            "VALUES ('CO', 'good', 100, 1000, 1060, 'admin')")
        self.event_tick_without_noise(1030, volatility=.2)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 100)
        self.market.update_prices(volatility=.2, now=1060)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 110)

    def test_new_event_replaces_only_its_asset_target_from_current_price(self):
        self.db.assets.register_asset("SPOT", "Spot", "EX", 100, "commodity_spot")
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "good", 100, None, 100, now=1000)[0])
        self.event_tick_without_noise(1020, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 120)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 120)
        self.assertTrue(admin.create_market_event("admin", "bad", 10, "CO", 40, now=1020, target_price=80)[0])
        self.event_tick_without_noise(1040, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 100)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 140)
        self.event_tick_without_noise(1060, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 80)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 160)
        self.event_tick_without_noise(1100, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 80)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 200)

    def test_global_percentage_uses_each_initial_price_and_excludes_fixed_linked_and_expired_assets(self):
        self.db.assets.register_asset("SPOT", "Spot", "EX", 200, "commodity_spot")
        self.db.assets.register_asset("FIX", "Fixed", "EX", 1000, "stock")
        self.db.assets.register_asset("OLD", "Expired", "EX", 500, "stock")
        self.db.assets.execute("UPDATE market_assets SET is_fixed = 1 WHERE code = 'FIX'")
        self.db.assets.execute("UPDATE market_assets SET status = 'expired' WHERE code = 'OLD'")
        self.issue("IF", "index_fund", underlying="EX", days=0)
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "bad", 5, None, 60, now=1000, target_percent=50)[0])
        targets = {row["asset_code"] for row in self.db.system.execute("SELECT asset_code FROM market_event_targets")}
        self.assertEqual(targets, {"CO", "SPOT"})
        self.event_tick_without_noise(1030, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 75)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 150)
        self.assertEqual(self.db.assets.get_asset_info("FIX")["price"], 1000)
        self.assertEqual(self.db.assets.get_asset_info("OLD")["price"], 500)
        self.event_tick_without_noise(1060, volatility=0)
        self.assertEqual(self.db.companies.get_company("CO")["share_price"], 50)
        self.assertEqual(self.db.assets.get_asset_info("SPOT")["price"], 100)

    def test_event_rejects_wrong_direction_invalid_and_unavailable_targets(self):
        admin = AdminManager(self.db)
        invalid = [
            ("good", "CO", {"target_price": 99}),
            ("bad", "CO", {"target_price": 101}),
            ("good", None, {"target_price": 200}),
            ("good", "missing", {"target_price": 200}),
        ]
        invalid.extend(("good", "CO", {"target_price": value})
                       for value in (0, -1, 1.5, True, float("nan"), float("inf"), MAX_NATIVE + 1))
        invalid.extend(("good", "CO", {"target_percent": value})
                       for value in (-1, 101, True, float("nan"), float("inf")))
        for event_type, code, kwargs in invalid:
            with self.subTest(event_type=event_type, code=code, kwargs=kwargs):
                self.assertFalse(admin.create_market_event("admin", event_type, 10, code, 60, now=1000, **kwargs)[0])
        self.assertEqual(self.db.system.execute("SELECT * FROM market_events"), [])
        self.assertEqual(self.db.system.execute("SELECT * FROM market_event_targets"), [])

    def test_explicit_zero_percent_leaves_normal_movement_enabled(self):
        admin = AdminManager(self.db)
        self.assertTrue(admin.create_market_event("admin", "good", 10, "CO", 60, now=1000, target_percent=0)[0])
        price = self.db.companies.get_company("CO")["share_price"]
        with patch("managers.market_manager.standardized_shock", return_value=10), \
             patch("managers.stochastic_price_engine.standardized_shock", return_value=10):
            self.market.update_prices(volatility=.1, market_condition="bull", now=1020)
        self.assertGreater(self.db.companies.get_company("CO")["share_price"], price)

    @staticmethod
    def event_interaction(user_id="admin"):
        return SimpleNamespace(user=SimpleNamespace(id=user_id),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    @staticmethod
    def fill_event_modal(modal, code="CO", duration="60", kind="good", percent="30", target=""):
        if modal.asset_select is not None:
            modal.asset_select._values = [code] if code else []
        else:
            modal.asset_input._value = code or ""
        modal.duration_input._value = duration
        modal.kind_select._values = [kind] if kind else []
        modal.severity_input._value = percent
        modal.target_price_input._value = target
        return modal

    def test_event_modal_contains_all_five_inputs_and_accepts_entire_percentage_range(self):
        create = Mock(return_value=(True, "registered"))
        bot = SimpleNamespace(db=self.db, market_mgr=self.market, admin_mgr=SimpleNamespace(create_market_event=create))
        for value in ("0", "1", "24", "25", "49", "50", "74", "75", "99", "100", "30.5"):
            with self.subTest(value=value):
                modal = self.fill_event_modal(MarketEventModal(bot, "admin"), kind="bad", percent=value)
                components = modal.to_dict()["components"]
                self.assertEqual(len(components), 5)
                self.assertEqual({option.value for option in modal.kind_select.options}, {"good", "bad"})
                self.assertIn("CO", {option.value for option in modal.asset_select.options})
                self.assertFalse(modal.target_price_input.required)
                interaction = self.event_interaction()
                asyncio.run(modal.on_submit(interaction))
                self.assertEqual(create.call_args.args, ("admin", "bad", int(round(float(value))), "CO", 60))
                self.assertEqual(create.call_args.kwargs, {"target_price": None, "target_percent": float(value)})
                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                interaction.followup.send.assert_awaited_once()
                self.assertIn("registered",interaction.followup.send.call_args.kwargs["embed"].description)
                self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
                self.assertTrue(modal.submitted)

    def test_event_modal_rejects_invalid_percent_and_missing_kind_without_creating_event(self):
        create = Mock(return_value=(True, "registered"))
        bot = SimpleNamespace(db=self.db, market_mgr=self.market, admin_mgr=SimpleNamespace(create_market_event=create))
        for value in ("-1", "101", "nan", "inf", "-inf", "abc", "30%", ""):
            with self.subTest(value=value):
                modal = self.fill_event_modal(MarketEventModal(bot, "admin"), percent=value)
                interaction = self.event_interaction()
                asyncio.run(modal.on_submit(interaction))
                interaction.response.send_message.assert_awaited_once()
                interaction.response.defer.assert_not_awaited()
                self.assertFalse(modal.submitted)
        modal = self.fill_event_modal(MarketEventModal(bot, "admin"), kind=None)
        interaction = self.event_interaction()
        asyncio.run(modal.on_submit(interaction))
        interaction.response.send_message.assert_awaited_once()
        create.assert_not_called()

    def test_event_modal_combines_target_price_with_random_percent_and_blocks_duplicate_submission(self):
        create = Mock(return_value=(True, "registered"))
        bot = SimpleNamespace(db=self.db, market_mgr=self.market, admin_mgr=SimpleNamespace(create_market_event=create))
        modal = self.fill_event_modal(MarketEventModal(bot, "admin"), kind="bad", target="40")
        asyncio.run(modal.on_submit(self.event_interaction()))
        self.assertEqual(create.call_args.kwargs, {"target_price": 40, "target_percent": 30.0})
        repeated = self.event_interaction()
        asyncio.run(modal.on_submit(repeated))
        repeated.response.send_message.assert_awaited_once()
        self.assertEqual(create.call_count, 1)

    def test_event_modal_rechecks_owner_and_current_administrator_role(self):
        create = Mock(return_value=(True, "registered"))
        bot = SimpleNamespace(db=self.db, market_mgr=self.market, admin_mgr=SimpleNamespace(create_market_event=create))
        modal = self.fill_event_modal(MarketEventModal(bot, "admin"))
        wrong_owner = self.event_interaction("alice")
        asyncio.run(modal.on_submit(wrong_owner))
        wrong_owner.response.send_message.assert_awaited_once()
        self.db.users.execute("UPDATE users SET role = 'user' WHERE user_id = 'admin'")
        demoted = self.event_interaction()
        asyncio.run(modal.on_submit(demoted))
        demoted.response.send_message.assert_awaited_once()
        create.assert_not_called()

    def test_event_modal_rejects_invalid_duration_asset_and_target_price_before_creation(self):
        create = Mock(return_value=(True, "registered"))
        bot = SimpleNamespace(db=self.db, market_mgr=self.market, admin_mgr=SimpleNamespace(create_market_event=create))
        invalid = [{"duration": value} for value in ("", "0", "-1", "604801", "1.5", "nan", "inf", "abc")]
        invalid.extend({"target": value} for value in ("0", "-1", "1.5", "abc", "nan", "inf", str(MAX_NATIVE + 1)))
        invalid.extend(({"code": None}, {"code": "missing"}, {"code": "__all__", "target": "40"}))
        for values in invalid:
            with self.subTest(values=values):
                modal = self.fill_event_modal(MarketEventModal(bot, "admin"), **values)
                interaction = self.event_interaction()
                asyncio.run(modal.on_submit(interaction))
                interaction.response.send_message.assert_awaited_once()
                interaction.response.defer.assert_not_awaited()
                self.assertFalse(modal.submitted)
        create.assert_not_called()

    def test_event_modal_accepts_global_selection_and_maximum_duration_and_price(self):
        create = Mock(return_value=(True, "registered"))
        bot = SimpleNamespace(db=self.db, market_mgr=self.market, admin_mgr=SimpleNamespace(create_market_event=create))
        modal = self.fill_event_modal(MarketEventModal(bot, "admin"), code="__all__", duration="604800")
        asyncio.run(modal.on_submit(self.event_interaction()))
        self.assertEqual(create.call_args.args, ("admin", "good", 30, None, 604800))
        modal = self.fill_event_modal(MarketEventModal(bot, "admin"), target=str(2 ** 53 - 1))
        asyncio.run(modal.on_submit(self.event_interaction()))
        self.assertEqual(create.call_args.kwargs, {"target_price": 2 ** 53 - 1, "target_percent": 30.0})

    def test_event_modal_asset_selection_contains_only_eligible_assets(self):
        self.db.assets.register_asset("SPOT", "Spot", "EX", 100, "commodity_spot")
        self.db.assets.register_asset("FIX", "Fixed", "EX", 100, "stock")
        self.db.assets.register_asset("OLD", "Expired", "EX", 100, "stock")
        self.db.assets.execute("UPDATE market_assets SET is_fixed = 1 WHERE code = 'FIX'")
        self.db.assets.execute("UPDATE market_assets SET status = 'expired' WHERE code = 'OLD'")
        self.issue("IF", "index_fund", underlying="EX", days=0)
        modal = MarketEventModal(SimpleNamespace(db=self.db, market_mgr=self.market, admin_mgr=AdminManager(self.db)), "admin")
        self.assertEqual({option.value for option in modal.asset_select.options}, {"__all__", "CO", "SPOT", "IF"})

    def test_event_modal_keeps_every_asset_accessible_above_discord_select_limit(self):
        for index in range(25):
            self.db.assets.register_asset(f"S{index}", f"Asset {index}", "EX", 100, "commodity_spot")
        create = Mock(return_value=(True, "registered"))
        bot = SimpleNamespace(db=self.db, market_mgr=self.market, admin_mgr=SimpleNamespace(create_market_event=create))
        for value, expected in ((" S24 ", "S24"), ("asset 24", "S24"), ("Asset 24 (S24)", "S24"), ("시장 전체", None)):
            with self.subTest(value=value):
                modal = self.fill_event_modal(MarketEventModal(bot, "admin"), code=value)
                self.assertIsNone(modal.asset_select)
                self.assertEqual(len(modal.to_dict()["components"]), 5)
                asyncio.run(modal.on_submit(self.event_interaction()))
                self.assertEqual(create.call_args.args[3], expected)

    def test_event_modal_reports_registration_failure_and_allows_another_attempt(self):
        create = Mock(side_effect=[(False, "❌ 자산 상태가 변경되었습니다."), (True, "registered")])
        bot = SimpleNamespace(db=self.db, market_mgr=self.market, admin_mgr=SimpleNamespace(create_market_event=create))
        modal = self.fill_event_modal(MarketEventModal(bot, "admin"))
        interaction = self.event_interaction()
        asyncio.run(modal.on_submit(interaction))
        self.assertFalse(modal.submitted)
        self.assertIn("자산 상태가 변경", interaction.followup.send.call_args.kwargs['embed'].description)
        asyncio.run(modal.on_submit(self.event_interaction()))
        self.assertTrue(modal.submitted)
        self.assertEqual(create.call_count, 2)

    def test_chart_settings_persist_within_discord_component_limits_and_defer_before_render(self):
        bot = SimpleNamespace(market_mgr=self.market)
        view = PriceMovementView(bot, "alice", "CO")
        view.upper_indicators = ["ema", "anchored_vwap"]
        view.lower_indicators = ["macd", "rsi"]
        view._save_preferences()
        restored = PriceMovementView(bot, "alice", "CO")
        self.assertEqual(restored.upper_indicators, view.upper_indicators)
        self.assertEqual(restored.lower_indicators, view.lower_indicators)
        self.assertEqual(len(restored.lower_select.options), 25)
        components = restored.to_components()
        self.assertLessEqual(len(components), 5)
        for row in components:
            for component in row["components"]:
                self.assertLessEqual(len(component.get("options", [])), 25)
        interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
            edit_original_response=AsyncMock())
        asyncio.run(restored._edit_chart(interaction))
        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()

    def test_matured_loan_has_no_statutory_interest(self):
        now = int(time.time())
        due = now - 86400
        self.db.products.register_liability("LOAN", "Loan", 0, 0, 1, 86400, 0, "Bank")
        self.db.accounts.execute("INSERT INTO loans (user_id, product_code, principal, outstanding_balance, created_at, last_payment_epoch) "
            "VALUES ('alice', 'LOAN', 1000, 1000, datetime(?, 'unixepoch'), ?)", (due - 86400, due))
        manager = InterestManager(self.db)
        manager.process_user_interests("alice", now=now)
        row = self.db.accounts.execute_one("SELECT * FROM loans WHERE user_id = 'alice'")
        self.assertEqual(row["outstanding_balance"], 1000)
        self.db.accounts.execute("UPDATE loans SET outstanding_balance = 500 WHERE user_id = 'alice'")
        manager.process_user_interests("alice", now=now + 86400)
        row = self.db.accounts.execute_one("SELECT * FROM loans WHERE user_id = 'alice'")
        self.assertEqual(row["outstanding_balance"], 500)

    def test_new_product_validation_and_duplicate_code_rejection(self):
        self.assertFalse(self.products.issue("alice", "NEW", "New", "commodity_spot", 1, 1, "EX")[0])
        self.assertFalse(self.products.issue("admin", "CO", "Dup", "commodity_spot", 1, 1, "EX")[0])
        self.assertFalse(self.products.issue("admin", "F", "Future", "stock_future", 100, 10, "EX", "missing", 1)[0])
        self.assertFalse(self.products.issue("admin", "B", "Bond", "corporate_bond", 100, 10, "EX", "CO", 0)[0])
        self.assertFalse(self.products.issue("admin", "I", "Index", "index_fund", 100, 10, "EX", "unknown", 0)[0])
        self.assertFalse(self.products.issue("admin", "X", "X", "mutual_fund", 100, 10, "EX", "EX", 1)[0])

    def test_spot_commodity_is_listed_and_trades_on_exchange(self):
        self.issue("GOLD", "commodity_spot", days=0, underlying=None)
        asset = self.market.get_asset_info("GOLD")
        self.assertEqual(asset["type"], "commodity_spot")
        self.assertTrue(self.book.place_limit_order("alice", "GOLD", "buy", 5, 100)[0])
        self.assertTrue(self.book.place_limit_order("alice", "GOLD", "sell", 2, 120)[0])
        self.assertTrue(self.book.place_limit_order("bob", "GOLD", "buy", 2, 120)[0])
        self.assertEqual(sum(r["quantity"] for r in self.db.assets.get_user_portfolio("bob")), 2)

    def test_funds_track_nav_and_use_subscription_redemption_with_conserved_cash(self):
        self.issue("MF", "mutual_fund", underlying="EX", days=0)
        self.issue("IF", "index_fund", underlying="EX", days=0)
        # Reconstruct pre-upgrade synthetic funds; new funds have separate cash.
        self.db.products.execute("DELETE FROM fund_accounts WHERE product_code IN ('MF','IF')")
        now = int(time.time())
        self.market.update_prices(volatility=0, now=now)
        before = self.db.users.get_user("alice")["cash"] + self.db.users.get_treasury_balance()
        self.assertTrue(self.products.trade_fund("alice", "MF", 5)[0])
        self.assertFalse(self.book.place_limit_order("alice", "MF", "buy", 1, 100)[0])
        self.db.companies.update_share_price("CO", 120)
        self.market.update_prices(volatility=0, now=now + 1)
        self.assertEqual(self.db.products.get_investment("MF")["unit_price"], 120)
        self.assertEqual(self.db.products.get_investment("IF")["unit_price"], 120)
        self.assertTrue(self.products.trade_fund("alice", "MF", 5, redeem=True)[0])
        self.assertEqual(self.db.users.get_user("alice")["cash"] + self.db.users.get_treasury_balance(), before)
        self.assertEqual(self.db.assets.get_user_portfolio("alice"), [])

    def test_all_four_fund_categories_accept_cross_asset_components_and_price_nav(self):
        self.db.assets.register_asset("GOLD", "Gold", "EX", 100, "commodity_spot", 1000)
        self.db.assets.register_asset("REIT", "Real estate", "EX", 100, "real_estate", 1000)
        self.issue("CB", "corporate_bond", price=100, underlying="CO", days=30)
        control = AdminControlManager(SimpleNamespace(db=self.db))

        cases = {
            "EQ": ("equity", {"CO": .60, "GOLD": .40}),
            "BD": ("bond", {"CB": .60, "GOLD": .40}),
            "MX": ("mixed", {"CO": .50, "CB": .50}),
            "OT": ("other", {"GOLD": .50, "REIT": .50}),
        }
        for code, (category, components) in cases.items():
            with self.subTest(category=category):
                control.entity(
                    "admin", "fund", "create", code, name=code, price="100", supply="1000",
                    extra={"exchange_id": "EX", "fund_category": category, "components": components},
                )
                product = self.db.products.get_investment(code)
                self.assertEqual(product["fund_category"], category)

        # Legacy weighted-price NAV remains supported alongside real holdings.
        self.db.products.execute("DELETE FROM fund_accounts WHERE product_code IN ('EQ','BD','MX','OT')")

        component_rows = self.db.products.execute(
            "SELECT asset_code,asset_source,asset_class FROM fund_components WHERE product_code='MX' ORDER BY asset_code"
        )
        self.assertEqual(
            [(row["asset_code"], row["asset_source"], row["asset_class"]) for row in component_rows],
            [("CB", "investment", "bond"), ("CO", "company", "equity")],
        )
        with self.db.atomic(("assets", "product", "company")) as conn:
            product = conn.execute("SELECT * FROM investment WHERE product_code='MX'").fetchone()
            components = {"MX": conn.execute(
                "SELECT * FROM fund_components WHERE product_code='MX'"
            ).fetchall()}
            price = InvestmentManager.linked_price(
                conn, product, 0, int(time.time()),
                prices={"CO": {"price": 120}, "CB": {"price": 110}},
                fund_components=components,
            )
        self.assertEqual(price, 115)

        with self.assertRaisesRegex(ValueError, "주식형 조건"):
            control.entity(
                "admin", "fund", "create", "BAD", name="BAD", price="100", supply="1000",
                extra={"exchange_id": "EX", "fund_category": "equity",
                       "components": {"CO": .5999, "GOLD": .4001}},
            )

    def test_fund_component_edit_reanchors_current_nav(self):
        self.db.assets.register_asset("GOLD", "Gold", "EX", 100, "commodity_spot", 1000)
        control = AdminControlManager(SimpleNamespace(db=self.db))
        control.entity(
            "admin", "fund", "create", "FD", name="Fund", price="100", supply="1000",
            extra={"exchange_id": "EX", "fund_category": "equity", "components": {"CO": 1}},
        )
        self.db.products.execute("DELETE FROM fund_accounts WHERE product_code='FD'")
        self.db.products.execute("UPDATE investment SET unit_price=125 WHERE product_code='FD'")
        control.entity(
            "admin", "fund", "edit", "FD",
            extra={"fund_category": "equity", "components": {"CO": .6, "GOLD": .4}},
        )
        product = self.db.products.get_investment("FD")
        self.assertEqual(product["nav_anchor"], 125)
        with self.db.atomic(("assets", "product", "company")) as conn:
            linked = InvestmentManager.linked_price(
                conn, conn.execute("SELECT * FROM investment WHERE product_code='FD'").fetchone(),
                0, int(time.time()),
            )
        self.assertEqual(linked, 125)

    def test_bond_fund_uses_same_tick_corporate_bond_quote(self):
        self.issue("CB", "corporate_bond", price=100, underlying="CO", days=365, rate=20)
        control = AdminControlManager(SimpleNamespace(db=self.db))
        control.entity(
            "admin", "fund", "create", "BF", name="Bond fund", price="100", supply="1000",
            extra={"exchange_id": "EX", "fund_category": "bond", "components": {"CB": 1}},
        )
        self.db.products.execute("DELETE FROM fund_accounts WHERE product_code='BF'")
        self.market._update_linked_products(int(time.time()))
        bond_price = self.db.products.get_investment("CB")["unit_price"]
        fund_price = self.db.products.get_investment("BF")["unit_price"]
        self.assertNotEqual(bond_price, 100)
        self.assertEqual(fund_price, bond_price)

    def test_corporate_bond_primary_proceeds_go_to_company_and_maturity_pays_once(self):
        self.assertTrue(self.rates.set_country_rate("legal_max_rate", "낙원", 40, "admin")[0])
        from managers.accounting_journal import AccountingJournal
        from managers.financial_manager import FinancialManager
        with self.db.atomic() as conn:
            FinancialManager.ensure(conn, "CO")
        self.issue("CB", "corporate_bond", rate=36.5)
        start = self.db.companies.get_company("CO")["capital"]
        self.assertTrue(self.book.place_limit_order("alice", "CB", "buy", 10, 100)[0])
        self.assertEqual(self.db.companies.get_company("CO")["capital"], start + 1000)
        with self.db.readonly() as conn:
            issue_lines = conn.execute("""SELECT l.account,l.amount FROM financial_lines l
                JOIN financial_entries e USING(entry_id)
                WHERE e.company_code='CO' AND e.source_key LIKE 'bond-primary-issue:%'""").fetchall()
            self.assertEqual({row["account"]: int(row["amount"]) for row in issue_lines},
                             {"cash": 1000, "bonds": -1000})
            bond_check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                              if item["ledger_scope"] == "company_bond_liabilities")
        self.assertEqual(bond_check["status"], "matched", bond_check)
        self.assertEqual(bond_check["gaps"], [], bond_check)
        expiry = self.db.products.get_investment("CB")["maturity_epoch"]
        result = self.products.redeem_corporate_bonds(now=expiry)
        self.assertEqual(result[0]["net"], 1010)
        self.assertEqual(self.db.users.get_user("alice")["cash"], 10010)
        self.assertEqual(self.db.companies.get_company("CO")["capital"], start - 10)
        with self.db.readonly() as conn:
            bond_check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                              if item["ledger_scope"] == "company_bond_liabilities")
            redemption = conn.execute("""SELECT l.account,l.amount FROM financial_lines l
                JOIN financial_entries e USING(entry_id)
                WHERE e.company_code='CO' AND e.source_key='bond-redemption:CB:' || ?""",
                (str(expiry),)).fetchall()
        self.assertEqual(bond_check["status"], "matched", bond_check)
        self.assertEqual({row["account"]: int(row["amount"]) for row in redemption},
                         {"bonds": 1000, "interest_expense": 10, "cash": -1010})
        self.assertEqual(self.products.redeem_corporate_bonds(now=expiry + 1), [])

    def test_company_bond_primary_issue_opens_books_and_reconciles(self):
        from managers.accounting_journal import AccountingJournal
        from managers.financial_manager import FinancialManager
        self.issue("CB-OPEN", "corporate_bond", rate=5)
        self.assertTrue(self.book.place_limit_order("alice", "CB-OPEN", "buy", 3, 100)[0])
        with self.db.atomic() as conn:
            self.assertIsNotNone(FinancialManager.entity(conn, "CO"))
            bond_check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                              if item["ledger_scope"] == "company_bond_liabilities")
        self.assertEqual(bond_check["status"], "matched", bond_check)

    def test_corporate_bond_coupon_posts_once_to_company_books(self):
        from managers.accounting_journal import AccountingJournal
        from managers.financial_manager import FinancialManager
        with self.db.atomic() as conn:
            FinancialManager.ensure(conn, "CO")
        self.issue("CB-COUPON", "corporate_bond", rate=36.5)
        self.assertTrue(self.book.place_limit_order("alice", "CB-COUPON", "buy", 10, 100)[0])
        now = int(time.time())
        paid_until = now
        with self.db.atomic() as conn:
            conn.execute("UPDATE investment SET coupon_frequency_days=5,last_coupon_epoch=?,"
                         "next_coupon_epoch=? WHERE product_code='CB-COUPON'",
                         (now - 5 * 86400, now))
        first = self.products.pay_corporate_bond_coupons(now=now)
        second = self.products.pay_corporate_bond_coupons(now=now)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        with self.db.readonly() as conn:
            rows = conn.execute("""SELECT l.account,l.amount FROM financial_lines l
                JOIN financial_entries e USING(entry_id)
                WHERE e.company_code='CO' AND e.source_key=?""",
                (f"bond-coupon:CB-COUPON:{paid_until}",)).fetchall()
            bond_check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                              if item["ledger_scope"] == "company_bond_liabilities")
        coupon = int(first[0]["coupon"])
        self.assertEqual({row["account"]: int(row["amount"]) for row in rows},
                         {"interest_expense": coupon, "cash": -coupon})
        self.assertEqual(bond_check["status"], "matched", bond_check)

    def test_corporate_bond_default_preserves_claim_and_retries_after_capital_injection(self):
        self.assertTrue(self.rates.set_country_rate("legal_max_rate", "낙원", 40, "admin")[0])
        self.db.companies.execute("UPDATE company SET capital = 0 WHERE company_code = 'CO'")
        self.issue("CB", "corporate_bond", rate=36.5)
        self.assertTrue(self.book.place_limit_order("alice", "CB", "buy", 10, 100)[0])
        self.assertTrue(self.book.place_limit_order("bob", "CB", "buy", 1, 50)[0])
        expiry = self.db.products.get_investment("CB")["maturity_epoch"]
        self.assertEqual(self.products.redeem_corporate_bonds(now=expiry), [])
        self.assertEqual(self.db.products.get_investment("CB")["status"], "defaulted")
        self.assertEqual(self.market.get_asset_info("CB")["status"], "defaulted")
        self.assertEqual(self.db.users.get_user("bob")["cash"], 10000)
        self.assertEqual(len(self.db.assets.get_user_portfolio("alice")), 1)
        self.db.products.execute("UPDATE investment SET created_at = '2020-01-01 00:00:00' WHERE product_code = 'CB'")
        self.assertFalse(self.book.place_limit_order("alice", "CB", "sell", 1, 100)[0])
        self.db.companies.execute("UPDATE company SET capital = capital + 10 WHERE company_code = 'CO'")
        self.assertEqual(len(self.products.redeem_corporate_bonds(now=expiry + 1)), 1)
        self.assertEqual(self.db.products.get_investment("CB")["status"], "redeemed")

    def test_future_long_profit_and_close_tax_without_counting_notional_as_assets(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 2, 100)[0])
        self.assertEqual(self.db.users.get_user("alice")["cash"], 9960)
        self.assertEqual(self.users.get_total_assets("alice")["net_worth"], 10000)
        self.db.companies.update_share_price("CO", 110)
        self.futures.settle_positions()
        position = self.db.assets.execute_one("SELECT * FROM futures_positions WHERE user_id = 'alice'")
        self.assertEqual(position["collateral"], 60)
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "sell", 2, 110)[0])
        self.assertIsNone(self.db.assets.execute_one("SELECT * FROM futures_positions WHERE user_id = 'alice'"))
        self.assertEqual(self.db.users.get_user("alice")["cash"], 10019)

    def test_future_short_profit_and_margin_addition(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "sell", 1, 100)[0])
        self.assertEqual(self.db.assets.execute_one("SELECT quantity FROM futures_positions")["quantity"], -1)
        self.assertTrue(self.futures.add_margin("alice", "FUT", 20)[0])
        self.db.companies.update_share_price("CO", 80)
        self.futures.settle_positions()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 80)[0])
        self.assertEqual(self.db.users.get_user("alice")["cash"], 10019)

    def test_future_margin_rounds_decimal_rate_without_float_overcharge(self):
        product = {"contract_size": 100, "initial_margin": 0.07}
        self.assertEqual(FuturesManager._margin(product, 1, 1), 7)

    def test_future_realized_pnl_allocation_avoids_float_precision_loss(self):
        amount = 9_007_199_254_740_993
        self.assertEqual(FuturesManager._allocate_proportional_amount(amount, 3, 4),
                         6_755_399_441_055_744)
        self.assertEqual(FuturesManager._allocate_proportional_amount(-amount, 3, 4),
                         -6_755_399_441_055_744)

    def test_corporate_bond_coupon_rounds_in_currency_minor_units(self):
        year_seconds = 365 * 86400
        self.assertEqual(InvestmentManager._coupon_minor_units(
            100, "0.5", year_seconds, "ACT/365", 0
        ), 1)
        self.assertEqual(InvestmentManager._coupon_minor_units(
            10_000, "0.5", year_seconds, "ACT/365", 2
        ), 50)

    def test_future_limit_orders_match_citizens_at_maker_price(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 2, 90)[0])
        self.assertTrue(self.book.place_limit_order("bob", "FUT", "sell", 2, 90)[0])
        trade = self.db.assets.execute_one("SELECT * FROM market_trade_history WHERE asset_code = 'FUT'")
        self.assertEqual((trade["price"], trade["quantity"]), (90, 2))
        self.assertEqual(len(self.db.assets.execute("SELECT * FROM futures_positions")), 2)

    def test_future_pending_order_refunds_and_reservation_is_in_net_worth(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "sell", 1, 200)[0])
        order = self.book.get_user_open_orders("alice")[0]
        self.assertEqual(self.users.get_total_assets("alice")["order_reserved_total"], 40)
        self.assertEqual(self.users.get_total_assets("alice")["net_worth"], 10000)
        self.assertTrue(self.book.cancel_order("alice", order["order_id"])[0])
        self.assertEqual(self.db.users.get_user("alice")["cash"], 10000)

    def test_future_pending_order_fills_when_fair_quote_crosses_limit(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 90)[0])
        self.db.companies.update_share_price("CO", 80)
        self.futures.settle_positions()
        self.assertEqual(self.db.assets.execute_one("SELECT settlement_price FROM futures_positions")["settlement_price"], 80)
        self.assertEqual(self.book.get_user_open_orders("alice"), [])

    def test_future_liquidation_caps_loss_at_margin_and_cancels_resting_risk(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 100)[0])
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 50)[0])
        self.db.companies.update_share_price("CO", 70)
        results = self.futures.settle_positions()
        self.assertEqual(results[0]["reason"], "futures_liquidation")
        self.assertEqual(self.db.users.get_user("alice")["cash"], 9980)
        self.assertEqual(self.book.get_user_open_orders("alice"), [])
        self.assertEqual(self.futures.settle_positions(), [])

    def test_future_maturity_uses_last_underlying_sample_at_expiry_and_refunds_orders(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 100)[0])
        self.assertTrue(self.book.place_limit_order("bob", "FUT", "sell", 1, 200)[0])
        now = int(time.time())
        self.db.products.execute("UPDATE investment SET maturity_epoch = ? WHERE product_code = 'FUT'", (now - 1,))
        self.db.assets.execute("INSERT INTO market_price_history (asset_code, price, recorded_epoch) VALUES ('CO', 120, ?)", (now - 2,))
        self.db.companies.update_share_price("CO", 500)
        results = self.futures.settle_positions(now=now)
        self.assertEqual(results[0]["reason"], "futures_maturity")
        self.assertEqual(self.db.users.get_user("alice")["cash"], 10019)
        self.assertEqual(self.db.users.get_user("bob")["cash"], 10000)
        self.assertEqual(self.db.products.get_investment("FUT")["status"], "expired")
        self.assertEqual(self.futures.settle_positions(now=now + 10), [])

    def test_bank_account_cannot_close_with_reserved_short_order_margin(self):
        self.issue_future()
        self.db.products.register_saving("SAVE", "Saving", 0, 0, 0, 1, 0, "Bank")
        bank = BankManager(self.db)
        success, message = bank.open_account("alice", "SAVE", "1234", 9000)
        self.assertTrue(success, message)
        account = self.db.accounts.get_user_accounts("alice")[0]["account_number"]
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "sell", 1, 200)[0])
        self.assertFalse(bank.close_account("alice", account, "1234")[0])

class IndicatorTests(unittest.TestCase):
    @staticmethod
    def candles(count=160, volume=10):
        return [dict(time=172800 + i * 60, open=100 + i * .1, high=102 + i * .1 + i % 3,
                     low=98 + i * .1, close=100 + i * .1, volume=volume) for i in range(count)]

    def test_all_requested_indicators_have_finite_series_and_warmup(self):
        candles = self.candles()
        for i, candle in enumerate(candles):
            candle["close"] += math.sin(i / 3)
        upper, lower = calculate_indicators(candles, UPPER_INDICATORS, LOWER_INDICATORS, candles[0]["time"])
        self.assertEqual(set(upper), set(UPPER_INDICATORS))
        self.assertEqual(set(lower), set(LOWER_INDICATORS))
        self.assertEqual(len(UPPER_INDICATORS), 13)
        self.assertEqual(len(LOWER_INDICATORS), 24)
        for key, group in {**upper, **lower}.items():
            self.assertTrue(group, key)
            self.assertTrue(any(value is not None for series in group for value in series["values"]), key)
            for series in group:
                self.assertTrue(all(value is None or math.isfinite(value) for value in series["values"]), key)
        self.assertIsNone(lower["rsi"][0]["values"][13])
        _, monotonic = calculate_indicators(self.candles(), (), ("rsi",))
        self.assertEqual(monotonic["rsi"][0]["values"][14], 100)

    def test_candle_ohlc_and_volume_aggregation(self):
        candles = build_candles([(0, 10), (1, 12), (2, 8), (59, 11), (60, 20)], [(1, 5), (30, 7), (60, 9)], 60)
        self.assertEqual(candles[0], dict(time=0, open=10, high=12, low=8, close=11, volume=12))
        self.assertEqual(candles[1]["volume"], 9)

    def test_candle_builder_ignores_invalid_epoch_points_and_volumes(self):
        candles = build_candles(
            [(0, 10), (-1, 50), (253402300800, 60), (float('nan'), 70), (60, 20)],
            [(0, 2), (-1, 100), (253402300800, 100), (60, float('inf'))], 60,
        )
        self.assertEqual(candles, [
            dict(time=0, open=10, high=10, low=10, close=10, volume=2),
            dict(time=60, open=20, high=20, low=20, close=20, volume=0),
        ])

    def test_indicator_chart_filters_bad_points_and_rejects_bad_viewport_epoch(self):
        from functions.indicator_chart import render_chart
        image = render_chart(
            [(-1, 100), (253402300800, 200), (0, 10)], 60,
            upper_indicators=(), lower_indicators=(), end_epoch=60,
        )
        self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
        with self.assertRaises(ValueError):
            render_chart([], 60, end_epoch=1e300)

    def test_flat_prices_zero_volume_and_short_history_do_not_fabricate_vwap(self):
        for count in (1, 2, 5, 20, 160):
            candles = [dict(time=i * 60, open=100, high=100, low=100, close=100, volume=0) for i in range(count)]
            upper, lower = calculate_indicators(candles, UPPER_INDICATORS, LOWER_INDICATORS, 0)
            self.assertTrue(all(x is None for x in upper["vwap"][0]["values"]))
            self.assertTrue(all(x is None for x in upper["anchored_vwap"][0]["values"]))
            self.assertTrue(all(x is None for x in lower["mfi"][0]["values"]))
            if count > 14:
                self.assertEqual(lower["rsi"][0]["values"][-1], 50)

    def test_vwap_resets_at_kst_midnight_and_anchor_excludes_prior_bars(self):
        candles = [dict(time=14 * 3600, open=100, high=100, low=100, close=100, volume=10),
                   dict(time=15 * 3600, open=200, high=200, low=200, close=200, volume=30)]
        upper, _ = calculate_indicators(candles, ("vwap", "anchored_vwap"), (), candles[1]["time"])
        self.assertEqual(upper["vwap"][0]["values"], [100, 200])
        self.assertEqual(upper["anchored_vwap"][0]["values"], [None, 200])

    def test_bollinger_and_obv_match_small_known_calculations(self):
        candles = [dict(time=i * 60, open=i + 1, high=i + 1, low=i + 1, close=i + 1, volume=10) for i in range(20)]
        upper, lower = calculate_indicators(candles, ("bollinger",), ("obv", "roc"))
        self.assertEqual(upper["bollinger"][0]["values"][-1], 10.5)
        self.assertAlmostEqual(upper["bollinger"][1]["values"][-1], 10.5 + 2 * math.sqrt(33.25))
        self.assertEqual(lower["obv"][0]["values"][-1], 190)
        self.assertAlmostEqual(lower["roc"][0]["values"][14], 1400)

    def test_ichimoku_projects_spans_and_fractals_wait_for_two_confirming_bars(self):
        candles = self.candles(80)
        upper, _ = calculate_indicators(candles, ("ichimoku", "fractals"), ())
        self.assertEqual(len(upper["ichimoku"][2]["values"]), 106)
        self.assertTrue(all(x is None for x in upper["ichimoku"][2]["values"][:51]))
        self.assertTrue(all(x is None for x in upper["fractals"][0]["values"][-2:]))

    def test_all_indicator_panels_render_to_png_and_can_all_be_hidden(self):
        candles = self.candles()
        points = [(c["time"], c["close"]) for c in candles]
        volumes = [(c["time"], c["volume"]) for c in candles]
        png = render_price_chart(points, 3600, volumes=volumes, upper_indicators=UPPER_INDICATORS,
                                 lower_indicators=LOWER_INDICATORS, end_epoch=candles[-1]["time"])
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        hidden = render_price_chart(points, 3600, volumes=volumes, upper_indicators=(), lower_indicators=(), end_epoch=candles[-1]["time"])
        self.assertNotEqual(png, hidden)


if __name__ == "__main__":
    unittest.main()
