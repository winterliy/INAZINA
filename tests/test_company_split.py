import unittest

import test_inazina as fixtures
from managers.company_account_manager import CompanyAccounts
from managers.corporate_actions_manager import CorporateActionsManager
from managers.currency_manager import CurrencyManager
from managers.financial_manager import FinancialManager


class CompanySplitTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def setup_company(self):
        # 100 shares at 100 NWX, capital 10,000; 70 held by the citizen, 20 by the admin, 10 unissued.
        self.db.companies.create_company('CO', '회사', '게임', 10000, 100, 100, 'admin', 'INZ')
        self.db.assets.add_user_asset('citizen', 'CO', 70, unit_cost=80)
        self.db.assets.add_user_asset('admin', 'CO', 20, unit_cost=120)
        self.actions = CorporateActionsManager(self.bot)
        self.fin = FinancialManager(self.db)
        self.assertTrue(self.actions.fund_issuer('admin', 'CO', 'add', 1000)[0])

    def holding(self, user, code):
        row = self.db.assets.execute_one(
            'SELECT SUM(quantity),MAX(average_cost) FROM user_assets WHERE user_id=? AND asset_code=?', (user, code))
        return (row[0] or 0, row[1])

    def company(self, code):
        return self.db.companies.execute_one(
            'SELECT share_price,total_shares,capital,status,quote_currency FROM company WHERE company_code=?', (code,))

    def cash(self, code):
        row = self.db.companies.execute_one('SELECT balance FROM issuer_cash_accounts WHERE company_code=?', (code,))
        return row[0] if row else None

    def test_spin_off_gives_shareholders_new_shares_and_splits_value(self):
        self.setup_company()
        ok, message = self.actions.split('admin', '인적', 'CO', 'NEW', '신설 회사', '30%')
        self.assertTrue(ok, message)
        self.assertEqual(tuple(self.company('CO'))[:3], (70, 100, 7000))
        self.assertEqual(tuple(self.company('NEW'))[:4], (30, 100, 3000, 'active'))
        # 1:1 allocation; cost basis splits 70/30 so the holder's total basis is unchanged.
        self.assertEqual(self.holding('citizen', 'CO'), (70, 56))
        self.assertEqual(self.holding('citizen', 'NEW'), (70, 24))
        self.assertEqual(self.holding('admin', 'NEW')[0], 20)
        treasury = CurrencyManager.exchange_treasury(self.db.companies.conn, 'CO')
        self.assertEqual(self.holding(treasury, 'NEW')[0], 10)  # the unissued parent shares' part
        self.assertEqual((self.cash('CO'), self.cash('NEW')), (700, 300))
        for code in ('CO', 'NEW'):
            report = self.fin.report(code)
            self.assertTrue(report['checks']['balance_sheet'], code)
            self.assertTrue(report['checks']['live_cash'], code)
        self.assertEqual(self.fin.report('NEW')['balances']['cash'], 300)
        record = self.db.companies.execute_one(
            "SELECT action_kind,source_code,target_code FROM corporate_action_records WHERE action_kind='spin_off'")
        self.assertEqual(tuple(record), ('spin_off', 'CO', 'NEW'))

    def test_split_off_makes_a_wholly_owned_subsidiary(self):
        self.setup_company()
        ok, message = self.actions.split('admin', '물적', 'CO', 'SUB', '자회사', '40', '1000')
        self.assertTrue(ok, message)
        self.assertEqual(self.company('CO')[0], 100)  # parent price unchanged
        self.assertEqual(self.company('CO')[2], 10000)  # parent capital unchanged
        self.assertEqual(tuple(self.company('SUB'))[:3], (4, 1000, 4000))
        account = CompanyAccounts.account_id(self.db.companies.conn, 'CO')
        self.assertEqual(self.holding(account, 'SUB')[0], 1000)
        self.assertEqual(self.holding('citizen', 'SUB')[0], 0)
        self.assertEqual(self.holding('citizen', 'CO'), (70, 80))
        self.assertEqual((self.cash('CO'), self.cash('SUB')), (600, 400))
        parent = self.fin.report('CO')
        self.assertTrue(parent['checks']['balance_sheet'])
        self.assertTrue(parent['checks']['live_cash'])
        self.assertEqual(parent['balances']['investments'], 400)
        self.assertIn('SUB', [item['code'] for item in FinancialManager.subsidiaries(self.db.companies.conn, 'CO')])

    def test_split_refuses_bad_input_and_changes_nothing(self):
        self.setup_company()
        cases = [
            ('합동', 'CO', 'X1', '이름', '30'),       # unknown kind
            ('인적', 'CO', 'CO', '이름', '30'),       # code in use
            ('인적', 'CO', 'X2', '이름', '0'),        # ratio out of range
            ('인적', 'CO', 'X3', '이름', '100'),
            ('인적', 'CO', 'X4', '이름', '33.333'),   # more than two decimals
            ('인적', 'NOPE', 'X5', '이름', '30'),     # unknown company
        ]
        for case in cases:
            ok, message = self.actions.split('admin', *case)
            self.assertFalse(ok, case)
            self.assertTrue(message.startswith('❌'), message)
        self.assertFalse(self.actions.split('citizen', '인적', 'CO', 'X6', '이름', '30')[0])  # not an admin
        self.assertEqual(tuple(self.company('CO'))[:3], (100, 100, 10000))
        self.assertEqual(self.cash('CO'), 1000)
        for code in ('X1', 'X2', 'X3', 'X4', 'X5', 'X6'):
            self.assertIsNone(self.company(code))

    def test_split_refuses_a_company_with_open_orders(self):
        self.setup_company()
        self.db.assets.execute(
            "INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,remaining_quantity,order_kind,status,"
            "created_epoch,updated_epoch,quote_currency) VALUES ('citizen','CO','sell',100,1,1,'asset','open',1,1,'NWX')")
        ok, message = self.actions.split('admin', '인적', 'CO', 'NEW', '신설', '30')
        self.assertFalse(ok)
        self.assertIn('미체결 주문', message)
        self.assertIsNone(self.company('NEW'))

    def test_admin_command_parses_quoted_name_and_optional_shares(self):
        self.setup_company()
        from managers.admin_commands_manager import AdminCommandsManager
        manager = AdminCommandsManager(self.bot)
        result = manager.execute('admin', '분할', '물적 CO SUB "새 자회사" 25% 500')
        self.assertIn('✅', result)
        self.assertEqual(self.db.companies.execute_one("SELECT name FROM company WHERE company_code='SUB'")[0], '새 자회사')
        self.assertEqual(self.company('SUB')[1], 500)
        with self.assertRaises(ValueError):
            manager.execute('admin', '분할', '물적 CO')


if __name__ == '__main__':
    unittest.main()
