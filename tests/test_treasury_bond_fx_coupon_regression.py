"""Coupon batch FX failures roll back prior lots and can be retried safely."""
import unittest
from unittest.mock import patch

from managers.accounting_journal import AccountingJournal
from test_treasury_bond_fx_settlement import _BondFxFixture, NOW, DAY


class CouponFxAtomicityTests(_BondFxFixture, unittest.TestCase):
    def test_second_coupon_fx_failure_rolls_back_batch_and_retry_is_idempotent(self):
        self._bond('COUPON-ONE', 150_000, coupon_days=180)
        self._bond('COUPON-TWO', 300_000, coupon_days=180)
        before_cash = self._cash_minor()
        tables = ('treasury_bond_cash_events', 'treasury_bond_cash_wallet_links', 'accounting_entries')
        baseline = {table: self.db.currency.execute_one(f'SELECT COUNT(*) FROM {table}')[0]
                    for table in tables}
        original = AccountingJournal.post.__func__
        settlement_posts = 0

        def fail_second(cls, conn, **kwargs):
            nonlocal settlement_posts
            if str(kwargs.get('source_key', '')).endswith(':fx:settlement'):
                settlement_posts += 1
                if settlement_posts == 2:
                    raise RuntimeError('injected second coupon settlement failure')
            return original(cls, conn, **kwargs)

        with patch.object(AccountingJournal, 'post', classmethod(fail_second)):
            with self.assertRaisesRegex(RuntimeError, 'second coupon'):
                self.bonds.pay_due_coupons('us-user', now=NOW)
        self.assertEqual(settlement_posts, 2)
        self.assertEqual(self._cash_minor(), before_cash)
        self.assertEqual(self._fx_rows(), [])
        for table in tables:
            self.assertEqual(self.db.currency.execute_one(f'SELECT COUNT(*) FROM {table}')[0], baseline[table])
        self.assertEqual(self.db.tax.execute_one('SELECT COUNT(*) FROM tax_transactions')[0], 0)
        cursors = self.db.accounts.execute(
            "SELECT last_coupon_epoch FROM treasury_bond_holdings ORDER BY product_code")
        self.assertEqual([int(row[0]) for row in cursors], [NOW - 180 * DAY] * 2)

        self.assertEqual(len(self.bonds.pay_due_coupons('us-user', now=NOW)), 2)
        paid_cash = self._cash_minor()
        self.assertGreater(paid_cash, before_cash)
        self.assertEqual(len(self._fx_rows()), 2)
        self.assertEqual(self._bond_check()['status'], 'matched')
        self.assertEqual(self.bonds.pay_due_coupons('us-user', now=NOW), [])
        self.assertEqual(self._cash_minor(), paid_cash)
        self.assertEqual(len(self._fx_rows()), 2)
        self.assertEqual(self._bond_check()['status'], 'matched')
