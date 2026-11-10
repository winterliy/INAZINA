import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from database.fund_accounting import PREFIX
from database.user_db import citizen_filter
from managers.admin_control_manager import AdminControlManager
from managers.corporate_actions_manager import CorporateActionsManager
from managers.fund_manager import FundManager
from tests import test_market_features as fixtures


class FundAccountingTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue

    def fund(self, code='FD', components=None, category='equity'):
        AdminControlManager(SimpleNamespace(db=self.db)).entity(
            'admin', 'fund', 'create', code, name=code, price='100', supply='1000',
            extra={'exchange_id': 'EX', 'fund_category': category, 'components': components or {'CO': 1}},
        )
        return FundManager(self.db)

    def ok(self, result):
        self.assertTrue(result[0], result[1])

    def test_fund_is_not_citizen_and_cash_is_segregated(self):
        before = self.db.users.execute_one(f'SELECT COUNT(*) FROM users WHERE {citizen_filter()}')[0]
        treasury = self.db.users.get_treasury_balance()
        fund = self.fund()
        self.fund('SECOND')
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.assertIsNone(self.db.users.get_user(PREFIX+'FD'))
        self.assertEqual(before, self.db.users.execute_one(f'SELECT COUNT(*) FROM users WHERE {citizen_filter()}')[0])
        self.assertEqual(fund.snapshot('FD')['availableCash'], 1000)
        self.assertEqual(fund.snapshot('SECOND')['availableCash'], 0)
        self.assertEqual(self.db.users.get_treasury_balance(), treasury)
        self.assertEqual(self.db.users.get_user('alice')['cash'], 9000)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.users.create_user(PREFIX+'FD', 'Fake', 'Nac')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.products.execute("DELETE FROM fund_cash_ledger WHERE product_code='FD'")

    def test_fund_cash_common_journal_reconciles_and_detects_missing_posting(self):
        from database.fund_accounting import change_cash
        from managers.accounting_journal import AccountingJournal
        fund = self.fund()
        self.ok(fund.activate('admin', 'FD'))
        with self.db.atomic() as conn:
            change_cash(conn, PREFIX+'FD', 25, 'fixture', 'fund-journal-test')
            check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                         if item['ledger_scope'] == 'fund')
            self.assertEqual(check['status'], 'matched', check)
            self.assertEqual(AccountingJournal.audit_unbalanced(conn, 'fund'), [])
        self.db.currency.execute("DROP TRIGGER trg_fund_cash_common_post")
        with self.db.atomic() as conn:
            change_cash(conn, PREFIX+'FD', 5, 'fixture', 'fund-journal-missing')
            check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                         if item['ledger_scope'] == 'fund')
            self.assertEqual(check['status'], 'mismatch', check)
            self.assertTrue(any(gap['status'] == 'missing_or_mismatched_posting'
                                for gap in check['posting_gaps']), check)
        self.db.currency._ensure_fund_cash_common_journal()
        self.db.currency._ensure_fund_cash_common_journal()
        with self.db.atomic() as conn:
            check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                         if item['ledger_scope'] == 'fund')
            self.assertEqual(check['status'], 'matched', check)

    def test_fund_cash_ledger_object_supports_native_currency_balances(self):
        from database.fund_accounting import FundCashLedger
        from managers.accounting_journal import AccountingJournal
        fund = self.fund()
        self.ok(fund.activate('admin', 'FD'))
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','US Dollar',1500,2)"
        )
        with self.db.atomic() as conn:
            ledger = FundCashLedger(conn)
            entry_id = ledger.deposit(PREFIX+'FD', '12.34', currency_code='USD',
                                      event='dividend', reference='CO')
            balance = ledger.balance(PREFIX+'FD', currency_code='USD')
            check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                         if item['ledger_scope'] == 'fund')
        row = self.db.products.execute_one(
            "SELECT currency_code,decimal_places,amount_minor_units,balance_minor_units "
            "FROM fund_cash_ledger WHERE entry_id=?", (entry_id,)
        )
        self.assertEqual((balance, tuple(row)),
                         (1234, ('USD', 2, 1234, 1234)))
        self.assertEqual(check['status'], 'matched', check)

    def test_fund_dividend_receiver_posts_currency_aware_fund_cash_entry(self):
        from managers.fund_dividend_receiver import FundDividendReceiver
        fund = self.fund()
        self.ok(fund.activate('admin', 'FD'))
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','US Dollar',1500,2)"
        )
        with self.db.atomic() as conn:
            receipt = FundDividendReceiver(conn).receive(
                PREFIX+'FD', '1.23', currency_code='USD', reference='CO'
            )
        row = self.db.products.execute_one(
            "SELECT currency_code,amount_minor_units FROM fund_cash_ledger WHERE entry_id=?",
            (receipt['entry_id'],),
        )
        self.assertEqual((receipt['net_amount_units'], tuple(row)),
                         (1_230_000, ('USD', 123)))

    def test_buy_hold_sell_redeem_real_nav_and_cash_conservation(self):
        fund = self.fund()
        initial = sum(r[0] for r in self.db.users.execute('SELECT cash FROM users'))
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.ok(fund.order('admin', 'FD', 'CO', 'buy', 6, 100))
        state = fund.snapshot('FD')
        self.assertEqual((state['availableCash'], state['assetValue'], state['nav']), (400, 600, 100))
        self.assertEqual(state['positions'][0]['quantity'], 6)
        self.db.companies.update_share_price('CO', 120)
        self.assertEqual(fund.snapshot('FD')['nav'], 112)
        before = self.db.users.get_user('alice')['cash']
        self.assertFalse(self.users.sell_asset('alice', 'FD', 10)[0])
        self.assertEqual(self.db.users.get_user('alice')['cash'], before)
        self.assertEqual(fund.snapshot('FD')['issuedUnits'], 10)
        self.ok(fund.order('admin', 'FD', 'CO', 'sell', 6, 120))
        self.ok(self.book.place_limit_order('bob', 'CO', 'buy', 6, 120))
        self.assertEqual(fund.snapshot('FD')['availableCash'], 1120)
        self.ok(self.users.sell_asset('alice', 'FD', 10))
        state = fund.snapshot('FD')
        self.assertEqual((state['availableCash'], state['issuedUnits'], state['positions']), (0, 0, []))
        self.assertTrue(state['ledgerBalanced'])
        # Primary-issue proceeds now sit in the issuer's cash account instead of the treasury user.
        issuer_cash = self.db.corporate.execute_one('SELECT COALESCE(SUM(balance),0) FROM issuer_cash_accounts')[0]
        self.assertEqual(sum(r[0] for r in self.db.users.execute('SELECT cash FROM users')) + issuer_cash, initial)
        self.assertGreater(self.db.tax.execute_one("SELECT COALESCE(SUM(tax_amount),0) FROM tax_transactions WHERE user_id='alice'")[0], 0)

    def test_reservations_are_assets_cancel_refunds_once(self):
        fund = self.fund()
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.ok(fund.order('admin', 'FD', 'CO', 'buy', 4, 90))
        state = fund.snapshot('FD')
        self.assertEqual((state['availableCash'], state['reservedCash'], state['nav']), (640, 360, 100))
        order_id = state['orders'][0]['order_id']
        self.ok(fund.cancel('admin', 'FD', order_id))
        self.assertFalse(fund.cancel('admin', 'FD', order_id)[0])
        self.assertEqual(fund.snapshot('FD')['availableCash'], 1000)
        self.assertTrue(fund.snapshot('FD')['ledgerBalanced'])

    def test_partial_fill_price_improvement_and_cancel(self):
        fund = self.fund()
        self.db.companies.execute("UPDATE company SET total_shares=2 WHERE company_code='CO'")
        self.db.assets.add_user_asset('bob', 'CO', 1, unit_cost=100)
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.ok(fund.order('admin', 'FD', 'CO', 'buy', 2, 110))
        state = fund.snapshot('FD')
        self.assertEqual((state['availableCash'], state['reservedCash'], state['netAssets']), (790, 110, 1000))
        self.assertEqual(state['positions'][0]['quantity'], 1)
        self.ok(fund.cancel('admin', 'FD', state['orders'][0]['order_id']))
        self.assertEqual(fund.snapshot('FD')['availableCash'], 900)

    def test_permissions_composition_insufficient_cash_and_invalid_input(self):
        fund = self.fund()
        with self.assertRaises(ValueError):
            fund.order('alice', 'FD', 'CO', 'buy', 1, 100)
        self.assertFalse(fund.order('admin', 'FD', 'CO', 'buy', 1, 100)[0])
        self.db.assets.register_asset('GOLD', 'Gold', 'EX', 100, 'commodity_spot', 1000)
        with self.assertRaises(ValueError):
            fund.order('admin', 'FD', 'GOLD', 'buy', 1, 100)
        for invalid in (True, 0, -1, 'NaN', '0.5'):
            self.assertFalse(self.users.buy_asset('alice', 'FD', invalid)[0])
        self.assertEqual(fund.snapshot('FD')['availableCash'], 0)

    def test_fractional_subscriptions_and_component_trades(self):
        fund = self.fund()
        self.db.products.execute("UPDATE investment SET Fractal=1 WHERE product_code='FD'")
        self.db.companies.execute("UPDATE company SET Fractal=1 WHERE company_code='CO'")
        self.ok(self.products.trade_fund('alice', 'FD', '1.25'))
        self.ok(fund.order('admin', 'FD', 'CO', 'buy', '0.5', 100))
        state = fund.snapshot('FD')
        self.assertEqual((state['availableCash'], state['assetValue'], state['issuedUnits']), (75, 50, 1.25))
        self.ok(self.products.trade_fund('alice', 'FD', '0.25', redeem=True))
        self.assertEqual(fund.snapshot('FD')['availableCash'], 50)
        self.assertTrue(fund.snapshot('FD')['ledgerBalanced'])

    def test_dividend_is_received_by_fund_not_manager(self):
        from managers.accounting_journal import AccountingJournal
        fund = self.fund()
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.ok(fund.order('admin', 'FD', 'CO', 'buy', 6, 100))
        admin_cash = self.db.users.get_user('admin')['cash']
        self.ok(CorporateActionsManager(SimpleNamespace(db=self.db)).special_dividend('admin', 'CO', 5, 'fund-dividend', delay=0))
        self.assertEqual(fund.snapshot('FD')['availableCash'], 430)
        self.assertEqual(self.db.users.get_user('admin')['cash'], admin_cash)
        self.assertEqual(fund.snapshot('FD')['nav'], 103)
        with self.db.readonly() as conn:
            settlement = AccountingJournal.reconcile_dividend_payment_settlements(conn)
        self.assertEqual(settlement['status'], 'matched', settlement)

    def test_all_asset_classes_are_bought_with_fund_cash(self):
        self.issue('CB', 'corporate_bond', price=100, underlying='CO', days=30)
        self.db.assets.register_asset('GOLD', 'Gold', 'EX', 100, 'commodity_spot', 1000)
        self.db.assets.register_asset('LAND', 'Land', 'EX', 100, 'real_estate', 1000)
        self.db.assets.register_asset('ART', 'Art', 'EX', 100, 'special_asset', 1000)
        for code, category, components in (
            ('EQ', 'equity', {'CO': 1}), ('BD', 'bond', {'CB': 1}),
            ('MX', 'mixed', {'CO': .5, 'CB': .5}),
            ('OT', 'other', {'GOLD': .3, 'LAND': .3, 'ART': .4}),
        ):
            with self.subTest(category=category):
                fund = self.fund(code, components, category)
                self.ok(self.users.buy_asset('alice', code, 10))
                for asset in components:
                    self.ok(fund.order('admin', code, asset, 'buy', 1, 100))
                self.assertEqual(len(fund.snapshot(code)['positions']), len(components))
                self.assertEqual(fund.snapshot(code)['netAssets'], 1000)

    def test_corporate_bond_redemption_credits_fund(self):
        self.issue('CB', 'corporate_bond', price=100, underlying='CO', days=30)
        fund = self.fund(components={'CB': 1}, category='bond')
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.ok(fund.order('admin', 'FD', 'CB', 'buy', 5, 100))
        self.db.products.execute("UPDATE investment SET maturity_epoch=? WHERE product_code='CB'", (int(time.time())-1,))
        self.products.redeem_corporate_bonds()
        self.assertEqual(fund.snapshot('FD')['availableCash'], 1000)
        self.assertEqual(fund.snapshot('FD')['positions'], [])

    def test_invalid_corporate_bond_epochs_cannot_pay_or_redeem(self):
        self.issue('CB', 'corporate_bond', price=100, underlying='CO', days=30)
        self.db.products.execute(
            "UPDATE investment SET coupon_frequency_days=1,last_coupon_epoch=0,maturity_epoch=0 "
            "WHERE product_code='CB'"
        )
        self.assertTrue(self.users.buy_asset('alice', 'CB', 1)[0])
        cash_before = self.db.users.get_user('alice')['cash']
        bond_before = tuple(self.db.products.execute_one(
            "SELECT status,last_coupon_epoch,maturity_epoch FROM investment WHERE product_code='CB'"
        ))
        holding_before = [tuple(row) for row in self.db.assets.execute(
            "SELECT user_id,asset_code,quantity FROM user_assets WHERE asset_code='CB'"
        )]
        invalid_epochs = (-1, float('nan'), float('inf'), 1e300, True, 'invalid')
        for invalid_epoch in invalid_epochs:
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, 'invalid_corporate_bond_coupon_epoch'):
                    self.products.pay_corporate_bond_coupons(now=invalid_epoch)
                with self.assertRaisesRegex(ValueError, 'invalid_corporate_bond_redemption_epoch'):
                    self.products.redeem_corporate_bonds(now=invalid_epoch)
        self.assertEqual(self.db.users.get_user('alice')['cash'], cash_before)
        self.assertEqual(tuple(self.db.products.execute_one(
            "SELECT status,last_coupon_epoch,maturity_epoch FROM investment WHERE product_code='CB'"
        )), bond_before)
        self.assertEqual([tuple(row) for row in self.db.assets.execute(
            "SELECT user_id,asset_code,quantity FROM user_assets WHERE asset_code='CB'"
        )], holding_before)

    def test_treasury_bond_sale_and_maturity_credit_fund(self):
        from managers.accounting_journal import AccountingJournal
        self.ok(self.users.bond_manager.issue('TB', 'Treasury', 10, 365, 100, 10000, 'EX'))
        fund = self.fund(components={'TB': 1}, category='bond')
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.ok(fund.order('admin', 'FD', 'TB', 'buy', 5, 100))
        self.assertEqual(fund.snapshot('FD')['assetValue'], 500)
        self.ok(fund.order('admin', 'FD', 'TB', 'sell', 1, 100))
        self.ok(self.book.place_limit_order('bob', 'TB', 'buy', 1, 100))
        self.assertEqual(fund.snapshot('FD')['availableCash'], 600)
        self.db.accounts.execute("UPDATE treasury_bond_holdings SET maturity_epoch=? WHERE user_id=?", (int(time.time())-1, PREFIX+'FD'))
        self.users.bond_manager.redeem_matured(user_id=PREFIX+'FD')
        self.assertEqual(fund.snapshot('FD')['availableCash'], 1040)
        self.assertTrue(fund.snapshot('FD')['ledgerBalanced'])
        with self.db.readonly() as conn:
            bond_check = AccountingJournal.reconcile_treasury_bond_cash_events(conn)
            link = conn.execute("SELECT fund_entry_id FROM treasury_bond_cash_fund_links "
                                "WHERE event_key LIKE 'redemption:%'").fetchone()
        self.assertEqual(bond_check['status'], 'matched', bond_check)
        self.assertIsNotNone(link)

    def test_fund_distribution_comes_from_fund_not_treasury(self):
        fund = self.fund()
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        treasury = self.db.users.get_treasury_balance()
        corporate = CorporateActionsManager(SimpleNamespace(db=self.db))
        self.ok(corporate.special_dividend('admin', 'FD', 5, 'fund-distribution', delay=0))
        self.assertEqual(fund.snapshot('FD')['availableCash'], 950)
        self.assertGreaterEqual(self.db.users.get_treasury_balance(), treasury)  # Investor tax only.
        self.assertEqual(fund.snapshot('FD')['nav'], 95)
        self.ok(corporate.special_dividend('admin', 'FD', 5, 'fund-distribution', delay=0))
        self.assertEqual(fund.snapshot('FD')['availableCash'], 950)

    def test_cash_or_position_failure_rolls_back_subscription(self):
        fund = self.fund()
        from managers.fractional_trading_manager import FractionalTradingManager
        before = self.db.users.get_user('alice')['cash']
        with patch.object(FractionalTradingManager, '_add_lot', side_effect=ValueError('failure')):
            self.assertFalse(self.users.buy_asset('alice', 'FD', 10)[0])
        self.assertEqual(self.db.users.get_user('alice')['cash'], before)
        self.assertEqual(fund.snapshot('FD')['availableCash'], 0)
        self.assertEqual(fund.snapshot('FD')['ledger'], [])

    def test_price_changes_without_holdings_do_not_create_value(self):
        fund = self.fund()
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.db.companies.update_share_price('CO', 150)
        self.assertEqual(fund.snapshot('FD')['nav'], 100)
        self.ok(fund.order('admin', 'FD', 'CO', 'buy', 4, 150))
        self.db.companies.update_share_price('CO', 200)
        # Subscribe before a market tick: live NAV is 120, not cached 100.
        self.ok(self.users.buy_asset('bob', 'FD', 5))
        self.assertEqual(fund.snapshot('FD')['availableCash'], 1000)
        self.assertEqual(fund.snapshot('FD')['nav'], 120)

    def test_index_fund_uses_held_assets_not_unowned_index(self):
        self.issue('IF', 'index_fund', underlying='EX', days=0)
        fund = FundManager(self.db)
        self.ok(self.users.buy_asset('alice', 'IF', 10))
        self.db.companies.update_share_price('CO', 120)
        self.market.update_prices(volatility=0, now=int(time.time()))
        self.assertEqual(self.db.products.get_investment('IF')['unit_price'], 100)
        self.ok(fund.order('admin', 'IF', 'CO', 'buy', 5, 120))
        self.db.companies.update_share_price('CO', 140)
        self.market.update_prices(volatility=0, now=int(time.time())+1)
        self.assertEqual(self.db.products.get_investment('IF')['unit_price'], 110)

    def test_legacy_fund_with_holders_cannot_silently_convert(self):
        fund = self.fund()
        self.db.products.execute("DELETE FROM fund_accounts WHERE product_code='FD'")
        self.ok(self.users.buy_asset('alice', 'FD', 1))
        with self.assertRaisesRegex(ValueError, '승인'):
            fund.activate('admin', 'FD')
        self.assertFalse(fund.is_backed('FD'))

    def test_legacy_conversion_conserves_cash_and_is_not_repeated(self):
        fund = self.fund()
        self.db.products.execute("DELETE FROM fund_accounts WHERE product_code='FD'")
        self.ok(self.users.buy_asset('alice', 'FD', 5))
        treasury = self.db.users.get_treasury_balance()
        self.assertEqual(fund.snapshot('FD')['conversionCash'], 500)
        self.ok(fund.activate('admin', 'FD', expected_funding=500))
        self.assertEqual(fund.snapshot('FD')['availableCash'], 500)
        self.assertEqual(self.db.users.get_treasury_balance(), treasury-500)
        self.ok(fund.activate('admin', 'FD', expected_funding=500))
        self.assertEqual(self.db.users.get_treasury_balance(), treasury-500)
        self.assertEqual(fund.snapshot('FD')['nav'], 100)

    def test_rebalance_buys_to_target_and_updates_real_positions(self):
        fund = self.fund()
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        result = fund.rebalance('admin', 'FD')
        self.assertTrue(result['success'], result)
        self.assertFalse(result['pending'])
        self.assertEqual([(row['side'], row['quantity']) for row in result['orders']], [('buy', 10)])
        state = result['fund']
        self.assertEqual(state['availableCash'], 0)
        self.assertEqual(state['positions'][0]['quantity'], 10)
        self.assertAlmostEqual(state['allocations'][0]['actualWeight'], 1)

    def test_rebalance_sells_overweight_then_buys_after_fill(self):
        self.db.assets.register_asset('GOLD', 'Gold', 'EX', 100, 'commodity_spot', 1000)
        fund = self.fund(components={'CO': .5, 'GOLD': .5}, category='other')
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.ok(fund.order('admin', 'FD', 'CO', 'buy', 8, 100))
        result = fund.rebalance('admin', 'FD')
        self.assertTrue(result['pending'])
        self.assertEqual([(row['side'], row['quantity']) for row in result['orders']], [('sell', 3), ('buy', 2)])
        with self.assertRaisesRegex(ValueError, '미체결 주문'):
            fund.rebalance('admin', 'FD')
        self.ok(self.book.place_limit_order('bob', 'CO', 'buy', 3, 100))
        result = fund.rebalance('admin', 'FD')
        self.assertFalse(result['pending'])
        self.assertEqual([(row['assetCode'], row['side'], row['quantity']) for row in result['orders']], [('GOLD', 'buy', 3)])
        allocations = {row['code']: row for row in result['fund']['allocations']}
        self.assertAlmostEqual(allocations['CO']['actualWeight'], .5)
        self.assertAlmostEqual(allocations['GOLD']['actualWeight'], .5)
        self.assertEqual(result['fund']['availableCash'], 0)

    def test_rebalance_uses_market_cap_weights_for_index_fund(self):
        self.db.companies.create_company('CO2', 'Second', 'Industry', 100000, 300, 100, 'admin', 'EX')
        self.issue('IF', 'index_fund', underlying='EX', days=0)
        investment = FundManager(self.db)
        self.ok(self.users.buy_asset('alice', 'IF', 10))
        result = investment.rebalance('admin', 'IF')
        self.assertTrue(result['success'], result)
        self.assertEqual({row['assetCode']: row['quantity'] for row in result['orders']}, {'CO': 7, 'CO2': 2})
        self.assertEqual(sum(row['value'] for row in result['fund']['positions']) + result['fund']['availableCash'], 1000)

    def test_rebalance_requires_quiescent_fund_and_never_borrows(self):
        fund = self.fund()
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        self.ok(fund.order('admin', 'FD', 'CO', 'buy', 2, 90))
        with self.assertRaisesRegex(ValueError, '미체결 주문'):
            fund.rebalance('admin', 'FD')
        self.ok(fund.cancel('admin', 'FD', self.db.assets.execute_one(
            "SELECT order_id FROM market_orders WHERE user_id=? AND status='open'", (PREFIX+'FD',))[0]))
        result = fund.rebalance('admin', 'FD')
        self.assertEqual(result['fund']['availableCash'], 0)
        self.assertEqual(sum(row['value'] for row in result['fund']['positions']), 1000)
        self.assertTrue(result['fund']['ledgerBalanced'])
        with self.assertRaises(ValueError):
            fund.rebalance('alice', 'FD')

    def test_web_service_actions_and_ownership(self):
        from web_api import server
        self.fund()
        self.ok(self.users.buy_asset('alice', 'FD', 10))
        with patch.object(server, 'domain', return_value=SimpleNamespace(db=self.db)), \
             patch.object(server, 'connect_readonly', side_effect=AssertionError('wrong database')):
            state = server.manage_fund('admin', 'FD')
            self.assertEqual(state['availableCash'], 1000)
            result = server.manage_fund('admin', 'FD', {'action': 'order', 'assetCode': 'CO', 'side': 'buy', 'quantity': '2', 'price': 100})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['fund']['availableCash'], 800)
            with self.assertRaises(ValueError):
                server.manage_fund('alice', 'FD', {'action': 'activate'})
