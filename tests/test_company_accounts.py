"""Companies invest, bank and borrow through a capital account sharing the issuer cash."""
import unittest
from pathlib import Path

import test_inazina as fixtures
from functions import company_actions
from managers.company_account_manager import CompanyAccounts
from managers.bank_journal import BankJournal
from managers.corporate_actions_manager import CorporateActionsManager
from managers.financial_manager import FinancialManager


class CompanyAccountTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self):
        self.db.system.set_setting('tax_percent', 0)
        self.db.users.execute("UPDATE users SET nationality='낙원' WHERE user_id='citizen'")
        for code in ('CO', 'SUB'):
            self.db.companies.create_company(code, code, '게임', 100000, 1000, 100, 'admin', 'INZ')
        self.db.assets.execute("INSERT INTO market_exchanges(exchange_id,index_value,status,updated_epoch) "
                               "VALUES ('INZ',1000,'open',0) ON CONFLICT DO NOTHING")
        self.actions = CorporateActionsManager(self.bot)
        self.assertTrue(self.actions.fund_issuer('admin', 'CO', 'add', 100000)[0])

    def account(self, code='CO'):
        return company_actions.company_account(self.bot, code)

    def cash(self, code='CO'):
        return self.db.corporate.execute_one("SELECT balance FROM issuer_cash_accounts WHERE company_code=?", (code,))[0]

    def test_account_shares_the_issuer_cash_in_both_directions(self):
        self.prepare()
        account = self.account()
        self.assertEqual(self.db.users.get_user(account)['cash'], 100000)
        self.assertTrue(self.actions.fund_issuer('admin', 'CO', 'add', 1000)[0])
        self.assertEqual(self.db.users.get_user(account)['cash'], 101000)
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '송금', {'recipient': 'citizen', 'amount': '1000'})
        self.assertTrue(ok, message)
        self.assertEqual(self.cash(), 100000)
        # A company is never a citizen and never logs in.
        from database.user_db import citizen_filter
        rows = self.db.users.execute(f"SELECT user_id FROM users WHERE {citizen_filter()}")
        self.assertNotIn(account, {row['user_id'] for row in rows})

    def test_treasury_stock_and_subsidiary_through_ordinary_orders(self):
        self.prepare()
        self.db.assets.add_user_asset('citizen', 'CO', 10, unit_cost=100)
        self.db.assets.add_user_asset('citizen', 'SUB', 600, unit_cost=100)
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'CO', 'sell', 4, 100)[0])
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'SUB', 'sell', 600, 100)[0])
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '매수', {'asset': 'CO', 'quantity': '4', 'price': '100'})
        self.assertTrue(ok, message)
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '매수', {'asset': 'SUB', 'quantity': '600', 'price': '100'})
        self.assertTrue(ok, message)
        account = self.account()
        self.assertEqual(self.cash(), 100000 - 400 - 60000)
        holdings = {r['asset_code']: r['quantity'] for r in self.db.assets.execute(
            "SELECT asset_code,quantity FROM user_assets WHERE user_id=?", (account,))}
        self.assertEqual(holdings, {'CO': 4, 'SUB': 600})
        # SUB is now a subsidiary: CO's account is its largest shareholder.
        self.assertEqual(self.db.companies.get_company('SUB')['max_shareholder_id'], account)
        with self.db.atomic() as conn:
            summary = {row['company_code']: row for row in CompanyAccounts.summary(conn)}
        self.assertEqual(summary['SUB']['parents'][0]['code'], 'CO')
        # Treasury stock earns no dividend; outside holders do.
        self.assertTrue(self.actions.fund_issuer('admin', 'CO', 'add', 1000)[0])
        citizen = self.db.users.get_user('citizen')['cash']
        self.assertTrue(self.actions.special_dividend('admin', 'CO', 10, 'own', delay=0)[0])
        self.assertEqual(self.db.users.get_user('citizen')['cash'] - citizen, 60)
        # The statements still agree with the live issuer cash.
        report = FinancialManager(self.db).report('CO')
        self.assertTrue(report['checks']['live_cash'])
        self.assertTrue(report['checks']['balance_sheet'])
        self.assertGreater(report['balances']['treasury_stock'], 0)

    def acquire_subsidiary(self):
        self.db.assets.add_user_asset('citizen', 'SUB', 600, unit_cost=100)
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'SUB', 'sell', 600, 150)[0])
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '매수', {'asset': 'SUB', 'quantity': '600', 'price': '150'})
        self.assertTrue(ok, message)

    def test_consolidated_statements_eliminate_the_investment(self):
        self.prepare()
        with self.db.atomic() as conn:
            FinancialManager.ensure(conn, 'SUB')
        self.assertTrue(self.actions.fund_issuer('admin', 'SUB', 'add', 20000)[0])
        self.acquire_subsidiary()
        report = FinancialManager(self.db).report('CO')
        consolidated = report['consolidated']
        self.assertEqual([m['code'] for m in consolidated['subsidiaries']], ['SUB'])
        member = consolidated['subsidiaries'][0]
        held, cost = self.db.assets.execute_one(
            "SELECT SUM(quantity),SUM(quantity*average_cost) FROM user_assets WHERE user_id=? AND asset_code='SUB'",
            (self.account(),))
        ownership = held / 1000
        self.assertAlmostEqual(member['ownership'], ownership * 100)
        summary = consolidated['summary']
        sub = FinancialManager(self.db).report('SUB')['summary']
        # The investment at cost is replaced by the subsidiary's net assets; the excess is goodwill.
        self.assertEqual(member['cost'], round(cost))
        share = round(sub['equity'] * ownership)
        self.assertEqual(summary['goodwill'], max(0, round(cost) - share))
        self.assertEqual(summary['noncontrolling_interest'], sub['equity'] - share)
        self.assertEqual(summary['assets'], report['summary']['assets'] + sub['assets'] - round(cost) + summary['goodwill'])
        self.assertEqual(summary['equity'], summary['parent_equity'] + summary['noncontrolling_interest'])
        self.assertIsNone(FinancialManager(self.db).report('SUB')['consolidated'])

    def test_dr_underlying_equivalent_can_establish_subsidiary_control(self):
        self.prepare()
        parent_account = self.account('CO')
        self.db.assets.add_user_asset(parent_account, 'SUB', 400, unit_cost=100)
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity) "
            "VALUES ('SUB_DR','SUB','LSE','AAA',2,100,200)"
        )
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate) VALUES ('AAA','Test currency',2)"
        )
        self.db.assets.register_asset('SUB_DR', 'SUB DR', 'LSE', 1, 'depositary_receipt', 100)
        self.db.assets.execute("UPDATE market_assets SET quote_currency='AAA' WHERE code='SUB_DR'")
        self.db.assets.add_user_asset(parent_account, 'SUB_DR', 100, unit_cost=0.1)
        with self.db.atomic() as conn:
            subsidiaries = FinancialManager.subsidiaries(conn, 'CO')
        self.assertEqual(len(subsidiaries), 1)
        self.assertEqual(subsidiaries[0]['code'], 'SUB')
        self.assertEqual(subsidiaries[0]['ownership'], 0.6)
        # 100 receipts × 0.1 AAA × 2 NWX/AAA translates to 20 NWX.
        self.assertEqual(subsidiaries[0]['cost'], 40_020)

    def test_multi_level_foreign_and_intragroup_eliminations(self):
        self.prepare()
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('AAA','A국 화폐','2')")
        self.db.companies.create_company('GRAND', 'GRAND', '게임', 100000, 1000, 100, 'admin', 'INZ')
        self.db.companies.create_company('FOR', 'FOR', '게임', 100000, 1000, 100, 'admin', 'INZ')
        self.db.companies.execute("UPDATE company SET quote_currency='AAA' WHERE company_code='FOR'")
        co, sub = self.account('CO'), self.account('SUB')
        # CO controls SUB and FOR; SUB controls GRAND (held directly at cost 100 per share).
        for holder, code in ((co, 'SUB'), (sub, 'GRAND'), (co, 'FOR')):
            self.db.assets.add_user_asset(holder, code, 600, unit_cost=100)
        self.db.assets.add_user_asset('citizen', 'SUB', 400, unit_cost=100)
        # GRAND holds 10 shares of its ultimate parent: a cross-holding, i.e. group treasury stock.
        grand = self.account('GRAND')
        self.db.assets.add_user_asset(grand, 'CO', 10, unit_cost=100)
        with self.db.atomic() as conn:
            for code in ('GRAND', 'FOR'):
                FinancialManager.ensure(conn, code)
        # SUB pays CO a dividend: income of CO, but not of the group.
        self.assertTrue(self.actions.fund_issuer('admin', 'SUB', 'add', 5000)[0])
        self.db.companies.execute("UPDATE company SET dividend_funding='issuer' WHERE company_code='SUB'")
        self.assertTrue(self.actions.special_dividend('admin', 'SUB', 2, 'group-div', delay=0)[0])
        with self.db.atomic() as conn:
            for code in ('CO', 'SUB', 'GRAND'):
                CompanyAccounts.reconcile(conn, code)
        report = FinancialManager(self.db).report('CO')
        consolidated = report['consolidated']
        members = {m['code']: m for m in consolidated['subsidiaries']}
        self.assertEqual(set(members), {'SUB', 'FOR', 'GRAND'})
        self.assertEqual(members['GRAND']['parent'], 'SUB')
        self.assertEqual(members['FOR']['currency'], 'AAA')
        self.assertTrue(any('환산' in note for note in consolidated['notes']))
        self.assertEqual(consolidated['eliminations']['dividends'], 1200)   # 600 shares x 2
        self.assertEqual(consolidated['eliminations']['cross_holdings'], 1000)
        summary = consolidated['summary']
        self.assertEqual(summary['equity'], summary['parent_equity'] + summary['noncontrolling_interest'])
        self.assertEqual(summary['assets'] - summary['liabilities'], summary['equity'])

    def test_treasury_stock_has_no_vote(self):
        self.prepare()
        self.db.assets.add_user_asset('citizen', 'CO', 300, unit_cost=100)
        self.db.assets.add_user_asset('admin', 'CO', 200, unit_cost=100)
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'CO', 'sell', 290, 100)[0])
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '매수', {'asset': 'CO', 'quantity': '290', 'price': '100'})
        self.assertTrue(ok, message)
        # The company holds the most shares, but its own shares carry no vote.
        self.assertEqual(self.db.companies.get_company('CO')['max_shareholder_id'], 'admin')

    def test_income_is_classified_dividends_gains_and_transfers(self):
        self.prepare()
        account = self.account()
        self.db.assets.add_user_asset('citizen', 'SUB', 1000, unit_cost=100)  # no unissued supply left
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'SUB', 'sell', 50, 100)[0])
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '매수', {'asset': 'SUB', 'quantity': '50', 'price': '100'})
        self.assertTrue(ok, message)
        # SUB pays a dividend to its holders, including CO's account.
        self.assertTrue(self.actions.special_dividend('admin', 'SUB', 2, 'sub-div', delay=0)[0])
        # CO sells 20 shares at 150 (cost 100): a 1,000 gain.
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'SUB', 'buy', 20, 150)[0])
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '매도', {'asset': 'SUB', 'quantity': '20', 'price': '150'})
        self.assertTrue(ok, message)
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '송금', {'recipient': 'citizen', 'amount': '300'})
        self.assertTrue(ok, message)
        report = FinancialManager(self.db).report('CO')
        changes = report['changes']
        held = self.db.assets.execute_one("SELECT SUM(quantity) FROM user_assets WHERE user_id=? AND asset_code='SUB'", (account,))[0]
        self.assertEqual(-changes['other_income'], 100)          # 50 shares x 2 dividend
        self.assertEqual(-changes['investment_gain'], 1000)      # (150-100) x 20
        self.assertEqual(changes['other_expense'], 300)
        self.assertEqual(report['balances']['short_investments'], held * 100)
        self.assertTrue(report['checks']['live_cash'])
        self.assertTrue(report['checks']['balance_sheet'])

    def test_deposit_interest_is_interest_income(self):
        self.prepare()
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
                                 "interest_cycle,compound_type,issuer,quote_currency) VALUES ('DEP','예금',10,0,0,86400,0,'Bank','NWX')")
        ok, message, _ = company_actions.run(self.bot, 'admin', 'CO', '계좌개설', {'product': 'DEP', 'password': 'pass1234', 'deposit': '36500'})
        self.assertTrue(ok, message)
        self.db.accounts.execute("UPDATE accounts SET last_payment_epoch=last_payment_epoch-10*86400 WHERE product_code='DEP'")
        from managers.interest_manager import InterestManager
        InterestManager(self.db).process_user_interests(self.account())
        with self.db.atomic() as conn:
            CompanyAccounts.reconcile(conn, 'CO')
        changes = FinancialManager(self.db).report('CO')['changes']
        self.assertEqual(-changes['interest_income'], 100)   # 36,500 x 10% x 10/365
        self.assertEqual(changes['investment_gain'], 0)
        self.assertEqual(changes['investment_loss'], 0)

    def test_loan_interest_is_interest_expense(self):
        self.prepare()
        account = self.account()
        self.db.products.execute("INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
                                 "interest_cycle,min_credit_score,issuer) VALUES ('LN','대출',10,1000000,365,86400,0,'Bank')")
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,quote_currency,"
                         "contract_rate,last_payment_epoch) VALUES (?,'LN',36500,36500,'active','NWX',10,?)",
                         (account, 1_000_000))
            CompanyAccounts.reconcile(conn, 'CO')  # the borrowing itself
            loan = conn.execute("SELECT * FROM loans WHERE user_id=?", (account,)).fetchone()
            product = conn.execute("SELECT * FROM liability WHERE product_code='LN'").fetchone()
            from managers.interest_manager import InterestManager
            InterestManager.settle_loan_rate_boundary(conn, loan, product, 1_000_000 + 10 * 86400)
            CompanyAccounts.reconcile(conn, 'CO')
            # Reusing the stale fetched contract row must not accrue the same
            # interval again; institution journal and common mirror are atomic.
            InterestManager.settle_loan_rate_boundary(
                conn, loan, product, 1_000_000 + 10 * 86400
            )
            interest_posts = conn.execute(
                "SELECT COUNT(*) FROM bank_journal_entries WHERE issuer='Bank' "
                "AND event_type='loan_interest'"
            ).fetchone()[0]
            self.assertEqual(interest_posts, 1)
            from managers.accounting_journal import AccountingJournal
            common = {row['ledger_scope']: row
                      for row in AccountingJournal.reconcile_subledgers(conn)}
            self.assertEqual(common['institution']['status'], 'matched', common['institution'])
        changes = FinancialManager(self.db).report('CO')['changes']
        interest = self.db.accounts.execute_one("SELECT outstanding_balance FROM loans WHERE user_id=?", (account,))[0] - 36500
        self.assertGreater(interest, 0)
        self.assertEqual(changes['interest_expense'], interest)
        self.assertEqual(changes['investment_loss'], 36500)   # the loan arrived without cash in this test

    def test_company_account_can_log_in_with_its_code(self):
        self.prepare()
        account = self.account()
        from web_api import server
        old = server.DATABASE
        server.DATABASE = Path(self.tmp.name) / 'inazina.db'
        try:
            server.admin_set_web_password('admin', account, 'CompanyPass123')
            user, exists = server.authenticate_login_identifier('CO', 'CompanyPass123')
            self.assertTrue(exists)
            self.assertEqual((user['userId'], user['role'], user['cash']), (account, 'company', 100000))
            self.assertEqual(server.trading_user_id(account), account)
            self.assertIsNone(server.authenticate_login_identifier('CO', 'WrongPass123')[0])
            self.assertEqual(server.authenticate_login_identifier('company:CO', 'CompanyPass123')[0]['userId'], account)
            # Without a password the company still cannot log in.
            self.assertEqual(server.authenticate_login_identifier('SUB', 'CompanyPass123'), (None, False))
        finally:
            server.DATABASE = old

    def test_overdue_interest_is_booked_separately(self):
        self.prepare()
        account = self.account()
        self.db.products.execute("INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
                                 "interest_cycle,min_credit_score,issuer) VALUES ('LN','대출',10,1000000,30,86400,0,'Bank')")
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,quote_currency,"
                         "contract_rate,last_payment_epoch,maturity_epoch) VALUES (?,'LN',36500,36500,'active','NWX',10,?,?)",
                         (account, 1_000_000, 1_000_000))
            CompanyAccounts.reconcile(conn, 'CO')
            loan = conn.execute("SELECT * FROM loans WHERE user_id=?", (account,)).fetchone()
            product = conn.execute("SELECT * FROM liability WHERE product_code='LN'").fetchone()
            from managers.interest_manager import InterestManager
            InterestManager._process_overdue(conn, loan, product, 1_000_000 + 10 * 86400)
            CompanyAccounts.reconcile(conn, 'CO')
        changes = FinancialManager(self.db).report('CO')['changes']
        overdue = self.db.accounts.execute_one("SELECT outstanding_balance FROM loans WHERE user_id=?", (account,))[0] - 36500
        self.assertGreater(overdue, 0)
        self.assertEqual(changes['overdue_interest'], overdue)
        self.assertEqual(changes['interest_expense'], 0)

    def test_positional_company_command_and_errors(self):
        self.prepare()
        ok, message, _ = company_actions.run_positional(self.bot, 'admin', 'CO', '송금', ['citizen', '500'])
        self.assertTrue(ok, message)
        with self.assertRaises(ValueError):
            company_actions.run_positional(self.bot, 'admin', 'CO', '파산', [])
        with self.assertRaises(ValueError):
            company_actions.run(self.bot, 'citizen', 'CO', '송금', {'recipient': 'admin', 'amount': '1'})
        with self.assertRaisesRegex(ValueError, '보통주'):
            company_actions.company_account(self.bot, 'NONE')


if __name__ == "__main__":
    unittest.main()
