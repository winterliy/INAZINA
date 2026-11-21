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
