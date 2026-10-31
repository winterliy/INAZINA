import unittest
import test_inazina as fixtures
from decimal import Decimal

class WideWalletTests(unittest.TestCase):
    setUp=fixtures.InazinaTests.setUp
    tearDown=fixtures.InazinaTests.tearDown

    def test_hundred_quadrillion_transfer_preserves_last_unit(self):
        self.db.users.execute("UPDATE users SET nationality='낙원' WHERE user_id='citizen'")
        self.db.users.execute("UPDATE users SET cash_minor=? WHERE user_id='admin'",(10**18+1,))
        before=self.db.users.get_user('citizen')['cash']
        result=self.bot.currency_mgr.transfer('admin','citizen',str(10**18))
        self.assertTrue(result)
        self.assertEqual(Decimal(str(self.db.users.get_user('admin')['cash'])),1)
        self.assertEqual(Decimal(str(self.db.users.get_user('citizen')['cash'])),Decimal(str(before))+10**18)

    def test_large_usd_transfer_preserves_one_cent_and_rejects_overspend(self):
        from managers.currency_exchange_manager import CurrencyExchangeManager
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1500',2)")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','USD')")
        self.db.users.execute("UPDATE users SET nationality='미국' WHERE user_id='admin'")
        self.db.users.execute("UPDATE users SET cash_minor=? WHERE user_id='admin'",(10**20+1,))
        before=self.db.users.get_user('citizen')['cash_minor']
        CurrencyExchangeManager(self.db).transfer('admin','citizen','1000000000000000000.00')
        self.assertEqual(self.db.users.get_user('admin')['cash_minor'],1)
        self.assertEqual(self.db.users.get_user('citizen')['cash_minor'],before+10**20)
        with self.assertRaises(ValueError):
            CurrencyExchangeManager(self.db).transfer('admin','citizen','0.02')
        self.assertEqual(self.db.users.get_user('admin')['cash_minor'],1)

    def test_large_company_funding_matches_financial_ledger(self):
        from managers.corporate_actions_manager import CorporateActionsManager
        from managers.financial_manager import FinancialManager
        self.db.companies.create_company('BIG','큰 법인','게임',100000,1000,100,'admin','INZ')
        ok,message=CorporateActionsManager(self.bot).fund_issuer('admin','BIG','modify',10**18+1)
        self.assertTrue(ok,message)
        report=FinancialManager(self.db).report('BIG')
        self.assertTrue(report['checks']['live_cash'],report['checks'])
        self.assertTrue(report['checks']['balance_sheet'],report['checks'])
        self.assertEqual(report['balances']['cash'],10**18+1)
