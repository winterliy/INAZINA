"""Naturalization requests change nothing until an administrator approves them."""
import unittest

import test_inazina as fixtures
from managers.admin_commands_manager import AdminCommandsManager
from managers.currency_manager import CurrencyManager
from managers.naturalization_manager import NaturalizationManager


class NaturalizationTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def prepare(self):
        self.seed()  # citizen is 낙원 (NWX)
        with self.db.atomic() as conn:
            conn.execute("INSERT OR IGNORE INTO country_currencies(nationality,currency_code) VALUES ('달러국','USD')")
            CurrencyManager.treasury_id(conn, '달러국')
        self.db.assets.add_user_asset('citizen', 'INZST001', 3, unit_cost=100)
        return NaturalizationManager(self.bot)

    def holdings(self):
        return self.db.assets.execute_one("SELECT COUNT(*) FROM user_assets WHERE user_id='citizen'")[0]

    def test_request_changes_nothing_and_only_one_can_wait(self):
        manager = self.prepare()
        request_id = manager.request('citizen', '달러국', '이사')
        self.assertEqual(self.db.users.get_user('citizen')['nationality'], '낙원')
        self.assertEqual(self.holdings(), 1)
        self.assertEqual(manager.requests()[0]['request_id'], request_id)
        with self.assertRaisesRegex(ValueError, '심사 중인'):
            manager.request('citizen', '달러국')
        with self.assertRaisesRegex(ValueError, '이미 그 나라'):
            manager.check('citizen', '낙원')

    def test_approval_liquidates_and_moves_the_citizen(self):
        manager = self.prepare()
        request_id = manager.request('citizen', '달러국')
        self.assertIn('승인', manager.decide('admin', request_id, True))
        self.assertEqual(self.db.users.get_user('citizen')['nationality'], '달러국')
        self.assertEqual(self.holdings(), 0)
        self.assertEqual(manager.requests()[0]['status'], 'approved')
        with self.assertRaisesRegex(ValueError, '심사 대기'):
            manager.decide('admin', request_id, True)

    def test_rejection_and_the_admin_command(self):
        manager = self.prepare()
        request_id = manager.request('citizen', '달러국')
        self.assertIn(f'#{request_id}', AdminCommandsManager(self.bot).execute('admin', '귀화심사', '목록'))
        self.assertIn('거절', manager.decide('admin', request_id, False, '서류 미비'))
        self.assertEqual(self.db.users.get_user('citizen')['nationality'], '낙원')
        self.assertEqual(self.holdings(), 1)
        with self.assertRaisesRegex(ValueError, '관리자'):
            manager.decide('citizen', manager.request('citizen', '달러국'), True)


class CompanyAccountSyncTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def test_listing_opens_the_account_and_names_stay_equal(self):
        self.seed()
        self.bot.admin_control.entity('admin', 'stock', 'create', 'SYNC1', '동기화전자', '1000', '100', {'exchange_id': 'INZ'})
        account = self.db.users.execute_one("SELECT user_id,nickname FROM users WHERE user_id='company:SYNC1'")
        self.assertEqual(tuple(account), ('company:SYNC1', '동기화전자'))
        self.db.companies.execute("UPDATE company SET name='동기화텔레콤' WHERE company_code='SYNC1'")
        self.assertEqual(self.db.users.get_user('company:SYNC1')['nickname'], '동기화텔레콤')
        self.db.users.execute("UPDATE users SET nickname='동기화네트웍스' WHERE user_id='company:SYNC1'")
        self.assertEqual(self.db.companies.execute_one("SELECT name FROM company WHERE company_code='SYNC1'")[0], '동기화네트웍스')
