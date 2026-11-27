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

    def test_withdrawal_fee_record_failure_rolls_the_withdrawal_back(self):
        from fractions import Fraction
        from managers.currency_exchange_manager import CurrencyExchangeManager
        from managers.payout_manager import PayoutManager
        self._close_position_without_an_nwx_account()
        payout = self._pending()[0]
        opened = payout["created_epoch"] + 10
        with self.db.atomic() as conn:
            PayoutManager.open_temp(conn, self.USER, payout["payout_id"], now=opened)
        self.db.products.execute(
            "INSERT OR IGNORE INTO saving(product_code,name,interest_rate,quote_currency,product_kind,status) "
            "VALUES ('FX_NWX','NWX 외환통장',0,'NWX','saving','active')")
        number = CurrencyExchangeManager(self.db).open_account(self.USER, "FX_NWX")
        fee = PayoutManager.fee_minor_units(int(payout["amount"]))
        real = CurrencyManager.receive

        def receive(conn, user_id, currency, minor_units):
            if int(minor_units) == fee and str(user_id) != self.USER:
                return {"currency": "USD", "decimal_places": 2, "minor_units": 1, "rate": Fraction(1, 1500)}
            return real(conn, user_id, currency, minor_units)

        def action(conn, manager):
            manager.withdraw(conn, self.USER, payout["payout_id"], now=opened + 200)
        before = (self.snapshot(), self._nwx_units(number))
        with patch.object(CurrencyManager, "receive", receive):
            with patch.object(CurrencyManager, "record_fx_settlement", side_effect=RuntimeError("fx record failure")):
                with self.assertRaises(RuntimeError):
                    with self.db.atomic() as conn:
                        action(conn, PayoutManager)
            self.assertEqual((self.snapshot(), self._nwx_units(number)), before)
            with self.db.atomic() as conn:
                action(conn, PayoutManager)
        self.assertEqual(self.snapshot()[0], before[0][0] + 1)


class TaxRollbackTests(unittest.TestCase):
    """Tax assessed in the citizen's own currency but withheld in the settlement currency keeps an exchange record;
    when that record fails, the closing trade is undone entirely and the retry books it once."""
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue
    issue_future = fixtures.MarketFeatureTests.issue_future
    _foreign_future_trader = fixtures.MarketFeatureTests._foreign_future_trader
    _nwx_units = fixtures.MarketFeatureTests._nwx_units

    def state(self, number):
        return (self._nwx_units(number),
                self.db.assets.execute_one("SELECT COUNT(*) FROM futures_positions WHERE user_id='foreign-future'")[0],
                self.db.accounts.execute_one("SELECT COUNT(*) FROM tax_transactions WHERE user_id='foreign-future'")[0],
                self.db.currency.execute_one("SELECT COUNT(*) FROM cash_fx_settlements WHERE kind LIKE 'tax:%'")[0])

    def test_futures_close_tax_exchange_record_failure_rolls_the_close_back(self):
        number = self._foreign_future_trader(5000)
        self.assertTrue(self.book.place_limit_order('foreign-future', 'FUT', 'buy', 2, 100)[0])
        self.db.companies.update_share_price("CO", 110)
        self.futures.settle_positions()
        before = self.state(number)
        self.assertEqual(before[1:], (1, 0, 0))
        with patch.object(CurrencyManager, "record_fx_settlement", side_effect=RuntimeError("fx record failure")):
            try:
                result = self.book.place_limit_order('foreign-future', 'FUT', 'sell', 2, 110)
                self.assertFalse(result[0], result)
            except RuntimeError:
                pass
        self.assertEqual(self.state(number), before)
        self.assertTrue(self.book.place_limit_order('foreign-future', 'FUT', 'sell', 2, 110)[0])
        after = self.state(number)
        self.assertEqual(after[1:], (0, 1, 1))
        self.assertGreater(after[0], before[0])


class ProductSettlementRollbackTests(unittest.TestCase):
    """Product settlements are all-or-nothing: when the ledger posting or link fails the whole settlement is undone and the retry books it once."""
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue
    issue_future = fixtures.MarketFeatureTests.issue_future
    _foreign_future_trader = fixtures.MarketFeatureTests._foreign_future_trader
    _nwx_units = fixtures.MarketFeatureTests._nwx_units

    def test_option_exercise_posting_failure_rolls_the_exercise_back(self):
        self.db.assets.add_user_asset('alice', 'CO', 2, unit_cost=100)
        self.assertTrue(self.options.create_and_write('alice', 'CO', 'call', 100, 1, 1, 10)[0])
        code = self.options.get_active_contracts()[0]['contract_code']
        self.assertTrue(self.options.place_limit_order('bob', code, 'buy', 1, 10)[0])
        self.db.companies.update_share_price('CO', 120)
        self.db.assets.execute('UPDATE option_contracts SET expiration_epoch=? WHERE contract_code=?', (int(time.time()) - 1, code))

        def state():
            return (self.db.assets.execute_one("SELECT COUNT(*) FROM option_exercise_events")[0],
                    self.db.assets.execute_one("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE asset_code='CO'")[0],
                    self.db.users.get_user('bob')['cash'], self.db.users.get_user('alice')['cash'],
                    self.db.assets.execute_one("SELECT status FROM option_contracts WHERE contract_code=?", (code,))[0])
        before = state()
        with patch.object(AccountingJournal, 'post_option_exercise', side_effect=RuntimeError('posting failure')):
            try:
                self.options.expire_matured()
            except RuntimeError:
                pass
        self.assertEqual(state(), before)
        self.assertTrue(self.options.expire_matured())
        self.assertEqual(state()[0], before[0] + 1)
        self.assertEqual(self.options.expire_matured(), [])
        self.assertEqual(state()[0], before[0] + 1)

    def test_futures_closing_fill_link_failure_rolls_the_fill_back(self):
        number = self._foreign_future_trader(5000)
        self.assertTrue(self.book.place_limit_order('foreign-future', 'FUT', 'buy', 2, 100)[0])
        self.db.companies.update_share_price("CO", 110)
        self.futures.settle_positions()

        def state():
            return (self._nwx_units(number),
                    self.db.assets.execute_one("SELECT COUNT(*) FROM futures_positions WHERE user_id='foreign-future'")[0],
                    self.db.assets.execute_one("SELECT COUNT(*) FROM business_cash_events WHERE kind='fill'")[0])
        before = state()
        real = AccountingJournal.link_futures_cash

        def failing(conn, tag, *args, **kwargs):
            if tag == 'fill':
                raise RuntimeError('link failure')
            return real(conn, tag, *args, **kwargs)
        with patch.object(AccountingJournal, 'link_futures_cash', staticmethod(failing)):
            try:
                result = self.book.place_limit_order('foreign-future', 'FUT', 'sell', 2, 110)
                self.assertFalse(result[0], result)
            except RuntimeError:
                pass
        self.assertEqual(state(), before)
        self.assertTrue(self.book.place_limit_order('foreign-future', 'FUT', 'sell', 2, 110)[0])
        self.assertEqual(state()[1], 0)
