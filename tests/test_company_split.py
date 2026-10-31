import unittest

import test_inazina as fixtures
from managers.company_account_manager import CompanyAccounts
from managers.corporate_actions_manager import CorporateActionsManager
from managers.currency_manager import CurrencyManager
from managers.financial_manager import FinancialManager


class CompanySplitTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_face_split_updates_fractional_holdings_and_listed_shares(self):
        self.db.companies.create_company('FACE', '분할회사', '게임', 100000000, 100, 1000000, 'admin', 'INZ')
        self.db.companies.execute("UPDATE company SET listed_shares=40,dividend_per_share=20,liquidation_value_per_share=1000000 WHERE company_code='FACE'")
        self.db.assets.add_user_asset('citizen', 'FACE', 2.5, unit_cost=600000)
        actions = CorporateActionsManager(self.bot)
        ok, message = actions.adjust_face_value('admin', '분할', 'FACE', 10)
        self.assertTrue(ok, message)
        row = self.db.companies.execute_one("SELECT share_price,total_shares,listed_shares,dividend_per_share,liquidation_value_per_share FROM company WHERE company_code='FACE'")
        self.assertEqual(tuple(row), (100000, 1000, 400, 2, 100000))
        self.assertEqual(self.holding('citizen', 'FACE'), (25, 60000))

    def test_face_reverse_split_restores_share_terms(self):
        self.db.companies.create_company('FACE', '분할회사', '게임', 100000000, 1000, 100000, 'admin', 'INZ')
        self.db.companies.execute("UPDATE company SET listed_shares=400,dividend_per_share=2,liquidation_value_per_share=100000 WHERE company_code='FACE'")
        self.db.assets.add_user_asset('citizen', 'FACE', 25, unit_cost=60000)
        ok, message = CorporateActionsManager(self.bot).adjust_face_value('admin', '병합', 'FACE', 5)
        self.assertTrue(ok, message)
        row = self.db.companies.execute_one("SELECT share_price,total_shares,listed_shares,dividend_per_share,liquidation_value_per_share FROM company WHERE company_code='FACE'")
        self.assertEqual(tuple(row), (500000, 200, 80, 10, 500000))
        self.assertEqual(self.holding('citizen', 'FACE'), (5, 300000))

    def test_face_split_preserves_small_total_cost_basis(self):
        from managers.asset_cost_manager import AssetCostManager
        self.db.companies.create_company('LOW', '저원가', '게임', 10000, 100, 1000000, 'admin', 'INZ')
        self.db.assets.add_user_asset('citizen', 'LOW', 20, unit_cost=24)
        ok, message = CorporateActionsManager(self.bot).adjust_face_value('admin', '분할', 'LOW', 100)
        self.assertTrue(ok, message)
        row = self.db.assets.execute_one("SELECT * FROM user_assets WHERE asset_code='LOW'")
        self.assertEqual(row['quantity'], 2000)
        with self.db.atomic() as conn:
            self.assertEqual(AssetCostManager.total_cost_minor(conn, row['holding_id']), 480)
            self.assertEqual(AssetCostManager.sale_cost_minor(conn, row['holding_id'], 1000), 240)
            conn.execute('UPDATE user_assets SET quantity=quantity-1000 WHERE holding_id=?', (row['holding_id'],))
            self.assertEqual(AssetCostManager.total_cost_minor(conn, row['holding_id']), 240)
            AssetCostManager.upsert(conn, 'citizen', 'LOW', None, 10, 10000)
            self.assertEqual(AssetCostManager.total_cost_minor(conn, row['holding_id']), 100240)


    def test_face_split_uses_quote_currency_minor_units(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1400',2)")
        self.db.companies.create_company('USDCO', '달러주식', '게임', 10000, 100, 100, 'admin', 'INZ')
        self.db.companies.execute("UPDATE company SET quote_currency='USD',share_price=100.50 WHERE company_code='USDCO'")
        ok, message = CorporateActionsManager(self.bot).adjust_face_value('admin', '분할', 'USDCO', 10)
        self.assertTrue(ok, message)
        self.assertEqual(self.db.companies.get_company('USDCO')['share_price'], 10.05)
        ok, message = CorporateActionsManager(self.bot).adjust_face_value('admin', '병합', 'USDCO', 10)
        self.assertTrue(ok, message)
        self.assertEqual(self.db.companies.get_company('USDCO')['share_price'], 100.5)

    def test_partial_position_transfer_keeps_allocated_total_basis(self):
        from managers.asset_cost_manager import AssetCostManager
        self.db.companies.create_company('LOW', '저원가', '게임', 10000, 100, 1000000, 'admin', 'INZ')
        self.db.assets.add_user_asset('citizen', 'LOW', 20, unit_cost=24)
        self.assertTrue(CorporateActionsManager(self.bot).adjust_face_value('admin', '분할', 'LOW', 100)[0])
        with self.db.atomic() as conn:
            source = conn.execute("SELECT holding_id FROM user_assets WHERE user_id='citizen' AND asset_code='LOW'").fetchone()[0]
            basis = AssetCostManager.sale_cost_minor(conn, source, 1000)
            AssetCostManager.upsert(conn, 'admin', 'LOW', None, 1000, 0, incoming_total_minor=basis)
            conn.execute('UPDATE user_assets SET quantity=1000 WHERE holding_id=?', (source,))
            target = conn.execute("SELECT holding_id FROM user_assets WHERE user_id='admin' AND asset_code='LOW'").fetchone()[0]
            self.assertEqual(AssetCostManager.total_cost_minor(conn, source), 240)
            self.assertEqual(AssetCostManager.total_cost_minor(conn, target), 240)

    def test_pledged_collateral_blocks_split_without_changes(self):
        self.db.companies.create_company('PLEDGED', '담보주식', '게임', 10000, 100, 1000000, 'admin', 'INZ')
        self.db.accounts.execute("INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,quote_currency) VALUES ('citizen','X',1,1,'active','NWX')")
        loan = self.db.accounts.execute_one('SELECT MAX(loan_id) FROM loans')[0]
        self.db.accounts.execute("INSERT INTO loan_collateral_assets(loan_id,user_id,asset_code,quantity,pledged_epoch,last_price,last_value) VALUES (?,'citizen','PLEDGED',1,0,1000000,1000000)", (loan,))
        ok, message = CorporateActionsManager(self.bot).adjust_face_value('admin', '분할', 'PLEDGED', 10)
        self.assertFalse(ok)
        self.assertIn('담보', message)
        self.assertEqual(self.db.companies.get_company('PLEDGED')['share_price'], 1000000)

    def test_bonus_after_split_keeps_exact_total_cost(self):
        from managers.asset_cost_manager import AssetCostManager
        self.db.companies.create_company('LOW', '저원가', '게임', 10000, 100, 1000000, 'admin', 'INZ')
        self.db.assets.add_user_asset('citizen', 'LOW', 20, unit_cost=24)
        actions = CorporateActionsManager(self.bot)
        self.assertTrue(actions.adjust_face_value('admin', '분할', 'LOW', 100)[0])
        ok, message = actions.increase_capital('admin', '무상', 'LOW', 1000)
        self.assertTrue(ok, message)
        with self.db.atomic() as conn:
            holding = conn.execute("SELECT holding_id FROM user_assets WHERE user_id='citizen' AND asset_code='LOW'").fetchone()[0]
            self.assertEqual(AssetCostManager.total_cost_minor(conn, holding), 480)

    def test_fractional_lot_basis_remains_fractional_after_split_and_transfer(self):
        from fractions import Fraction
        from managers.asset_cost_manager import AssetCostManager
        self.db.companies.create_company('FRAC', '소수주식', '게임', 10000, 100, 1000000, 'admin', 'INZ')
        self.db.assets.add_user_asset('citizen', 'FRAC', 0.5, unit_cost=1)
        actions = CorporateActionsManager(self.bot)
        self.assertTrue(actions.adjust_face_value('admin', '분할', 'FRAC', 10)[0])
        with self.db.atomic() as conn:
            lot = conn.execute("SELECT holding_id FROM user_assets WHERE user_id='citizen' AND asset_code='FRAC'").fetchone()[0]
            self.assertEqual(AssetCostManager.total_cost_minor(conn, lot), Fraction(1, 2))
            moved = AssetCostManager.sale_cost_minor(conn, lot, 1)
            self.assertEqual(moved, Fraction(1, 10))
            AssetCostManager.upsert(conn, 'admin', 'FRAC', None, 1, 0, incoming_total_minor=moved)
            conn.execute('UPDATE user_assets SET quantity=4 WHERE holding_id=?', (lot,))
            self.assertEqual(AssetCostManager.total_cost_minor(conn, lot), Fraction(2, 5))
            target = conn.execute("SELECT holding_id FROM user_assets WHERE user_id='admin' AND asset_code='FRAC'").fetchone()[0]
            self.assertEqual(AssetCostManager.total_cost_minor(conn, target), Fraction(1, 10))

    def test_split_cannot_inflate_value_below_minimum_quote_unit(self):
        self.db.companies.create_company('PENNY', '최소가격', '게임', 100, 100, 1, 'admin', 'INZ')
        ok, message = CorporateActionsManager(self.bot).adjust_face_value('admin', '분할', 'PENNY', 10)
        self.assertFalse(ok)
        self.assertIn('최소단위', message)
        self.assertEqual(self.db.companies.get_company('PENNY')['total_shares'], 100)

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
        account = self.db.companies.execute_one("SELECT user_id FROM company_accounts WHERE company_code='CO'")[0]
        self.assertEqual(self.holding(account, 'SUB')[0], 1000)
        self.assertEqual(self.holding('citizen', 'SUB')[0], 0)
        self.assertEqual(self.holding('citizen', 'CO'), (70, 80))
        self.assertEqual((self.cash('CO'), self.cash('SUB')), (600, 400))
        parent = self.fin.report('CO')
        self.assertTrue(parent['checks']['balance_sheet'])
        self.assertTrue(parent['checks']['live_cash'])
        # The parent holds the subsidiary at the book value it handed over (400 cash for 1,000 shares).
        self.assertEqual(self.holding(account, 'SUB')[1], 0)  # round(400/1000)
        self.assertIn('SUB', [item['code'] for item in FinancialManager.subsidiaries(self.db.companies.conn, 'CO')])

    def company_with_assets(self):
        """CO's company account holds 100 OTHER shares, a 10,000 NWX deposit and a 6,000 NWX loan."""
        from functions import company_actions
        self.setup_company()
        self.db.companies.create_company('OTHER', '다른회사', '게임', 1000, 1000, 50, 'admin', 'INZ')
        with self.db.atomic() as conn:
            account = CompanyAccounts.account_id(conn, 'CO')
        self.db.assets.add_user_asset(account, 'OTHER', 100, unit_cost=50)
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
                                 "interest_cycle,compound_type,issuer,quote_currency) VALUES ('DEP','예금',0,0,0,86400,0,'Bank','NWX')")
        self.assertTrue(self.actions.fund_issuer('admin', 'CO', 'add', 10000)[0])
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '계좌개설', {'product': 'DEP', 'password': 'pass1234', 'deposit': '10000'})
        self.assertTrue(ok, message)
        self.db.products.execute("INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
                                 "interest_cycle,min_credit_score,issuer) VALUES ('LN','대출',0,1000000,365,86400,0,'Bank')")
        with self.db.atomic() as conn:
            loan = conn.execute("INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,quote_currency,"
                                "contract_rate,last_payment_epoch) VALUES (?,'LN',6000,6000,'active','NWX',0,?)",
                                (account, 1_000_000)).lastrowid
            for number in (1, 2):
                conn.execute("INSERT INTO loan_payment_schedule(loan_id,installment_number,due_epoch,principal_due,interest_due) "
                             "VALUES (?,?,?,3000,10)", (loan, number, 2_000_000 + number))
            CompanyAccounts.reconcile(conn, 'CO')
        return account

    def test_split_divides_securities_deposits_and_loans_by_the_ratio(self):
        account = self.company_with_assets()
        ok, message = self.actions.split('admin', '인적', 'CO', 'NEW', '신설', '30%')
        self.assertTrue(ok, message)
        conn = self.db.companies.conn
        new_account = conn.execute("SELECT user_id FROM company_accounts WHERE company_code='NEW'").fetchone()[0]
        self.assertEqual(self.holding(account, 'OTHER'), (70, 50))
        self.assertEqual(self.holding(new_account, 'OTHER'), (30, 50))
        deposits = {row[0]: row[1] // 1_000_000 for row in conn.execute(
            "SELECT user_id,SUM(balance_units) FROM accounts WHERE product_code='DEP' GROUP BY user_id")}
        self.assertEqual(deposits, {account: 7000, new_account: 3000})
        loans = {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT user_id,principal,outstanding_balance FROM loans WHERE product_code='LN'")}
        self.assertEqual(loans, {account: (4200, 4200), new_account: (1800, 1800)})
        schedule = conn.execute("SELECT l.user_id,s.installment_number,s.principal_due,s.interest_due "
                                "FROM loan_payment_schedule s JOIN loans l USING(loan_id) ORDER BY l.user_id,s.installment_number").fetchall()
        self.assertEqual(sorted(tuple(row) for row in schedule), sorted([
            (account, 1, 2100, 7), (account, 2, 2100, 7), (new_account, 1, 900, 3), (new_account, 2, 900, 3)]))
        self.assertIn('보유 증권 1건', message)
        for code in ('CO', 'NEW'):
            report = self.fin.report(code)
            self.assertTrue(report['checks']['balance_sheet'], code)
            self.assertTrue(report['checks']['live_cash'], code)
        # Cash 300 (1,000 left after the deposit) + OTHER 1,500 + deposit 3,000 - loan 1,800 = 3,000 went to NEW.
        new = self.fin.report('NEW')
        self.assertEqual(new['summary']['equity'], 3000)
        self.assertEqual(new['balances']['common_capital'], -3000)

    def test_split_off_values_the_subsidiary_at_the_book_value_handed_over(self):
        account = self.company_with_assets()
        before = self.fin.report('CO')['changes']  # setup gifts (shares, a loan without cash) are already booked
        ok, message = self.actions.split('admin', '물적', 'CO', 'SUB', '자회사', '50', '100')
        self.assertTrue(ok, message)
        # Cash 500 + OTHER 2,500 + deposit 5,000 - loan 3,000 = 5,000 of book value for 100 shares.
        self.assertEqual(self.holding(account, 'SUB'), (100, 50))
        parent = self.fin.report('CO')
        self.assertTrue(parent['checks']['balance_sheet'])
        # Handing assets to a wholly owned subsidiary at book value is neither a gain nor a loss.
        self.assertEqual(parent['changes']['investment_gain'], before['investment_gain'])
        self.assertEqual(parent['changes']['investment_loss'], before['investment_loss'])
        self.assertEqual(self.fin.report('SUB')['summary']['equity'], 5000)

    def test_split_refuses_loans_that_are_not_in_good_standing(self):
        account = self.company_with_assets()
        self.db.accounts.execute("UPDATE loans SET collection_status='delinquent',overdue_since_epoch=1 WHERE user_id=?", (account,))
        ok, message = self.actions.split('admin', '인적', 'CO', 'NEW', '신설', '30')
        self.assertFalse(ok)
        self.assertIn('대출', message)
        self.assertIsNone(self.company('NEW'))
        self.assertEqual(self.holding(account, 'OTHER')[0], 100)

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
