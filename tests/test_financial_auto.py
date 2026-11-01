"""Automatic statement reconciliation uses sources, never fabricated earnings."""
import unittest
from decimal import Decimal
import test_inazina as fixtures
from managers.financial_manager import FinancialManager
from managers.company_account_manager import CompanyAccounts


class FinancialAutoTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self):
        self.db.companies.create_company('AUTO', '자동 재무', '게임', 10000, 100, 100, 'admin', 'INZ')
        self.fin = FinancialManager(self.db)
        self.fin.report('AUTO')

    def test_actual_cash_is_balanced_without_fabricated_profit_and_idempotent(self):
        self.prepare()
        with self.db.atomic() as conn:
            conn.execute("INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES ('AUTO')")
            conn.execute("UPDATE issuer_cash_accounts SET balance=1234567890123 WHERE company_code='AUTO'")
        report = self.fin.report('AUTO')
        self.assertEqual(report['balances']['cash'], 1234567890123)
        self.assertEqual(report['income']['profit'], 0)
        self.assertTrue(report['checks']['balance_sheet'])
        self.assertTrue(report['checks']['live_cash'])
        self.assertEqual(self.fin.report('AUTO')['entry_count'], report['entry_count'])

    def test_positions_preserve_manual_assets_and_returning_snapshot(self):
        self.prepare()
        self.fin.manual('admin', 'AUTO', '수동 금융자산', [{'account':'short_investments','amount':30}, {'account':'payables','amount':-30}])
        with self.db.atomic() as conn:
            positions = {'investments':Decimal('100.4'), 'treasury':Decimal(0), 'debt':Decimal(50)}
            self.fin.reconcile_sources(conn, 'AUTO', positions)
            self.assertIsNone(self.fin.reconcile_sources(conn, 'AUTO', positions))
            balances = self.fin.balances(conn, 'AUTO', 253402214400)
            self.assertEqual(balances['short_investments'],130)
            self.assertEqual(balances['short_debt'],-50)
            positions['investments'] = Decimal(200)
            self.fin.reconcile_sources(conn, 'AUTO', positions)
            positions['investments'] = Decimal(100)
            self.assertIsNotNone(self.fin.reconcile_sources(conn, 'AUTO', positions))

    def test_logged_income_booked_once_from_source(self):
        self.prepare()
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO company_income_log(company_code,kind,gross_nwx,tax_nwx,recorded_epoch) VALUES ('AUTO','interest_income',100,10,0)")
            conn.execute("INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES ('AUTO')")
            conn.execute("UPDATE issuer_cash_accounts SET balance=90 WHERE company_code='AUTO'")
        report = CompanyAccounts.financial_report(self.db, 'AUTO')
        self.assertEqual(report['changes']['interest_income'], -100)
        self.assertEqual(report['income']['profit'],90)
        self.assertEqual(CompanyAccounts.financial_report(self.db,'AUTO')['entry_count'],report['entry_count'])

    def test_closed_report_is_preserved(self):
        self.prepare()
        import json
        from managers.financial_manager import month
        with self.db.atomic() as conn:
            saved = self.fin.build(conn,'AUTO',month())
            saved['closed'] = True
            conn.execute('INSERT INTO financial_periods VALUES (?,?,?,?)',('AUTO',month(),0,json.dumps(saved)))
        self.assertEqual(CompanyAccounts.financial_report(self.db,'AUTO'), saved)

    def test_issuance_capital_reconciles_without_revenue(self):
        self.prepare()
        self.db.companies.execute("UPDATE company SET capital=20000 WHERE company_code='AUTO'")
        report = self.fin.report('AUTO')
        self.assertEqual(report['balances']['common_capital'], -20000)
        self.assertEqual(report['income']['profit'], 0)
        self.assertTrue(report['checks']['balance_sheet'])
        self.assertEqual(self.fin.report('AUTO')['entry_count'], report['entry_count'])

    def test_company_holdings_automatically_use_cost_not_market_price(self):
        self.prepare()
        self.db.companies.create_company('HELD', '보유 종목', '게임', 10000, 100, 100, 'admin', 'INZ')
        with self.db.atomic() as conn:
            account = CompanyAccounts.account_id(conn, 'AUTO')
        self.db.assets.add_user_asset(account, 'HELD', 2, unit_cost=100)
        self.db.companies.execute("UPDATE company SET share_price=99999 WHERE company_code='HELD'")
        report = CompanyAccounts.financial_report(self.db, 'AUTO')
        self.assertEqual(report['balances']['short_investments'], 200)
        self.assertEqual(report['income']['profit'], 0)
        self.assertEqual(CompanyAccounts.financial_report(self.db, 'AUTO')['entry_count'], report['entry_count'])
