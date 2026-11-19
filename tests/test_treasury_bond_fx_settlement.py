"""Treasury-bond cash paid to a holder whose home currency differs from the bond's."""
import sqlite3
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.accounting_journal import AccountingJournal
from managers.currency_exchange_manager import CurrencyExchangeManager
from managers.currency_manager import CurrencyManager
from managers.treasury_bond_manager import TreasuryBondManager

NOW = 2_000_000_000
DAY = 86400


class _BondFxFixture:
    """A USD-home holder, an NWX account product and helpers; holds no tests itself."""

    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1500',2)")
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','USD')")
        self.db.currency.execute(
            "INSERT INTO country_tax_rules(nationality,income_source,tax_percent,updated_by,updated_epoch) "
            "VALUES ('미국','treasury_bond_interest',20,'test',1)")
        self.db.users.create_user('us-user', '미국인', '미국', 1000)
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind,status) "
            "VALUES ('FX_NWX','NWX 외환통장',0,'NWX','saving','active')")
        self.bonds = TreasuryBondManager(self.db)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def _bond(self, code, principal, *, coupon_days=0, matured=True):
        self.db.products.execute(
            "INSERT INTO treasury_bond_products(product_code,name,annual_rate,maturity_days,face_value,"
            "exchange_id,market_price,total_supply,coupon_frequency_days) VALUES (?,?,10,365,?,'BOND',?,?,?)",
            (code, code, principal, principal, principal, coupon_days))
        if coupon_days:
            self.db.accounts.execute(
                "INSERT INTO treasury_bond_holdings(user_id,product_code,product_name,annual_rate,"
                "maturity_days,principal,face_value,acquisition_cost,purchased_epoch,maturity_epoch,"
                "coupon_frequency_days,day_count_convention,last_coupon_epoch) "
                "VALUES ('us-user',?,?,10,365,?,?,?,?,?,?,'ACT/365',?)",
                (code, code, principal, principal, principal, NOW - coupon_days * DAY,
                 NOW + 100 * DAY, coupon_days, NOW - coupon_days * DAY))
        else:
            self.db.accounts.execute(
                "INSERT INTO treasury_bond_holdings(user_id,product_code,product_name,annual_rate,"
                "maturity_days,principal,face_value,acquisition_cost,purchased_epoch,maturity_epoch) "
                "VALUES ('us-user',?,?,10,365,?,?,?,?,?)",
                (code, code, principal, principal, principal, NOW - 365 * DAY,
                 NOW - 1 if matured else NOW + DAY))

    def _cash_minor(self):
        return int(self.db.users.execute_one(
            "SELECT cash_minor FROM users WHERE user_id='us-user'")[0])

    def _fx_rows(self):
        return [dict(row) for row in self.db.currency.execute(
            "SELECT * FROM treasury_bond_cash_fx_settlements ORDER BY event_key")]

    def _bond_check(self):
        with self.db.readonly() as conn:
            return AccountingJournal.reconcile_treasury_bond_cash_events(conn)


