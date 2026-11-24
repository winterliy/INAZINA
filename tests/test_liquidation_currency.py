"""Liquidation cash is base currency; a holder with another home currency receives it exchanged."""
import unittest

import test_fractional_trading as fixtures
from managers.corporate_actions_manager import CorporateActionsManager


class LiquidationCurrencyTests(unittest.TestCase):
    setUp = fixtures.FractionalTradingTests.setUp
    tearDown = fixtures.FractionalTradingTests.tearDown
    command = fixtures.FractionalTradingTests.command
    stock = fixtures.FractionalTradingTests.stock

    def _foreign_holder(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('SCD','남방','1/120',2)")
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code,tax_percent) VALUES ('United Southerns','SCD',10)")
        self.db.users.create_user('scd', '남방인', 'United Southerns', 0)

    def _liquidate_with_preferred(self, holder):
        self.stock()
        self.command('상품생성', 'PR stock 우선주 게임 100000 10 100 EX 1 {"share_class":"preferred",'
                     '"parent_company_code":"ST","dividend_funding":"issuer","liquidation_value_per_share":1000}')
        self.db.assets.add_user_asset(holder, 'PR', 1, unit_cost=100)
        CorporateActionsManager(self.bot).fund_issuer('admin', 'ST', 'add', 1000)
        result = CorporateActionsManager(self.bot).liquidate('admin', 'ST', f'liquidate-{holder}')
        self.assertTrue(result[0], result[1])
        return self.db.corporate.execute_one(
            "SELECT paid_amount,tax_amount FROM corporate_liquidation_payments WHERE user_id=?", (holder,))

    def test_foreign_home_holder_receives_the_exchanged_value_not_the_same_number(self):
        self._foreign_holder()
        payment = self._liquidate_with_preferred('scd')
        self.assertEqual(int(payment['paid_amount']), 1000)  # the claim itself stays in base currency
        cash_minor = int(self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id='scd'")[0])
        # 1,000 base units at 1 SCD = 1/120 base is about 120,000.00 SCD, never 1,000.00 SCD.
        self.assertNotEqual(cash_minor, 1000 * 100)
        self.assertTrue(10_000_000 < cash_minor < 14_000_000, cash_minor)
        # The exchange is recorded against the payment so the amount can be recomputed later.
        payment_id = self.db.corporate.execute_one(
            "SELECT rowid FROM corporate_liquidation_payments WHERE user_id='scd'")[0]
        fx = dict(self.db.currency.execute_one(
            "SELECT * FROM cash_fx_settlements WHERE kind='liquidation' AND reference=?", (str(payment_id),)))
        net = int(payment['paid_amount']) - int(payment['tax_amount'])
        self.assertEqual((fx['user_id'], fx['source_currency'], fx['source_decimal_places'],
                          fx['source_minor_units'], fx['currency'], fx['decimal_places']),
                         ('scd', 'NWX', 0, net, 'SCD', 2))
        self.assertEqual(fx['minor_units'], cash_minor)
        self.assertEqual(cash_minor, net * int(fx['rate_numerator']) * 100 // int(fx['rate_denominator']))
        for statement in ("UPDATE cash_fx_settlements SET minor_units=1", "DELETE FROM cash_fx_settlements"):
            with self.subTest(statement=statement), self.assertRaisesRegex(Exception, 'append-only'):
                self.db.currency.execute(statement)

    def test_base_currency_holder_is_unchanged(self):
        before = self.db.users.get_user('citizen')['cash']
        payment = self._liquidate_with_preferred('citizen')
        self.assertEqual(self.db.users.get_user('citizen')['cash'] - before,
                         int(payment['paid_amount']) - int(payment['tax_amount']))
        self.assertEqual(self.db.currency.execute_one("SELECT COUNT(*) FROM cash_fx_settlements")[0], 0)

    def test_a_failed_exchange_record_rolls_the_whole_liquidation_back_and_the_retry_pays_once(self):
        from unittest.mock import patch
        from managers.currency_manager import CurrencyManager
        self._foreign_holder()
        self.stock()
        self.command('상품생성', 'PR stock 우선주 게임 100000 10 100 EX 1 {"share_class":"preferred",'
                     '"parent_company_code":"ST","dividend_funding":"issuer","liquidation_value_per_share":1000}')
        self.db.assets.add_user_asset('scd', 'PR', 1, unit_cost=100)
        CorporateActionsManager(self.bot).fund_issuer('admin', 'ST', 'add', 1000)

        def state():
            return (int(self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id='scd'")[0]),
                    self.db.corporate.execute_one("SELECT COUNT(*) FROM corporate_liquidation_payments")[0],
                    self.db.currency.execute_one("SELECT COUNT(*) FROM cash_fx_settlements")[0],
                    self.db.assets.execute_one("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id='scd'")[0])
        before = state()
        with patch.object(CurrencyManager, 'record_fx_settlement', side_effect=RuntimeError('fx record failure')):
            try:
                result = CorporateActionsManager(self.bot).liquidate('admin', 'ST', 'liquidate-rollback')
                self.assertFalse(result[0], result)
            except RuntimeError:
                pass
        self.assertEqual(state(), before)
        result = CorporateActionsManager(self.bot).liquidate('admin', 'ST', 'liquidate-rollback')
        self.assertTrue(result[0], result)
        after = state()
        self.assertEqual((after[1], after[2]), (before[1] + 1, before[2] + 1))
        CorporateActionsManager(self.bot).liquidate('admin', 'ST', 'liquidate-rollback')   # same key: no second payment
        self.assertEqual(state(), after)


if __name__ == '__main__':
    unittest.main()
