import asyncio
import json
import sqlite3
import time
import unittest
from unittest.mock import patch
import test_inazina as fixtures
from database.financial_db import ACCOUNTS
from functions.financial_views import FinancialReportView,PAGES
from managers.financial_manager import FinancialManager,month
from managers.corporate_actions_manager import CorporateActionsManager


class FinancialTests(unittest.TestCase):
    setUp=fixtures.InazinaTests.setUp
    tearDown=fixtures.InazinaTests.tearDown

    def setup_company(self):
        self.db.companies.create_company('CO','회사','게임',10000,100,100,'admin','INZ')
        self.db.companies.execute("UPDATE company SET dividend_funding='issuer' WHERE company_code='CO'")
        self.fin=FinancialManager(self.db)
        self.actions=CorporateActionsManager(self.bot)

    def test_invalid_financial_epochs_cannot_create_entities_or_entries(self):
        self.setup_company()
        invalid_epochs=(-1,float('nan'),float('inf'),1e300,True,'invalid')
        with self.db.atomic() as conn:
            entity_count=conn.execute('SELECT COUNT(*) FROM financial_entities').fetchone()[0]
            entry_count=conn.execute('SELECT COUNT(*) FROM financial_entries').fetchone()[0]
            for epoch in invalid_epochs:
                with self.subTest(operation='ensure',epoch=epoch), self.assertRaises(ValueError):
                    self.fin.ensure(conn,'CO',epoch)
                with self.subTest(operation='post',epoch=epoch), self.assertRaises(ValueError):
                    self.fin.event(conn,'CO','invalid-time',[('receivables',1),('revenue',-1)],now=epoch)
        self.assertEqual(self.db.financial.execute_one('SELECT COUNT(*) FROM financial_entities')[0],entity_count)
        self.assertEqual(self.db.financial.execute_one('SELECT COUNT(*) FROM financial_entries')[0],entry_count)
        for epoch in invalid_epochs:
            with self.subTest(operation='month',epoch=epoch), self.assertRaisesRegex(ValueError,'회계 시각'):
                month(epoch)
            with self.subTest(operation='process_due',epoch=epoch), self.assertRaises(ValueError):
                self.fin.process_due(now=epoch)

    def test_invalid_internal_accounting_epochs_precede_balance_reads_and_writes(self):
        self.setup_company()
        invalid_epochs=(-1,float('nan'),float('inf'),1e300,True,'invalid')
        with self.db.atomic() as conn:
            for epoch in invalid_epochs:
                for operation in (
                    lambda: self.fin.merge(conn,'CO','OTHER',epoch),
                    lambda: self.fin.liquidate(conn,'CO',epoch),
                    lambda: self.fin.amortize_bond(conn,'CO','CB',epoch),
                    lambda: self.fin.record_shares(conn,'CO',epoch),
                    lambda: self.fin.balances(None,'CO',epoch),
                    lambda: self.fin.eps(None,'CO',epoch,100,0,0),
                    lambda: self.fin.eps(None,'CO',0,epoch,0,0),
                ):
                    with self.subTest(epoch=epoch,operation=operation), self.assertRaises(ValueError):
                        operation()
        self.assertEqual(self.db.financial.execute_one('SELECT COUNT(*) FROM financial_entries')[0],0)
        self.assertEqual(self.db.financial.execute_one('SELECT COUNT(*) FROM financial_share_history')[0],0)

    def test_opening_preferred_and_reports_have_no_invented_profit(self):
        self.setup_company()
        self.db.companies.create_company('PR','우선주','게임',1000,10,100,'admin','INZ')
        self.db.companies.execute("UPDATE company SET share_class='preferred',parent_company_code='CO' WHERE company_code='PR'")
        r=self.fin.report('PR')
        self.assertEqual(r['code'],'CO')
        self.assertEqual(r['income']['profit'],0)
        self.assertEqual(r['summary']['assets'],0)
        self.assertEqual(r['balances']['preferred_capital'],-1000)
        self.assertTrue(r['checks']['balance_sheet'])
        self.assertIsNone(r['ratios']['자기자본이익률']['value'])
        with self.assertRaises(ValueError):self.fin.report('UNKNOWN')
        async def render():
            view=FinancialReportView(self.db,'citizen',r)
            for page,_ in PAGES:
                view.page=page
                embed=view.build_embed()
                self.assertLessEqual(len(embed),6000)
                self.assertLessEqual(len(embed.fields),25)
        asyncio.run(render())

    def test_funding_dividends_and_rollback_are_atomic(self):
        self.setup_company()
        self.db.assets.add_user_asset('citizen','CO',2)
        self.assertTrue(self.actions.fund_issuer('admin','CO','add',1000)[0])
        self.assertTrue(self.actions.special_dividend('admin','CO',10,'fin:test', delay=0)[0])
        self.assertTrue(self.actions.special_dividend('admin','CO',10,'fin:test', delay=0)[0])
        r=self.fin.report('CO')
        self.assertEqual(r['balances']['cash'],980)
        self.assertEqual(r['income']['profit'],0)
        self.assertEqual(r['cash_flows']['financing'],980)
        self.assertTrue(r['checks']['live_cash'])
        before=r['entry_count']
        with self.assertRaises(RuntimeError):
            with self.db.atomic() as conn:
                FinancialManager.event(conn,'CO','rollback',[('receivables',100),('revenue',-100)])
                raise RuntimeError()
        self.assertEqual(self.fin.report('CO')['entry_count'],before)

    def test_common_journal_company_mapping_replay_and_legacy_rebuild(self):
        self.setup_company()
        with self.db.atomic() as conn:
            self.fin.event(conn,'CO','공통원장 매출',
                           [('receivables',125),('revenue',-125)],now=1756684800,key='common:test')
            common = conn.execute("SELECT entry_id,currency_code,decimal_places FROM accounting_entries "
                                  "WHERE ledger_scope='company' AND entity_id='CO' AND source_key='common:test'").fetchone()
            self.assertIsNotNone(common)
            self.assertEqual((common['decimal_places'],), (0,))
            self.assertEqual(tuple(conn.execute("SELECT SUM(debit_units),SUM(credit_units) FROM accounting_lines WHERE entry_id=?",(common['entry_id'],)).fetchone()), (125,125))
            # Retry is idempotent at both native and shared journal boundaries.
            self.fin.event(conn,'CO','공통원장 매출',
                           [('receivables',125),('revenue',-125)],now=1756684800,key='common:test')
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='company' AND source_key='common:test'").fetchone()[0],1)
        from managers.accounting_journal import AccountingJournal
        with self.db.atomic() as conn:
            # Simulate a native posting that predates common-ledger coverage.
            conn.execute("INSERT INTO financial_entries(company_code,period,recorded_epoch,source_key,description,cash_flow,status) "
                         "VALUES ('CO','2025-09',1756684800,'legacy:common','legacy fixture','operating','draft')")
            native_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.executemany("INSERT INTO financial_lines(entry_id,account,amount) VALUES (?,?,?)",
                             [(native_id,'receivables',125),(native_id,'revenue',-125)])
            conn.execute("UPDATE financial_entries SET status='posted' WHERE entry_id=?",(native_id,))
            self.assertEqual(AccountingJournal.rebuild_from_subledgers(conn)['company'],1)
            self.assertEqual(AccountingJournal.rebuild_from_subledgers(conn)['company'],0)
            self.assertEqual(AccountingJournal.reconcile_subledgers(conn)[0]['status'],'matched')
            self.assertEqual(AccountingJournal.audit_unbalanced(conn),[])
            with self.assertRaisesRegex(sqlite3.IntegrityError,'append-only'):
                conn.execute("UPDATE accounting_lines SET debit_units=126 WHERE entry_id=?",(common['entry_id'],))

    def test_common_journal_savepoint_rolls_back_partial_failure(self):
        self.setup_company()
        from managers.accounting_journal import AccountingJournal
        with self.db.atomic() as conn:
            self.fin.ensure(conn,'CO',1756684800)
            for account in ('receivables','revenue'):
                AccountingJournal.register_mapping(conn,'test',account,account)
            conn.execute("CREATE TEMP TRIGGER fail_common_line BEFORE INSERT ON accounting_lines "
                         "BEGIN SELECT RAISE(ABORT,'injected partial failure'); END")
            with self.assertRaisesRegex(sqlite3.IntegrityError,'injected partial failure'):
                AccountingJournal.post(conn,ledger_scope='test',entity_type='customer',entity_id='u1',
                    source_key='partial',currency_code='INZ',decimal_places=0,recorded_epoch=1,
                    lines=[('receivables','debit',10),('revenue','credit',10)])
            conn.execute("DROP TRIGGER fail_common_line")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='test'").fetchone()[0],0)

    def test_manual_authorization_balance_and_reversal(self):
        self.setup_company()
        lines=[{'account':'receivables','amount':1000},{'account':'revenue','amount':-1000}]
        with self.assertRaises(ValueError):self.fin.manual('citizen','CO','invalid',lines)
        with self.assertRaises(ValueError):self.fin.manual('admin','CO','invalid',[{'account':'receivables','amount':1},{'account':'revenue','amount':-2}])
        self.fin.manual('admin','CO','외상매출',lines)
        r=self.fin.report('CO')
        self.assertEqual(r['income']['revenue'],1000)
        self.assertEqual(r['summary']['assets'],1000)
        ident=self.db.financial.execute_one("SELECT entry_id FROM financial_entries WHERE source_key LIKE 'manual:%'")[0]
        self.fin.manual('admin','CO','취소',[],reversal=ident)
        self.assertEqual(self.fin.report('CO')['income']['revenue'],0)
        with self.assertRaises(ValueError):self.fin.manual('admin','CO','중복취소',[],reversal=ident)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.financial.execute('UPDATE financial_lines SET amount=1 WHERE entry_id=?',(ident,))

    def test_close_freezes_report_and_rejects_backdating(self):
        self.setup_company()
        old=1756684800
        with self.db.atomic() as conn:
            self.fin.ensure(conn,'CO',old)
            self.fin.event(conn,'CO','매출',[('receivables',200),('revenue',-200)],now=old+100)
        period=month(old)
        self.fin.close_period('admin','CO',period)
        r=self.fin.report('CO',period)
        self.assertTrue(r['closed'])
        self.assertEqual(r['income']['profit'],200)
        with self.assertRaises(ValueError):
            with self.db.atomic() as conn:self.fin.event(conn,'CO','late',[('receivables',5),('revenue',-5)],now=old+200)
        self.assertEqual(self.fin.report('CO',period),r)

    def test_foreign_precision_and_cash_drift(self):
        self.setup_company()
        self.db.currency.execute("INSERT INTO currencies(code,name,asset_code,reference_rate) VALUES ('USD','Dollar',NULL,1500)")
        self.db.currency.execute("UPDATE currencies SET decimal_places=2 WHERE code='USD'")
        self.db.companies.execute("UPDATE company SET quote_currency='USD' WHERE company_code='CO'")
        self.fin.manual('admin','CO','소수점매출',[{'account':'receivables','amount':'1.25'},{'account':'revenue','amount':'-1.25'}])
        r=self.fin.report('CO')
        self.assertEqual(r['currency'],'USD');self.assertEqual(r['income']['profit'],125)
        with self.assertRaises(ValueError):self.fin.manual('admin','CO','bad',[{'account':'receivables','amount':'0.001'},{'account':'revenue','amount':'-0.001'}])

    def test_cumulative_preferred_liability(self):
        self.setup_company()
        self.db.companies.create_company('PR','우선주','게임',1000,10,100,'admin','INZ')
        self.db.companies.execute("UPDATE company SET share_class='preferred',parent_company_code='CO',dividend_funding='issuer',cumulative_dividend=1 WHERE company_code='PR'")
        self.db.assets.add_user_asset('citizen','PR',2)
        self.fin.report('CO')
        self.assertTrue(self.actions.special_dividend('admin','PR',10,'owed', delay=0)[0])
        r=self.fin.report('CO')
        self.assertEqual(r['balances']['dividend_payable'],-20)
        self.actions.fund_issuer('admin','CO','add',20)
        with self.db.atomic() as conn:self.actions._settle_claims(conn,'CO',int(time.time()))
        r=self.fin.report('CO')
        self.assertEqual(r['balances']['dividend_payable'],0)
        self.assertEqual(r['balances']['cash'],0)
        self.assertTrue(r['checks']['balance_sheet'])

    def test_monthly_operations_cash_collection_and_retry(self):
        self.setup_company()
        from managers.financial_manager import period_bounds
        start,end=period_bounds('2025-09')
        with patch('managers.financial_manager.time.time',return_value=start+100):
            self.fin.configure('admin','CO',dict(enabled=True,revenue=1000,cogs=400,sga=100,tax_percent=10))
        self.fin.process_due(end+1)
        r=self.fin.report('CO','2025-09')
        self.assertEqual(r['income']['profit'],450)
        self.assertEqual(r['balances']['receivables'],1000)
        self.fin.process_due(end+100)
        self.assertEqual(self.fin.report('CO','2025-09')['entry_count'],r['entry_count'])
        self.fin.manual('admin','CO','수금',[{'account':'cash','amount':500},{'account':'receivables','amount':-500}],'operating')
        r=self.fin.report('CO')
        self.assertEqual(r['balances']['cash'],500)
        self.assertTrue(r['checks']['live_cash'])
        with self.assertRaises(ValueError):self.fin.manual('admin','CO','초과출금',[{'account':'cash','amount':-501},{'account':'sga','amount':501}],'operating')

    def test_slash_id_and_private_view_authorization(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from functions.slash_commands import SlashCommandRegistry
        self.setup_company()
        commands=[]
        SlashCommandRegistry(SimpleNamespace(tree=SimpleNamespace(add_command=commands.append))).register()
        command=next(c for c in commands if c.name=='재무')
        self.assertEqual([(p.name,p.required) for p in command.parameters],[('product_id',True)])
        async def run():
            view=FinancialReportView(self.db,'citizen',self.fin.report('CO'))
            other=SimpleNamespace(user=SimpleNamespace(id='other'),response=SimpleNamespace(send_message=AsyncMock()))
            self.assertFalse(await view.interaction_check(other))
            other.response.send_message.assert_awaited_once()
        asyncio.run(run())

    def test_merge_transfers_profit_into_retained_and_conserves_cash(self):
        self.setup_company()
        self.db.companies.create_company('B','합병회사','게임',1000,10,100,'admin','INZ')
        self.fin.manual('admin','B','이전회사매출',[{'account':'receivables','amount':100},{'account':'revenue','amount':-100}])
        self.actions.fund_issuer('admin','B','add',200)
        self.actions.fund_issuer('admin','CO','add',300)
        self.assertTrue(self.actions.merge('admin','CO','B')[0])
        r=self.fin.report('CO')
        self.assertEqual(r['balances']['cash'],500)
        self.assertEqual(r['income']['revenue'],0)
        self.assertTrue(r['checks']['live_cash'])
        self.assertTrue(r['checks']['balance_sheet'])
        self.assertEqual(self.fin.report('B')['summary']['equity'],0)

    def test_eps_weights_fractional_and_pledged_shares_and_capex(self):
        self.setup_company()
        from managers.financial_manager import period_bounds
        start,end=period_bounds('2025-09')
        with self.db.atomic() as conn:
            self.fin.ensure(conn,'CO',start-1)
            conn.execute('INSERT OR REPLACE INTO financial_share_history VALUES (?,?,?)',('CO',start,5000))
            conn.execute('INSERT OR REPLACE INTO financial_share_history VALUES (?,?,?)',('CO',(start+end)//2,10000))
            self.fin.event(conn,'CO','영업매출',[('receivables',750),('revenue',-750)],now=start+1)
        r=self.fin.report('CO','2025-09')
        self.assertEqual(r['weighted_shares'],7.5)
        self.assertEqual(r['eps'],100)
        self.db.assets.add_user_asset('citizen','CO',1.5)
        with self.db.atomic() as conn:
            conn.execute('INSERT INTO option_collateral_lots(contract_id,writer_id,underlying_code,quantity) VALUES (?,?,?,?)',(1,'citizen','CO',2))
        last=self.db.financial.execute_one('SELECT quantity_milli FROM financial_share_history WHERE company_code=? ORDER BY recorded_epoch DESC LIMIT 1',('CO',))[0]
        self.assertEqual(last,3500)
        self.actions.fund_issuer('admin','CO','add',1000)
        self.fin.manual('admin','CO','설비매입',[{'account':'ppe','amount':100},{'account':'cash','amount':-100}],'investing')
        r=self.fin.report('CO')
        self.assertEqual(r['capex'],100);self.assertEqual(r['fcf'],-100)
        self.assertTrue(r['checks']['live_cash'])
