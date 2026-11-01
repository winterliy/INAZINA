"""Core money flows keep every unit at 10^18 and above."""
import unittest
from decimal import Decimal

import test_inazina as fixtures
from managers.admin_commands_manager import AdminCommandsManager

BIG = 10 ** 18 + 1


def dec(value):
    return Decimal(str(value))


class WideFlowTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def set_cash(self, user_id, amount):
        self.db.users.execute("UPDATE users SET cash=? WHERE user_id=?", (amount, user_id))

    def command(self, name, raw):
        return AdminCommandsManager(self.bot).execute('admin', name, raw)

    def test_savings_deposit_keeps_every_unit(self):
        self.command('상품생성', 'SV saving 예금 0 100 0 86400 1 Inazina')
        self.set_cash('citizen', 3 * 10 ** 18)
        ok, message = self.bot.bank_mgr.open_account('citizen', 'SV', 'pass1234', BIG)
        self.assertTrue(ok, message)
        account = self.db.accounts.execute_one(
            "SELECT balance FROM accounts WHERE user_id='citizen' AND account_type='saving'")
        self.assertEqual(dec(account['balance']), BIG)
        self.assertEqual(dec(self.db.users.get_user('citizen')['cash']), 3 * 10 ** 18 - BIG)

    def test_loan_principal_keeps_every_unit(self):
        self.command('상품생성', 'LN loan 대출 0 %d 30 86400 0 Inazina' % (3 * 10 ** 18))
        self.db.accounts.execute("UPDATE financial_institutions SET equity_capital=?", (10 ** 22,))
        self.db.users.execute("UPDATE users SET credit_score=1000 WHERE user_id='citizen'")
        ok, message = self.bot.bank_mgr.apply_loan('citizen', 'LN', BIG)
        self.assertTrue(ok, message)
        loan = self.db.accounts.execute_one("SELECT principal, outstanding_balance FROM loans WHERE user_id='citizen'")
        self.assertEqual(dec(loan['principal']), BIG)
        self.assertEqual(dec(loan['outstanding_balance']), BIG)

    def test_exchange_into_foreign_account_keeps_units(self):
        self.seed()
        self.bot.currency_mgr.open_account('citizen', 'INZCU001')
        self.set_cash('citizen', 5 * 10 ** 18 + 7)
        self.assertTrue(self.bot.currency_mgr.exchange('citizen', 'NWX', 'USD', str(10 ** 18)))
        units = self.db.accounts.execute_one(
            "SELECT balance_units FROM accounts WHERE user_id='citizen' AND account_type='saving'")[0]
        self.assertGreater(dec(units), 0)
        spent = 5 * 10 ** 18 + 7 - dec(self.db.users.get_user('citizen')['cash'])
        self.assertGreaterEqual(spent, 10 ** 18)
        self.assertEqual(spent, spent.to_integral_value())

    def test_resting_limit_order_reserves_exact_large_price(self):
        self.db.companies.create_company('BIG', '큰 법인', '게임', 10 ** 21, 1000, 10 ** 18 + 5, 'admin', 'INZ')
        self.set_cash('citizen', 3 * 10 ** 18)
        result = self.bot.order_book.place_limit_order('citizen', 'BIG', 'buy', 1, BIG)
        self.assertTrue(result[0], result)
        order = self.db.assets.execute_one(
            "SELECT limit_price, reserved_cash, status FROM market_orders WHERE user_id='citizen'")
        self.assertEqual(order['status'], 'open')
        self.assertEqual(dec(order['limit_price']), BIG)
        self.assertEqual(dec(order['reserved_cash']), BIG)
        self.assertEqual(dec(self.db.users.get_user('citizen')['cash']), 3 * 10 ** 18 - BIG)

    def test_marketable_order_fills_at_the_exact_quote(self):
        self.db.companies.create_company('BIG', '큰 법인', '게임', 10 ** 18, 1000, 100, 'admin', 'INZ')
        self.set_cash('citizen', 3 * 10 ** 18)
        result = self.bot.order_book.place_limit_order('citizen', 'BIG', 'buy', 1, BIG)
        self.assertTrue(result[0], result)
        self.assertEqual(dec(self.db.users.get_user('citizen')['cash']), 3 * 10 ** 18 - 100)

if __name__ == "__main__":
    unittest.main()
