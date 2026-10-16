"""$login / $logout choose which country's treasury national commands act for."""
import asyncio
import unittest

import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from functions.admin_prefix import ActionModal, national_identity, national_login, national_logout
from managers.currency_manager import CurrencyManager


class NationalLoginTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('AAA','A국 화폐','1')")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('A국','AAA')")
        self.db.assets.register_asset('TRADE', '국가주식', 'EX', 100, 'stock')
        with self.db.atomic() as conn:
            self.treasury = CurrencyManager.treasury_id(conn, 'A국')
            conn.execute("UPDATE market_assets SET quote_currency='AAA' WHERE code='TRADE'")

    def buy(self):
        async def run():
            return ActionModal(self.bot, 'admin', '매수').execute(['TRADE', '2', '100'])
        return asyncio.run(run())

    def test_login_routes_national_orders_to_that_treasury_until_logout(self):
        self.prepare()
        self.assertEqual(national_identity(self.bot, 'admin'), (TREASURY_USER_ID, '낙원'))
        self.assertIn('A국', national_login(self.bot, 'admin', 'A국'))
        self.assertEqual(national_identity(self.bot, 'admin'), (self.treasury, 'A국'))
        self.assertIn('✅', self.buy())
        order = self.db.assets.execute_one("SELECT user_id,quantity FROM market_orders")
        self.assertEqual((order['user_id'], order['quantity']), (self.treasury, 2))
        # Another administrator keeps the default identity.
        self.assertEqual(national_identity(self.bot, 'other-admin'), (TREASURY_USER_ID, '낙원'))
        self.assertIn('로그아웃', national_logout(self.bot, 'admin'))
        self.assertEqual(national_identity(self.bot, 'admin'), (TREASURY_USER_ID, '낙원'))
        self.assertIn('로그인한 국가가 없습니다', national_logout(self.bot, 'admin'))

    def test_login_requires_an_administrator_and_a_registered_country(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, '국가 ID'):
            national_login(self.bot, 'admin', '없는나라')
        with self.assertRaises(ValueError):
            national_login(self.bot, 'citizen', 'A국')


if __name__ == "__main__":
    unittest.main()
