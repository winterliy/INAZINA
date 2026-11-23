"""Business flows followed by the close gate: reruns add nothing, a failure in the middle leaves nothing behind."""
import time
import unittest
from unittest.mock import patch

from managers.accounting_journal import AccountingJournal
from managers.currency_manager import CurrencyManager
from managers.fx_revaluation import FxRevaluation
from tests import test_market_features as fixtures


class CloseIntegrationTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue

    def prepare_coupon(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('SCD','남방','1/120',2)")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code,tax_percent) VALUES ('United Southerns','SCD',10)")
        self.db.users.create_user('scd', '남방인', 'United Southerns', 0)
        self.issue("CB-INT", "corporate_bond", rate=36.5)
        self.db.assets.add_user_asset('scd', "CB-INT", 10, unit_cost=100)
        now = int(time.time())
        with self.db.atomic() as conn:
            conn.execute("UPDATE investment SET coupon_frequency_days=5,last_coupon_epoch=?,next_coupon_epoch=? "
                         "WHERE product_code='CB-INT'", (now - 5 * 86400, now))
        return now

    def counts(self):
        with self.db.readonly() as conn:
            return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("cash_fx_settlements", "accounting_entries", "fx_revaluation_runs", "fx_revaluation_corrections")]

    def test_coupon_then_revaluation_then_reruns_are_idempotent_and_the_gate_has_no_fx_or_payout_finding(self):
        now = self.prepare_coupon()
        self.products.pay_corporate_bond_coupons(now=now)
        with self.db.atomic() as conn:
            FxRevaluation.run(conn, "admin", now)
        after_first = self.counts()
        self.products.pay_corporate_bond_coupons(now=now)
        with self.db.atomic() as conn:
            self.assertEqual(FxRevaluation.run(conn, "admin", now + 1), [])
        self.assertEqual(self.counts(), after_first)
        with self.db.readonly() as conn:
            report = AccountingJournal.reconcile_all_financial_ledgers(conn)
        bad = [f for f in report["findings"] if f.get("scope") in ("fx_revaluation", "payout_requests", "treasury_bond_cash")]
        self.assertEqual(bad, [])

    def test_a_failed_fx_record_rolls_the_whole_coupon_back_and_a_retry_pays_once(self):
        now = self.prepare_coupon()
        before = self.counts()
        cash_before = int(self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id='scd'")[0])
        with patch.object(CurrencyManager, "record_fx_settlement", side_effect=RuntimeError("fx record failure")):
            try:
                self.products.pay_corporate_bond_coupons(now=now)
            except RuntimeError:
                pass
        self.assertEqual(int(self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id='scd'")[0]), cash_before)
        self.assertEqual(self.counts(), before)
        self.products.pay_corporate_bond_coupons(now=now)
        self.assertEqual(self.counts()[0], before[0] + 1)


class PayoutRollbackTests(unittest.TestCase):
    """Rollback matrix: a failing exchange record leaves every payout path exactly as it was, and a retry succeeds once."""
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue
    issue_future = fixtures.MarketFeatureTests.issue_future
    _foreign_future_trader = fixtures.MarketFeatureTests._foreign_future_trader
    _nwx_units = fixtures.MarketFeatureTests._nwx_units
    USER = "foreign-future"
    from tests.test_payouts import PayoutChoiceTests as _P
    _close_position_without_an_nwx_account = _P._close_position_without_an_nwx_account
    _pending = _P._pending

    def snapshot(self):
        with self.db.readonly() as conn:
            return (conn.execute("SELECT COUNT(*) FROM cash_fx_settlements").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM business_cash_events").fetchone()[0],
                    [tuple(r) for r in conn.execute("SELECT payout_id,status,decided_epoch FROM payout_requests ORDER BY payout_id")],
                    int(conn.execute("SELECT SUM(cash_minor) FROM users").fetchone()[0]))

    def fail_then_retry(self, action):
        from managers.payout_manager import PayoutManager
        before = self.snapshot()
        with patch.object(CurrencyManager, "record_fx_settlement", side_effect=RuntimeError("fx record failure")):
            with self.assertRaises(RuntimeError):
                with self.db.atomic() as conn:
                    action(conn, PayoutManager)
        self.assertEqual(self.snapshot(), before)
        with self.db.atomic() as conn:
            action(conn, PayoutManager)
        self.assertNotEqual(self.snapshot(), before)
        self.assertEqual(self.snapshot()[0], before[0] + 1)

    def test_convert_rolls_back_whole(self):
        self._close_position_without_an_nwx_account()
        payout = self._pending()[0]
        self.fail_then_retry(lambda conn, manager: manager.convert(conn, self.USER, payout["payout_id"], now=payout["created_epoch"] + 10))

    def test_expiry_return_rolls_back_whole(self):
        from managers.payout_manager import DAY, HOLD_DAYS
        self._close_position_without_an_nwx_account()
        payout = self._pending()[0]
        late = payout["created_epoch"] + HOLD_DAYS * DAY + 1
        with self.db.atomic() as conn:       # the payer is paid back in its own currency; make it a foreign home so an exchange happens
            conn.execute("UPDATE payout_requests SET payer_id=? WHERE payout_id=?", (self.USER, payout["payout_id"]))
            conn.execute("UPDATE payout_requests SET user_id='alice' WHERE payout_id=?", (payout["payout_id"],))
        self.fail_then_retry(lambda conn, manager: manager.expire_due(conn, late))
