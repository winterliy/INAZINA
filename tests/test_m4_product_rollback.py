"""Fund, ELS and swap settlements are all-or-nothing: a failure injected into each path's own cash/ledger record undoes the whole
operation, and the retry books it exactly once."""
import time
import unittest
from unittest.mock import patch

import test_inazina as inazina_fixtures
from database.fund_accounting import FundCashLedger
from managers.accounting_journal import AccountingJournal
from managers.capital_markets_manager import CapitalMarketsManager
from tests import test_els, test_fund_accounting


class FundRollbackTests(unittest.TestCase):
    setUp = test_fund_accounting.FundAccountingTests.setUp
    tearDown = test_fund_accounting.FundAccountingTests.tearDown
    issue = test_fund_accounting.FundAccountingTests.issue
    fund = test_fund_accounting.FundAccountingTests.fund
    ok = test_fund_accounting.FundAccountingTests.ok

    def state(self, fund):
        snap = fund.snapshot('FD')
        return (self.db.users.get_user('alice')['cash'], snap['availableCash'], snap['issuedUnits'], len(snap['ledger']))

    def test_redemption_fund_cash_failure_rolls_the_redemption_back_and_the_retry_pays_once(self):
        fund = self.fund()
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        before = self.state(fund)
        with patch.object(FundCashLedger, 'post', side_effect=RuntimeError('fund cash failure')):
            try:
                result = self.users.sell_asset('alice', 'FD', 10)
                self.assertFalse(result[0], result)
            except RuntimeError:
                pass
        self.assertEqual(self.state(fund), before)
        self.ok(self.users.sell_asset('alice', 'FD', 10))
        after = self.state(fund)
        self.assertEqual((after[1], after[2]), (0, 0))
        self.assertGreater(after[0], before[0])
        self.assertFalse(self.users.sell_asset('alice', 'FD', 10)[0])          # nothing left to redeem a second time
        self.assertEqual(self.state(fund), after)


class ElsRollbackTests(unittest.TestCase):
    setUp = test_els.ELSTests.setUp
    tearDown = test_els.ELSTests.tearDown
    tick = test_els.ELSTests.tick
    subscribe_and_issue = test_els.ELSTests.subscribe_and_issue

    def state(self):
        return (self.db.users.get_user('citizen')['cash'],
                self.db.products.execute_one('SELECT COUNT(*) FROM els_settlements')[0],
                self.db.products.execute_one('SELECT COUNT(*) FROM business_cash_events WHERE kind=?', ('els_settlement',))[0],
                self.manager.get_details('ELS1')['state'])

    def test_settlement_ledger_link_failure_rolls_the_settlement_back_and_the_retry_pays_once(self):
        self.subscribe_and_issue()
        self.tick(self.issue_epoch + 30 * test_els.DAY, 90)
        before = self.state()
        real = AccountingJournal.link_cash

        def failing(conn, kind, *args, **kwargs):
            if kind == 'els_settlement':
                raise RuntimeError('ledger link failure')
            return real(conn, kind, *args, **kwargs)
        with patch.object(AccountingJournal, 'link_cash', staticmethod(failing)):
            with self.assertRaises(RuntimeError):
                self.manager.process_due(self.issue_epoch + 30 * test_els.DAY)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.manager.process_due(self.issue_epoch + 31 * test_els.DAY)), 1)
        after = self.state()
        self.assertEqual((after[1], after[2]), (before[1] + 1, before[2] + 1))
        self.assertGreater(after[0], before[0])
        self.assertEqual(self.manager.process_due(self.issue_epoch + 32 * test_els.DAY), [])
        self.assertEqual(self.state(), after)


class SwapRollbackTests(unittest.TestCase):
    setUp = inazina_fixtures.InazinaTests.setUp
    tearDown = inazina_fixtures.InazinaTests.tearDown
    seed = inazina_fixtures.InazinaTests.seed

    def test_swap_open_link_failure_rolls_back_margin_and_contract(self):
        self.seed()
        markets = CapitalMarketsManager(self.db, self.bot.user_mgr, self.bot.order_book)
        cash = self.db.users.get_user('citizen')['cash']

        def counts():
            return (self.db.users.get_user('citizen')['cash'],
                    self.db.accounts.execute_one("SELECT COUNT(*) FROM swap_contracts")[0],
                    self.db.accounts.execute_one("SELECT COUNT(*) FROM business_cash_events WHERE kind LIKE 'open:%'")[0])
        before = counts()
        with patch.object(AccountingJournal, 'link_swap_cash', side_effect=RuntimeError('swap link failure')):
            with self.assertRaises(RuntimeError):
                markets.open_swap('citizen', 1_000_000, '고정지급', 365)
        self.assertEqual(counts(), before)
        ok, message = markets.open_swap('citizen', 1_000_000, '고정지급', 365)
        self.assertTrue(ok, message)
        after = counts()
        self.assertEqual((after[0], after[1]), (cash - 50_000, before[1] + 1))

    def test_swap_settlement_failure_leaves_the_contract_unsettled_and_the_retry_settles_once(self):
        self.seed()
        markets = CapitalMarketsManager(self.db, self.bot.user_mgr, self.bot.order_book)
        self.assertTrue(markets.open_swap('citizen', 1_000_000, '고정지급', 365)[0])
        swap = self.db.accounts.execute_one("SELECT * FROM swap_contracts")
        self.db.currency.execute("UPDATE country_currencies SET base_rate=base_rate+5 WHERE nationality='낙원'")
        cash = self.db.users.get_user('citizen')['cash']
        now = swap['start_epoch'] + 10 * 86400
        with patch.object(markets, '_settle_swap', side_effect=RuntimeError('settlement failure')):
            try:
                markets.process_due(now=now)
            except RuntimeError:
                pass
        self.assertEqual(self.db.users.get_user('citizen')['cash'], cash)
        self.assertEqual(self.db.accounts.execute_one("SELECT last_settle_epoch FROM swap_contracts")[0], swap['last_settle_epoch'])
        markets.process_due(now=now)
        settled_cash = self.db.users.get_user('citizen')['cash']
        self.assertGreater(settled_cash, cash)
        markets.process_due(now=now)
        self.assertEqual(self.db.users.get_user('citizen')['cash'], settled_cash)