class TreasuryBondFxSettlementTests(_BondFxFixture, unittest.TestCase):
    def test_base_currency_redemption_is_converted_into_the_holders_home_currency(self):
        self._bond('NWX-MATURE', 150_000)
        before = self._cash_minor()
        redeemed = self.bonds.redeem_matured('us-user', now=NOW)
        self.assertEqual(len(redeemed), 1)
        # 150,000 principal + 15,000 interest - 3,000 tax, all in the bond currency.
        self.assertEqual((redeemed[0]['principal'], redeemed[0]['interest'],
                          redeemed[0]['tax'], redeemed[0]['net']), (150_000, 15_000, 3_000, 162_000))
        fx = self._fx_rows()
        self.assertEqual(len(fx), 1, fx)
        row = fx[0]
        self.assertEqual((row['source_currency'], row['source_decimal_places'], row['source_minor_units'],
                          row['settlement_currency'], row['settlement_decimal_places']),
                         ('NWX', 0, 162_000, 'USD', 2))
        rate = Fraction(int(row['rate_numerator']), int(row['rate_denominator']))
        expected = int(Fraction(162_000) * rate * 100)
        self.assertEqual(row['settlement_minor_units'], expected)
        # Roughly 162,000 NWX / 1,500 = 108 USD: never the same number in another currency.
        self.assertTrue(10_000 < expected < 11_600, expected)
        self.assertEqual(self._cash_minor() - before, expected)
        link = self.db.currency.execute_one(
            "SELECT l.currency_code,l.decimal_places,l.delta_minor_units,l.user_id "
            "FROM treasury_bond_cash_wallet_links w JOIN customer_wallet_ledger l "
            "ON l.entry_id=w.wallet_entry_id WHERE w.event_key=?", (row['event_key'],))
        self.assertEqual(tuple(link), ('USD', 2, expected, 'us-user'))
        check = self._bond_check()
        self.assertEqual(check['status'], 'matched', check)
        self.assertEqual(check['fx_settlement_count'], 1)
        tax = self.db.tax.execute_one(
            "SELECT tax_amount_minor_units,currency_code FROM tax_transactions "
            "WHERE user_id='us-user' AND source='treasury_bond_interest'")
        self.assertEqual(tuple(tax), (3_000, 'NWX'))
        with self.db.readonly() as conn:
            report = AccountingJournal.reconcile_all_financial_ledgers(conn)
            self.assertEqual(AccountingJournal.audit_unbalanced(conn, 'treasury_bond'), [])
        self.assertEqual([f for f in report['findings'] if f['scope'] == 'common_journals'], [], report['findings'])
        # The tax went to the holder's treasury converted as well, not as 3,000 USD.
        treasury = self.db.currency.execute_one(
            "SELECT u.cash_minor FROM country_treasuries t JOIN users.users u ON u.user_id=t.user_id "
            "WHERE t.nationality='미국'")
        self.assertTrue(0 < int(treasury[0]) < 300, tuple(treasury))

    def test_fx_clearing_entries_balance_in_each_currency(self):
        self._bond('NWX-MATURE', 150_000)
        self.bonds.redeem_matured('us-user', now=NOW)
        row = self._fx_rows()[0]
        entries = {r['source_key']: r for r in self.db.currency.execute(
            "SELECT * FROM accounting_entries WHERE ledger_scope='treasury_bond' ORDER BY entry_id")}
        self.assertEqual(set(entries), {row['event_key'], f"{row['event_key']}:fx:source",
                                        f"{row['event_key']}:fx:settlement"})

        def lines(key):
            return {r['account_code']: (r['debit_units'], r['credit_units']) for r in self.db.currency.execute(
                "SELECT * FROM accounting_lines WHERE entry_id=?", (entries[key]['entry_id'],))}
        source, settlement = entries[f"{row['event_key']}:fx:source"], entries[f"{row['event_key']}:fx:settlement"]
        self.assertEqual((source['currency_code'], source['decimal_places']), ('NWX', 0))
        self.assertEqual(lines(source['source_key']), {
            'customer_cash_payable': (162_000, 0), 'fx_conversion_clearing': (0, 162_000)})
        self.assertEqual((settlement['currency_code'], settlement['decimal_places']), ('USD', 2))
        self.assertEqual(lines(settlement['source_key']), {
            'fx_conversion_clearing': (row['settlement_minor_units'], 0),
            'customer_cash_payable': (0, row['settlement_minor_units'])})

    def test_fx_clearing_keeps_one_open_position_per_currency_explained_by_the_snapshots(self):
        """Policy: no cross-currency netting. Each currency's clearing balance is the
        simulator's own FX position and must equal the sum of the recorded conversions."""
        self._bond('NWX-MATURE', 150_000)
        self._bond('NWX-SECOND', 30_000)
        self.bonds.redeem_matured('us-user', now=NOW)
        fx = self._fx_rows()
        self.assertEqual(len(fx), 2)
        settled = {row['event_key']: row['settlement_minor_units'] for row in fx}
        with self.db.readonly() as conn:
            for row in fx:
                holding = row['event_key'].split(':')[1]
                nwx = {r['account_code']: r for r in AccountingJournal.trial_balance(
                    conn, ledger_scope='treasury_bond', entity_id=holding, currency_code='NWX')}
                usd = {r['account_code']: r for r in AccountingJournal.trial_balance(
                    conn, ledger_scope='treasury_bond', entity_id=holding, currency_code='USD')}
                net = row['source_minor_units']
                # Bond currency: the payable is fully moved into clearing, which stays credited.
                self.assertEqual(nwx['customer_cash_payable']['balance_units'], 0)
                self.assertEqual(nwx['fx_conversion_clearing']['balance_units'], -net)
                self.assertEqual(sum(r['balance_units'] for r in nwx.values()), 0)
                # Holder currency: clearing stays debited by exactly what the wallet received.
                self.assertEqual(usd['fx_conversion_clearing']['balance_units'], settled[row['event_key']])
                self.assertEqual(usd['customer_cash_payable']['balance_units'], -settled[row['event_key']])
                self.assertEqual(sum(r['balance_units'] for r in usd.values()), 0)
            check = AccountingJournal.reconcile_treasury_bond_cash_events(conn)
        self.assertEqual(check['status'], 'matched', check)
        self.assertEqual(check['fx_clearing_positions'], [
            {'currency_code': 'NWX', 'decimal_places': 0, 'debit_units': 0,
             'credit_units': sum(row['source_minor_units'] for row in fx),
             'balance_units': -sum(row['source_minor_units'] for row in fx)},
            {'currency_code': 'USD', 'decimal_places': 2, 'debit_units': sum(settled.values()),
             'credit_units': 0, 'balance_units': sum(settled.values())}])

    def test_clearing_balance_not_explained_by_a_snapshot_is_reported(self):
        self._bond('NWX-MATURE', 150_000)
        self.bonds.redeem_matured('us-user', now=NOW)
        key = self._fx_rows()[0]['event_key']
        with self.db.atomic() as conn:
            # Same holding and an fx-looking key, so the orphan-entry check alone would pass.
            entry = conn.execute(
                "SELECT entry_id FROM accounting_entries WHERE source_key=?", (f"{key}:fx:source",)).fetchone()[0]
            conn.execute("INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units) "
                         "VALUES (?,'customer_cash_payable',5,0),(?,'fx_conversion_clearing',0,5)", (entry, entry))
            check = AccountingJournal.reconcile_treasury_bond_cash_events(conn)
            close = AccountingJournal.reconcile_all_financial_ledgers(conn)
        self.assertEqual(check['status'], 'mismatch')
        self.assertTrue(any(gap['status'] == 'fx_clearing_position_mismatch' and gap['currency_code'] == 'NWX'
                            for gap in check['common_journal_gaps']), check)
        self.assertEqual(close['status'], 'mismatch')
        self.assertFalse(close['close_ready'])

    def test_holder_with_an_account_in_the_bond_currency_is_paid_without_conversion(self):
        number = CurrencyExchangeManager(self.db).open_account('us-user', 'FX_NWX')
        self._bond('NWX-MATURE', 150_000)
        before = self._cash_minor()
        self.bonds.redeem_matured('us-user', now=NOW)
        self.assertEqual(self._cash_minor(), before)
        account = self.db.accounts.execute_one(
            "SELECT balance_minor_units,balance_decimal_places FROM accounts WHERE account_number=?", (number,))
        self.assertEqual(tuple(account), (162_000, 0))
        self.assertEqual(self._fx_rows(), [])
        self.assertEqual(self._bond_check()['status'], 'matched')

    def test_coupon_below_one_home_minor_unit_records_a_zero_settlement_without_a_wallet_row(self):
        self._bond('NWX-COUPON', 100, coupon_days=180)
        before = self._cash_minor()
        paid = self.bonds.pay_due_coupons('us-user', now=NOW)
        self.assertEqual(len(paid), 1)
        self.assertEqual(self._cash_minor(), before)
        fx = self._fx_rows()
        self.assertEqual(len(fx), 1, fx)
        self.assertGreater(fx[0]['source_minor_units'], 0)
        self.assertEqual(fx[0]['settlement_minor_units'], 0)
        self.assertEqual(self.db.currency.execute_one(
            "SELECT COUNT(*) FROM treasury_bond_cash_wallet_links")[0], 0)
        self.assertEqual(self.db.currency.execute_one(
            "SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='treasury_bond' "
            "AND source_key LIKE '%:fx:settlement'")[0], 0)
        check = self._bond_check()
        self.assertEqual(check['status'], 'matched', check)
        # A tax too small for the receiving treasury's currency is not withheld at all.
        self.assertEqual(paid[0]['tax'], 0)

    def test_rerun_does_not_pay_or_convert_twice(self):
        self._bond('NWX-MATURE', 150_000)
        self.bonds.redeem_matured('us-user', now=NOW)
        after = self._cash_minor()
        self.assertEqual(self.bonds.redeem_matured('us-user', now=NOW + 5), [])
        self.assertEqual(self._cash_minor(), after)
        self.assertEqual(len(self._fx_rows()), 1)
        self.assertEqual(self._bond_check()['status'], 'matched')

    def test_failed_fx_posting_rolls_back_the_whole_redemption(self):
        self._bond('NWX-MATURE', 150_000)
        before = self._cash_minor()
        original = AccountingJournal.post.__func__

        def failing(cls, conn, **kwargs):
            if str(kwargs.get('source_key', '')).endswith(':fx:settlement'):
                raise RuntimeError('injected fx posting failure')
            return original(cls, conn, **kwargs)
        with patch.object(AccountingJournal, 'post', classmethod(failing)):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                self.bonds.redeem_matured('us-user', now=NOW)
        self.assertEqual(self._cash_minor(), before)
        self.assertEqual(self._fx_rows(), [])
        for table in ('treasury_bond_cash_events', 'treasury_bond_cash_wallet_links'):
            self.assertEqual(self.db.currency.execute_one(f"SELECT COUNT(*) FROM {table}")[0], 0)
        self.assertEqual(self.db.tax.execute_one("SELECT COUNT(*) FROM tax_transactions")[0], 0)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT status FROM treasury_bond_holdings WHERE product_code='NWX-MATURE'")[0], 'active')
        # The same call then succeeds once the failure is gone.
        self.assertEqual(len(self.bonds.redeem_matured('us-user', now=NOW)), 1)
        self.assertEqual(self._bond_check()['status'], 'matched')

    def test_reconciliation_reports_a_tampered_or_missing_fx_settlement(self):
        self._bond('NWX-MATURE', 150_000)
        self.bonds.redeem_matured('us-user', now=NOW)
        with self.db.atomic() as conn:
            conn.execute("DROP TRIGGER trg_treasury_bond_cash_fx_settlements_update")
            conn.execute("UPDATE treasury_bond_cash_fx_settlements SET settlement_minor_units=settlement_minor_units+1")
            check = AccountingJournal.reconcile_treasury_bond_cash_events(conn)
            self.assertEqual(check['status'], 'mismatch')
            self.assertTrue(any(gap['status'] == 'fx_settlement_mismatch'
                                for gap in check['common_journal_gaps']), check)
            conn.execute("DROP TRIGGER trg_treasury_bond_cash_fx_settlements_delete")
            conn.execute("DELETE FROM treasury_bond_cash_fx_settlements")
            check = AccountingJournal.reconcile_treasury_bond_cash_events(conn)
            self.assertTrue(any(gap['status'] == 'wallet_settlement_amount_or_currency_mismatch'
                                for gap in check['common_journal_gaps']), check)

    def test_fx_settlement_rows_are_append_only(self):
        self._bond('NWX-MATURE', 150_000)
        self.bonds.redeem_matured('us-user', now=NOW)
        for statement in ("UPDATE treasury_bond_cash_fx_settlements SET settlement_minor_units=1",
                          "DELETE FROM treasury_bond_cash_fx_settlements"):
            with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                self.db.currency.execute(statement)

    def test_home_currency_holder_is_unchanged(self):
        self.db.users.create_user('nwx-user', '시민', '낙원', 0)
        self.db.products.execute(
            "INSERT INTO treasury_bond_products(product_code,name,annual_rate,maturity_days,face_value,"
            "exchange_id,market_price,total_supply) VALUES ('HOME','HOME',10,365,1000,'BOND',1000,1000)")
        self.db.accounts.execute(
            "INSERT INTO treasury_bond_holdings(user_id,product_code,product_name,annual_rate,maturity_days,"
            "principal,face_value,acquisition_cost,purchased_epoch,maturity_epoch) "
            "VALUES ('nwx-user','HOME','HOME',10,365,1000,1000,1000,?,?)", (NOW - 365 * DAY, NOW - 1))
        redeemed = self.bonds.redeem_matured('nwx-user', now=NOW)
        self.assertEqual(len(redeemed), 1)
        with self.db.readonly() as conn:
            self.assertEqual(CurrencyManager.home(conn, 'nwx-user'), 'NWX')
        self.assertEqual(self.db.users.get_user('nwx-user')['cash'], redeemed[0]['net'])
        self.assertEqual(self._fx_rows(), [])
        self.assertEqual(self._bond_check()['status'], 'matched')


