import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from database.els_db import ELSDB
from database.corporate_db import CorporateDB
from database.user_db import TREASURY_USER_ID
from managers.els_manager import ELSManager, DAY, MAX_MONEY
from managers.market_session_manager import MarketSessionManager


class ELSTests(unittest.TestCase):
    def setUp(self):
        self.previous_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.elsdb = ELSDB()
        self.corporate = CorporateDB()
        self.db.users.create_user('admin', '관리자', '낙원', 1000000, 'admin')
        self.db.users.create_user('citizen', '시민', '낙원', 1000000)
        self.db.users.update_cash(TREASURY_USER_ID, 10000000)
        self.db.companies.create_company('CO', '회사', '게임', 100000, 1000, 100, 'admin', 'INZ')
        self.db.assets.execute("INSERT INTO market_exchange_indices(exchange_id,index_value,updated_epoch) VALUES ('INZ',1000,0)")
        self.manager = ELSManager(self.db)
        self.fields = dict(name='테스트 ELS', face_value=10000, total_supply=20, underlyings='CO',
            annual_coupon=12, maturity_days=180, observation_days='30,60,90', redemption_levels='90,85,80',
            maturity_level='75%', knock_in_level='60%', subscription_days=1, exchange_id='INZ')
        with patch('managers.els_manager.time.time', return_value=1000):
            self.manager.create('admin', 'ELS1', self.fields)
        self.issue_epoch = 1000 + DAY

    def tearDown(self):
        self.elsdb.close()
        self.corporate.close()
        self.db.close()
        DBManager.database_dir = self.previous_root
        self.tmp.cleanup()

    def tick(self, now, price=100):
        with self.db.atomic() as conn:
            conn.execute("UPDATE company SET share_price=? WHERE company_code='CO'", (price,))
            return self.manager.observe(conn, now)

    def subscribe_and_issue(self, quantity=2):
        self.assertTrue(self.manager.subscribe('citizen', 'ELS1', quantity, now=1001)[0])
        self.tick(self.issue_epoch)

    def total_money(self):
        return (self.db.users.execute_one('SELECT SUM(cash) FROM users')[0]
            + self.db.products.execute_one('SELECT SUM(locked_amount) FROM els_reserves')[0]
            + self.db.products.execute_one('SELECT COALESCE(SUM(balance),0) FROM issuer_cash_accounts')[0])

    def test_early_redemption_threshold_tax_conservation_and_idempotency(self):
        total = self.total_money()
        self.subscribe_and_issue()
        self.assertEqual(self.total_money(), total)
        details = self.manager.get_details('ELS1', 'citizen')
        self.assertEqual(details['quantity'], 2)
        self.assertEqual(details['underlyings'][0]['initial_price'], 100)
        self.tick(self.issue_epoch + 30 * DAY, 90)
        notifications = self.manager.process_due(self.issue_epoch + 30 * DAY)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]['gross'], 20197)
        self.assertEqual(notifications[0]['tax'], 6)
        self.assertEqual(self.total_money(), total)
        self.assertEqual(self.manager.get_details('ELS1')['state'], 'redeemed')
        self.assertEqual(self.manager.get_user_positions('citizen'), [])
        self.assertEqual(self.manager.process_due(self.issue_epoch + 31 * DAY), [])
        self.assertEqual(self.db.products.execute_one('SELECT COUNT(*) FROM els_settlements')[0], 1)

    def test_knock_in_persists_through_restart_and_loses_only_principal(self):
        total = self.total_money()
        self.subscribe_and_issue()
        self.tick(self.issue_epoch + DAY, 60)
        self.manager = ELSManager(self.db)
        self.tick(self.issue_epoch + 180 * DAY, 70)
        self.assertTrue(self.manager.get_details('ELS1')['knocked_in'])
        payment = self.manager.process_due(self.issue_epoch + 180 * DAY)[0]
        self.assertEqual((payment['gross'], payment['tax']), (14000, 0))
        self.assertEqual(self.db.users.get_user('citizen')['cash'], 994000)
        self.assertEqual(self.total_money(), total)
        rounds = self.manager.get_details('ELS1')['observations']
        self.assertEqual([r['outcome'] for r in rounds], ['skipped', 'skipped', 'skipped', 'loss'])

    def test_no_knock_in_and_recovered_knock_in_both_pay_maturity_coupon(self):
        for hit in (False, True):
            with self.subTest(knock_in=hit):
                if hit:
                    self.tearDown()
                    self.setUp()
                self.subscribe_and_issue()
                if hit:
                    self.tick(self.issue_epoch + DAY, 55)
                self.tick(self.issue_epoch + 180 * DAY, 80 if hit else 72)
                payment = self.manager.process_due(self.issue_epoch + 180 * DAY)[0]
                self.assertEqual(payment['gross'], 21184)
                self.assertEqual(self.manager.get_details('ELS1')['state'], 'redeemed')

    def test_cancel_refunds_principal_and_all_coupon_reserves(self):
        before_user = self.db.users.get_user('citizen')['cash']
        before_treasury = self.db.users.get_treasury_balance()
        self.assertTrue(self.manager.subscribe('citizen', 'ELS1', 2, now=1001)[0])
        self.assertTrue(self.manager.cancel('admin', 'ELS1', now=1002)[0])
        self.assertEqual(self.db.users.get_user('citizen')['cash'], before_user)
        self.assertEqual(self.db.users.get_treasury_balance(), before_treasury)
        self.assertEqual(self.manager.get_details('ELS1')['state'], 'cancelled')
        self.assertFalse(self.manager.cancel('admin', 'ELS1')[0])

    def test_coupon_insufficiency_and_unauthorized_actions_roll_back(self):
        self.db.users.execute("UPDATE users SET cash=0 WHERE user_id=?", (TREASURY_USER_ID,))
        self.assertFalse(self.manager.subscribe('citizen', 'ELS1', 2, now=1001)[0])
        self.assertEqual(self.db.users.get_user('citizen')['cash'], 1000000)
        self.assertEqual(self.db.products.execute_one('SELECT COUNT(*) FROM els_subscriptions')[0], 0)
        with self.assertRaises(ValueError):
            self.manager.create('citizen', 'BAD', self.fields)
        self.assertFalse(self.manager.cancel('citizen', 'ELS1')[0])

    def test_missing_underlying_waits_and_evaluates_only_latest_round(self):
        self.subscribe_and_issue()
        with self.db.atomic() as conn:
            self.manager.observe(conn, self.issue_epoch + 30 * DAY, prices={})
        self.assertEqual(self.manager.get_details('ELS1')['state'], 'observation_pending')
        self.assertEqual(self.manager.process_due(self.issue_epoch + 30 * DAY), [])
        self.tick(self.issue_epoch + 60 * DAY, 87)
        details = self.manager.get_details('ELS1')
        self.assertEqual([r['outcome'] for r in details['observations']][:2], ['skipped', 'redeem'])
        self.assertEqual(self.manager.process_due(self.issue_epoch + 60 * DAY)[0]['gross'], 20395)

    def test_failed_settlement_keeps_snapshot_and_retries_without_duplicate_pay(self):
        self.subscribe_and_issue()
        self.tick(self.issue_epoch + 30 * DAY, 90)
        cash = self.db.users.get_user('citizen')['cash']
        with patch.object(self.manager, '_safe_credit', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                self.manager.process_due(self.issue_epoch + 30 * DAY)
        self.assertEqual(self.db.users.get_user('citizen')['cash'], cash)
        self.assertEqual(self.manager.get_details('ELS1')['state'], 'redemption_pending')
        self.assertEqual(self.db.products.execute_one('SELECT COUNT(*) FROM els_settlements')[0], 0)
        self.assertEqual(len(self.manager.process_due(self.issue_epoch + 31 * DAY)), 1)

    def test_index_basis_company_issuer_locked_cash_and_reference_guard(self):
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO issuer_cash_accounts VALUES ('CO',100000,0)")
        fields = dict(self.fields, underlyings='CO,__exchange_index__:INZ', issuer_code='CO')
        with patch('managers.els_manager.time.time', return_value=1000):
            self.manager.create('admin', 'ELS2', fields)
        total = self.total_money()
        self.assertTrue(self.manager.subscribe('citizen', 'ELS2', 1, now=1001)[0])
        account = self.db.products.execute_one("SELECT * FROM issuer_cash_accounts WHERE company_code='CO'")
        self.assertEqual(account['balance'], 99408)
        self.assertEqual(account['locked_amount'], 10592)
        with self.db.atomic() as conn:
            with self.assertRaises(ValueError):
                self.manager.ensure_unreferenced(conn, 'CO')
        self.tick(self.issue_epoch)
        details = self.manager.get_details('ELS2')
        self.assertEqual([r['initial_price'] for r in details['underlyings']], [100, 1000])
        self.tick(self.issue_epoch + 30 * DAY, 90)
        self.manager.process_due(self.issue_epoch + 30 * DAY)
        self.assertEqual(self.db.products.execute_one("SELECT locked_amount FROM issuer_cash_accounts WHERE company_code='CO'")[0], 0)
        self.assertEqual(self.total_money(), total)

    def test_treasury_self_subscription_empty_early_rounds_and_closed_account_fallback(self):
        fields = dict(self.fields, observation_days='', redemption_levels='', annual_coupon=0)
        with patch('managers.els_manager.time.time', return_value=1000):
            self.manager.create('admin', 'ELS2', fields)
        total = self.total_money()
        self.assertTrue(self.manager.subscribe(TREASURY_USER_ID, 'ELS2', 1, now=1001)[0])
        self.tick(self.issue_epoch)
        self.tick(self.issue_epoch + 180 * DAY, 70)
        self.manager.process_due(self.issue_epoch + 180 * DAY)
        self.assertEqual(self.total_money(), total)
        self.db.accounts.execute("INSERT INTO accounts(account_number,user_id,balance,principal,account_type) "
            "VALUES ('ACC1','citizen',100000,100000,'saving')")
        with patch('managers.els_manager.time.time', return_value=1000):
            self.manager.create('admin', 'ELS3', self.fields)
        self.tick(1000, 100)
        self.assertTrue(self.manager.subscribe('citizen', 'ELS3', 2, account_number='ACC1', now=1001)[0])
        self.tick(self.issue_epoch)
        self.db.accounts.execute("DELETE FROM accounts WHERE account_number='ACC1'")
        before = self.db.users.get_user('citizen')['cash']
        self.tick(self.issue_epoch + 30 * DAY, 90)
        payment = self.manager.process_due(self.issue_epoch + 30 * DAY)[0]
        self.assertEqual(payment['tax'], 6)
        self.assertEqual(self.db.users.get_user('citizen')['cash'], before + payment['net'])

    def test_trade_only_stop_allows_observation_but_blocks_subscription(self):
        self.assertTrue(self.manager.subscribe('citizen', 'ELS1', 2, now=1001)[0])
        sessions = MarketSessionManager(self.db)
        self.assertTrue(sessions.control('admin', 'trade_stop', now=1002)[0])
        self.assertFalse(self.manager.subscribe('citizen', 'ELS1', 1, now=1003)[0])
        self.tick(self.issue_epoch)
        self.assertEqual(self.manager.get_details('ELS1')['issue_epoch'], self.issue_epoch)
        self.tick(self.issue_epoch+DAY, 50)
        self.assertTrue(self.manager.get_details('ELS1')['knocked_in'])

    def test_halt_blocks_subscription_issuance_and_observation(self):
        sessions = MarketSessionManager(self.db)
        self.assertTrue(sessions.control('admin', 'close', now=1001)[0])
        self.assertFalse(self.manager.subscribe('citizen', 'ELS1', 2, now=1002)[0])
        sessions.control('admin', 'open', now=1002)
        self.assertTrue(self.manager.subscribe('citizen', 'ELS1', 2, now=1003)[0])
        sessions.control('admin', 'close', now=1004)
        self.tick(self.issue_epoch)
        self.assertIsNone(self.manager.get_details('ELS1')['issue_epoch'])
        sessions.control('admin', 'open', now=self.issue_epoch)
        self.tick(self.issue_epoch)
        sessions.control('admin', 'close', now=self.issue_epoch + DAY)
        self.tick(self.issue_epoch + 30 * DAY, 50)
        self.assertFalse(self.manager.get_details('ELS1')['knocked_in'])
        self.assertEqual(self.manager.process_due(self.issue_epoch + 30 * DAY), [])
        sessions.control('admin', 'open', now=self.issue_epoch + 31 * DAY)
        self.tick(self.issue_epoch + 31 * DAY, 90)
        self.assertFalse(self.manager.get_details('ELS1')['knocked_in'])
        self.assertEqual(len(self.manager.process_due(self.issue_epoch + 31 * DAY)), 1)

    def test_basis_and_terms_are_immutable_after_subscription_and_valid_partial_tick_retains_knock_in(self):
        fields = dict(self.fields, underlyings='CO,__exchange_index__:INZ')
        with patch('managers.els_manager.time.time', return_value=1000):
            self.manager.create('admin', 'ELS2', fields)
        self.assertTrue(self.manager.subscribe('citizen', 'ELS2', 1, now=1001)[0])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.products.execute("UPDATE els_terms SET annual_coupon=50 WHERE product_code='ELS2'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.products.execute("DELETE FROM els_underlyings WHERE product_code='ELS2'")
        self.tick(self.issue_epoch)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.products.execute("UPDATE els_underlyings SET initial_price=200 WHERE product_code='ELS2'")
        with self.db.atomic() as conn:
            self.manager.observe(conn, self.issue_epoch + DAY, prices={'CO': 55, '__exchange_index__:INZ': None})
        details = self.manager.get_details('ELS2')
        self.assertEqual(details['state'], 'observation_pending')
        self.assertTrue(details['knocked_in'])
        self.assertTrue(details['underlyings'][0]['knocked_in'])

    def test_one_recipient_limit_does_not_block_other_contracts(self):
        self.db.users.create_user('citizen2', '시민2', '낙원', 1000000)
        with patch('managers.els_manager.time.time', return_value=1000):
            self.manager.create('admin', 'ELS2', self.fields)
        self.assertTrue(self.manager.subscribe('citizen', 'ELS1', 2, now=1001)[0])
        self.assertTrue(self.manager.subscribe('citizen2', 'ELS2', 2, now=1001)[0])
        self.tick(self.issue_epoch)
        self.db.users.execute("UPDATE users SET cash=? WHERE user_id='citizen'", (MAX_MONEY,))
        self.tick(self.issue_epoch + 30 * DAY, 90)
        notifications = self.manager.process_due(self.issue_epoch + 30 * DAY)
        self.assertEqual([item['product_code'] for item in notifications], ['ELS2'])
        self.assertEqual(self.manager.last_settlement_errors[0]['product_code'], 'ELS1')
        self.assertEqual(self.manager.get_details('ELS1')['state'], 'redemption_pending')
        self.assertEqual(self.manager.get_details('ELS2')['state'], 'redeemed')
        self.assertEqual(self.db.products.execute_one("SELECT COUNT(*) FROM els_settlements WHERE product_code='ELS1'")[0], 0)

    def test_last_valid_index_constituent_is_protected_for_single_and_group_removal(self):
        self.assertTrue(self.manager.cancel('admin', 'ELS1')[0])
        fields = dict(self.fields, underlyings='__exchange_index__:INZ')
        with patch('managers.els_manager.time.time', return_value=1000):
            self.manager.create('admin', 'ELS2', fields)
        with self.db.atomic() as conn:
            self.assertTrue(self.manager.index_removal_blocked(conn, ['CO']))
            with self.assertRaises(ValueError):
                self.manager.ensure_unreferenced(conn, 'CO')
        self.db.companies.create_company('CO2', '다른회사', '게임', 100000, 1000, 100, 'admin', 'INZ')
        with self.db.atomic() as conn:
            self.assertFalse(self.manager.index_removal_blocked(conn, 'CO'))
            self.manager.ensure_unreferenced(conn, 'CO')
            self.assertTrue(self.manager.index_removal_blocked(conn, ['CO', 'CO2']))
        self.db.companies.execute("UPDATE company SET total_shares=0 WHERE company_code='CO2'")
        with self.db.atomic() as conn:
            self.assertTrue(self.manager.index_removal_blocked(conn, ['CO']))


if __name__ == '__main__':
    unittest.main()