class TreasuryBondPurchaseCurrencyTests(unittest.TestCase):
    """A bond is paid for in its own currency, mirroring how it pays out."""

    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','달러','1500',2),('EUR','유로','1600',2)")
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','USD')")
        self.db.users.create_user('us-user', '미국인', '미국', 1000)
        self.db.users.create_user('nwx-user', '시민', '낙원', 1000)
        for code in ('NWX', 'EUR'):
            self.db.products.execute(
                "INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind,status) "
                "VALUES (?,?,0,?,'saving','active')", (f'FX_{code}', f'{code} 외환통장', code))
        from managers.user_manager import UserManager
        self.users = UserManager(self.db)
        self.bonds = self.users.bond_manager
        ok, message = self.bonds.issue('GOV', '국채', 10, 365, 100, 10_000)
        self.assertTrue(ok, message)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def _cash_minor(self, user='us-user'):
        return int(self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id=?", (user,))[0])

    def _holdings(self, user='us-user'):
        return [int(row[0]) for row in self.db.accounts.execute(
            "SELECT principal FROM treasury_bond_holdings WHERE user_id=?", (user,))]

    def _open(self, product, units):
        number = CurrencyExchangeManager(self.db).open_account('us-user', product)
        with self.db.atomic() as conn:
            CurrencyManager.change(conn, 'us-user', product[3:], units * 1_000_000)
        return number

    def _balance(self, number):
        return int(self.db.accounts.execute_one(
            "SELECT balance_minor_units FROM accounts WHERE account_number=?", (number,))[0])

    def test_direct_purchase_never_debits_home_cash_for_a_bond_in_another_currency(self):
        before = self._cash_minor()
        ok, message = self.bonds.buy('us-user', 'GOV', 500)
        self.assertFalse(ok, message)
        self.assertIn('NWX', message)
        self.assertEqual(self._cash_minor(), before)
        self.assertEqual(self._holdings(), [])
        self.assertEqual(int(self.db.products.execute_one(
            "SELECT sold_amount FROM treasury_bond_products WHERE product_code='GOV'")[0]), 0)

    def test_direct_purchase_debits_the_account_in_the_bond_currency(self):
        nwx = self._open('FX_NWX', 800)
        before = self._cash_minor()
        ok, message = self.bonds.buy('us-user', 'GOV', 500)
        self.assertTrue(ok, message)
        self.assertEqual(self._balance(nwx), 300)
        self.assertEqual(self._cash_minor(), before)
        self.assertEqual(self._holdings(), [500])

    def test_direct_purchase_ignores_a_larger_balance_in_a_third_currency(self):
        eur = self._open('FX_EUR', 900)
        before = self._cash_minor()
        ok, message = self.bonds.buy('us-user', 'GOV', 500)
        self.assertFalse(ok, message)
        self.assertEqual(self._balance(eur), 90_000)
        self.assertEqual(self._cash_minor(), before)
        nwx = self._open('FX_NWX', 400)
        ok, message = self.bonds.buy('us-user', 'GOV', 500)
        self.assertFalse(ok, message)  # 400 NWX is short; EUR and USD are not a substitute
        self.assertEqual((self._balance(nwx), self._balance(eur), self._cash_minor()), (400, 90_000, before))

    def test_direct_purchase_in_the_holders_own_currency_still_uses_cash(self):
        ok, message = self.bonds.buy('nwx-user', 'GOV', 500)
        self.assertTrue(ok, message)
        self.assertEqual(self._cash_minor('nwx-user'), 500)
        self.assertEqual(self._holdings('nwx-user'), [500])

    def test_order_book_does_not_settle_a_foreign_currency_bond_from_home_cash(self):
        from managers.order_book_manager import OrderBookManager
        book = OrderBookManager(self.db, self.users)
        before = self._cash_minor()
        ok, message = book.place_limit_order('us-user', 'GOV', 'buy', 2, 100)
        self.assertFalse(ok, message)
        self.assertEqual(self._cash_minor(), before)
        self.assertEqual(self._holdings(), [])
        ok, message = book.place_limit_order('nwx-user', 'GOV', 'buy', 2, 100)
        self.assertTrue(ok, message)
        self.assertEqual(self._cash_minor('nwx-user'), 800)
        self.assertEqual(self._holdings('nwx-user'), [200])

    def test_order_book_settles_a_foreign_currency_bond_from_the_matching_account(self):
        from managers.order_book_manager import OrderBookManager
        book = OrderBookManager(self.db, self.users)
        nwx = self._open('FX_NWX', 800)
        before = self._cash_minor()
        ok, message = book.place_limit_order('us-user', 'GOV', 'buy', 2, 100)
        self.assertTrue(ok, message)
        self.assertEqual(self._balance(nwx), 600)           # 2 x 100 NWX from the NWX account
        self.assertEqual(self._cash_minor(), before)         # the USD wallet is never touched
        self.assertEqual(self._holdings(), [200])

    def test_order_book_sale_proceeds_go_to_the_account_in_the_bond_currency(self):
        from managers.order_book_manager import OrderBookManager
        book = OrderBookManager(self.db, self.users)
        nwx = self._open('FX_NWX', 800)
        self.assertTrue(book.place_limit_order('us-user', 'GOV', 'buy', 2, 100)[0])
        self.assertEqual((self._balance(nwx), self._holdings()), (600, [200]))
        ok, message = book.place_limit_order('us-user', 'GOV', 'sell', 2, 100)
        self.assertTrue(ok, message)
        before_wallet = self._cash_minor()
        ok, message = book.place_limit_order('nwx-user', 'GOV', 'buy', 2, 100)
        self.assertTrue(ok, message)
        self.assertEqual(self._cash_minor(), before_wallet)                  # never booked into the USD wallet
        self.assertEqual(self._holdings('nwx-user'), [200])
        self.assertEqual(self._balance(nwx), 800)                            # the 200 came back to the NWX account

    def test_purchase_and_payout_use_the_same_currency_for_an_account_holder(self):
        nwx = self._open('FX_NWX', 500)
        ok, message = self.bonds.buy('us-user', 'GOV', 500)
        self.assertTrue(ok, message)
        self.db.accounts.execute(
            "UPDATE treasury_bond_holdings SET purchased_epoch=?,maturity_epoch=? WHERE user_id='us-user'",
            (NOW - 365 * DAY, NOW - 1))
        before = self._cash_minor()
        redeemed = self.bonds.redeem_matured('us-user', now=NOW)
        self.assertEqual(len(redeemed), 1)
        self.assertEqual(self._balance(nwx), int(redeemed[0]['net']))
        self.assertEqual(self._cash_minor(), before)


class TreasuryBondWalletLinkTests(_BondFxFixture, unittest.TestCase):
    """The bond event must link the wallet row of its own payment, not merely the first credit."""

    def _link(self, event_key=None):
        row = self.db.currency.execute_one(
            "SELECT w.event_key,l.entry_id,l.user_id,l.currency_code,l.delta_minor_units "
            "FROM treasury_bond_cash_wallet_links w JOIN customer_wallet_ledger l "
            "ON l.entry_id=w.wallet_entry_id" + (" WHERE w.event_key=?" if event_key else ""),
            (event_key,) if event_key else ())
        return dict(row) if row else None

    def _post(self, conn, key, net, **extra):
        AccountingJournal.post_treasury_bond_cash_event(
            conn, event_key=key, event_type='coupon', holding_id=1, product_code='B',
            user_id='us-user', currency_code='USD', decimal_places=2, principal_minor_units=0,
            interest_minor_units=net, tax_minor_units=0, net_minor_units=net, recorded_epoch=NOW, **extra)

    def test_an_earlier_unrelated_credit_to_the_same_holder_is_not_linked(self):
        with self.db.atomic() as conn:
            mark = AccountingJournal.wallet_watermark(conn)
            conn.execute("UPDATE users SET cash_minor=cash_minor+7 WHERE user_id='us-user'")   # unrelated
            conn.execute("UPDATE users SET cash_minor=cash_minor+100 WHERE user_id='us-user'")  # the coupon
            self._post(conn, 'decoy-first', 100, wallet_watermark=mark)
        self.assertEqual(self._link('decoy-first')['delta_minor_units'], 100)
        self.assertEqual(self._bond_check()['status'], 'matched')

    def test_an_explicit_wallet_entry_must_be_the_payment_itself(self):
        with self.db.atomic() as conn:
            conn.execute("UPDATE users SET cash_minor=cash_minor+7 WHERE user_id='us-user'")
            wrong = AccountingJournal.wallet_watermark(conn)
            conn.execute("UPDATE users SET cash_minor=cash_minor+100 WHERE user_id='us-user'")
            right = AccountingJournal.wallet_watermark(conn)
            with self.assertRaisesRegex(RuntimeError, '지갑 입금'):
                self._post(conn, 'explicit', 100, wallet_entry_id=wrong)
            # The refusal left no event or journal entry behind in the open transaction.
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM treasury_bond_cash_events").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='treasury_bond'").fetchone()[0], 0)
            self._post(conn, 'explicit', 100, wallet_entry_id=right)
        self.assertEqual(self._link('explicit')['entry_id'], right)

    def test_two_indistinguishable_credits_are_refused_rather_than_guessed(self):
        with self.db.atomic() as conn:
            mark = AccountingJournal.wallet_watermark(conn)
            for _ in range(2):
                conn.execute("UPDATE users SET cash_minor=cash_minor+100 WHERE user_id='us-user'")
            with self.assertRaisesRegex(RuntimeError, '지갑 입금'):
                self._post(conn, 'ambiguous', 100, wallet_watermark=mark)
        self.assertIsNone(self._link())

    def test_a_missing_or_wrong_amount_credit_is_refused(self):
        with self.db.atomic() as conn:
            mark = AccountingJournal.wallet_watermark(conn)
            conn.execute("UPDATE users SET cash_minor=cash_minor+99 WHERE user_id='us-user'")
            with self.assertRaisesRegex(RuntimeError, '지갑 입금'):
                self._post(conn, 'short', 100, wallet_watermark=mark)

    def _treasury_holding(self, nationality, code='T-HELD'):
        with self.db.atomic() as conn:
            treasury = CurrencyManager.treasury_id(conn, nationality)
        self._bond(code, 150_000)
        self.db.accounts.execute(
            "UPDATE treasury_bond_holdings SET user_id=? WHERE product_code=?", (treasury, code))
        return treasury

    def test_holder_that_is_the_issuing_treasury_links_only_its_redemption_credit(self):
        treasury = self._treasury_holding('낙원')
        redeemed = self.bonds.redeem_matured(treasury, now=NOW)
        self.assertEqual(len(redeemed), 1)
        link = self._link()
        self.assertEqual((link['user_id'], link['currency_code'], link['delta_minor_units']),
                         (treasury, 'NWX', int(redeemed[0]['net'])))
        self.assertEqual(self._bond_check()['status'], 'matched', self._bond_check())

    def test_holder_that_is_the_tax_receiving_treasury_links_the_converted_payment_not_the_tax(self):
        treasury = self._treasury_holding('미국')
        redeemed = self.bonds.redeem_matured(treasury, now=NOW)
        self.assertEqual(len(redeemed), 1)
        fx = self._fx_rows()[0]
        link = self._link()
        self.assertEqual((link['user_id'], link['currency_code'], link['delta_minor_units']),
                         (treasury, 'USD', fx['settlement_minor_units']))
        credits = [tuple(row) for row in self.db.currency.execute(
            "SELECT entry_id,delta_minor_units FROM customer_wallet_ledger "
            "WHERE user_id=? AND delta_minor_units>0 ORDER BY entry_id", (treasury,))]
        if redeemed[0]['tax']:
            # The treasury's own tax receipt precedes the payment and must stay unlinked.
            self.assertGreater(len(credits), 1, credits)
            self.assertNotEqual(link['entry_id'], credits[0][0])
        self.assertEqual(self._bond_check()['status'], 'matched', self._bond_check())

    def test_converted_payment_and_tax_receipt_link_different_wallet_rows(self):
        self._bond('NWX-MATURE', 150_000)
        self.bonds.redeem_matured('us-user', now=NOW)
        bond_entry = self._link()['entry_id']
        tax_entries = {int(row[0]) for row in self.db.currency.execute(
            "SELECT wallet_entry_id FROM tax_transaction_wallet_links")}
        self.assertTrue(tax_entries)
        self.assertNotIn(bond_entry, tax_entries)


if __name__ == '__main__':
    unittest.main()
