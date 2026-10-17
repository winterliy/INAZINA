"""Foreign-currency accounts behave like savings: interest, opening, closing and storage."""
import unittest

import test_inazina as fixtures
from managers.interest_manager import InterestManager
from managers.currency_manager import SCALE, CurrencyManager
from managers.bank_manager import BankManager

DAY = 86400


class FxInterestTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def open_usd(self, rate):
        self.seed()
        number = self.bot.currency_mgr.open_account('citizen', 'INZCU001')
        self.bot.currency_mgr.exchange('citizen', 'NWX', 'USD', '15000')
        with self.db.atomic() as conn:
            conn.execute("UPDATE saving SET interest_rate=?,interest_cycle=86400,compound_type=0,maturity_days=0 WHERE product_code='INZCU001'", (rate,))
            conn.execute("UPDATE accounts SET contract_rate=?,rate_type='fixed' WHERE account_number=?", (rate, number))
        return number

    def account(self, number):
        with self.db.atomic() as conn:
            return dict(conn.execute("SELECT * FROM accounts WHERE account_number=?", (number,)).fetchone())

    def test_migration_settles_old_interest_once_and_fills_product_defaults(self):
        number = self.open_usd(10)
        start = self.account(number)
        with self.db.atomic() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (InterestManager.FX_INTEREST_MIGRATION,))
            conn.execute("UPDATE saving SET interest_cycle=0,compound_type=NULL WHERE product_code='INZCU001'")
            conn.execute("UPDATE accounts SET last_payment_epoch=? WHERE account_number=?", (start['last_payment_epoch'], number))
            now = start['last_payment_epoch'] + 30 * DAY
            self.assertTrue(InterestManager.migrate_currency_accounts(conn, now))
            self.assertFalse(InterestManager.migrate_currency_accounts(conn, now + DAY))
            product = conn.execute("SELECT interest_cycle,compound_type FROM saving WHERE product_code='INZCU001'").fetchone()
        after = self.account(number)
        expected = start['balance_units'] * 0.10 * 30 / 365
        self.assertAlmostEqual(after['balance_units'] - start['balance_units'], expected, delta=SCALE / 100)
        self.assertEqual(after['last_payment_epoch'], now)
        self.assertEqual(after['interest_remainder'], '0')
        self.assertEqual((product['interest_cycle'], product['compound_type']), (86400, 0))

    def test_interest_is_paid_per_complete_cycle_with_tax(self):
        number = self.open_usd(36.5)
        start = self.account(number)
        with self.db.atomic() as conn:
            self.bot.currency_mgr._settle(conn, 'citizen', start['last_payment_epoch'] + 1.5 * DAY)
            tax = InterestManager._tax_rate(conn, 'citizen')
        after = self.account(number)
        gross = start['balance_units'] / SCALE * 0.365 / 365
        net = gross - gross * tax / 100
        self.assertAlmostEqual((after['balance_units'] - start['balance_units']) / SCALE, net, delta=0.011)
        # Only one whole day is paid; the half day waits for the next cycle.
        self.assertEqual(after['last_payment_epoch'], start['last_payment_epoch'] + DAY)

    def test_savings_processing_pays_fx_accounts_instead_of_skipping_them(self):
        number = self.open_usd(36.5)
        start = self.account(number)
        InterestManager(self.db).process_all_interests(now=start['last_payment_epoch'] + 2 * DAY, user_id='citizen')
        after = self.account(number)
        self.assertGreater(after['balance_units'], start['balance_units'])
        self.assertEqual(after['last_payment_epoch'], start['last_payment_epoch'] + 2 * DAY)
        # balance mirrors balance_units for every account.
        self.assertAlmostEqual(after['balance'], after['balance_units'] / SCALE, places=2)

    def test_foreign_savings_withholding_remits_tax_payable_through_fx_clearing(self):
        from managers.bank_journal import BankJournal

        number = self.open_usd(36.5)
        self.bot.currency_mgr.exchange('citizen', 'NWX', 'USD', '450000')
        start = self.account(number)
        InterestManager(self.db).process_user_interests(
            'citizen', now=start['last_payment_epoch'] + DAY
        )
        issuer = self.db.products.execute_one(
            "SELECT issuer FROM saving WHERE product_code='INZCU001'"
        )[0]
        with self.db.atomic() as conn:
            usd_trial = BankJournal.trial_balance(conn, issuer, 'USD')
            nwx_trial = BankJournal.trial_balance(conn, issuer, 'NWX')
            remittances = conn.execute(
                "SELECT COUNT(*) FROM bank_journal_entries WHERE issuer=? "
                "AND event_type='tax_remittance'", (issuer,),
            ).fetchone()[0]
        usd_accounts = {row['account_code']: row['balance_units'] for row in usd_trial}
        nwx_accounts = {row['account_code']: row['balance_units'] for row in nwx_trial}
        self.assertGreater(usd_accounts.get('interest_expense', 0), 0)
        self.assertEqual(usd_accounts.get('tax_payable', 0), 0)
        self.assertLess(usd_accounts.get('fx_conversion_clearing', 0), 0)
        self.assertGreater(nwx_accounts.get('fx_conversion_clearing', 0), 0)
        self.assertEqual(remittances, 2)

    def test_fx_opening_needs_password_and_minimum_deposit_paid_from_home_cash(self):
        self.seed()
        with self.db.atomic() as conn:
            conn.execute("UPDATE saving SET deposit_amount=10 WHERE product_code='INZCU001'")
            usd_cost = CurrencyManager.quote(conn, 'USD', 'NWX')
        bank = BankManager(self.db)
        self.assertFalse(bank.open_account('citizen', 'INZCU001', '12', '10')[0])
        ok, message = bank.open_account('citizen', 'INZCU001', 'pass1234', '5')
        self.assertFalse(ok)
        self.assertIn('최소 예치금', message)
        cash = self.db.users.get_user('citizen')['cash']
        ok, message = bank.open_account('citizen', 'INZCU001', 'pass1234', '12.5')
        self.assertTrue(ok, message)
        account = self.db.accounts.execute_one("SELECT * FROM accounts WHERE user_id='citizen' AND account_type='currency_saving'")
        self.assertEqual(account['balance_units'], 12_500_000)
        self.assertAlmostEqual(account['balance'], 12.5)
        self.assertIsNotNone(account['password'])
        paid = cash - self.db.users.get_user('citizen')['cash']
        self.assertGreaterEqual(paid, float(usd_cost * 12.5))
        self.assertLess(paid, float(usd_cost * 12.5) + 1)

    def test_fx_closing_checks_password_and_returns_balance_to_home_cash(self):
        self.seed()
        bank = BankManager(self.db)
        self.assertTrue(bank.open_account('citizen', 'INZCU001', 'pass1234', '10')[0])
        number = self.db.accounts.execute_one("SELECT account_number FROM accounts WHERE user_id='citizen' AND account_type='currency_saving'")[0]
        cash = self.db.users.get_user('citizen')['cash']
        self.assertFalse(bank.close_account('citizen', number, None, 'wrong')[0])
        ok, message = bank.close_account('citizen', number, None, 'pass1234')
        self.assertTrue(ok, message)
        self.assertIsNone(self.db.accounts.execute_one("SELECT 1 FROM accounts WHERE account_number=?", (number,)))
        self.assertGreater(self.db.users.get_user('citizen')['cash'], cash)

    def test_savings_balances_live_in_balance_units(self):
        self.seed()
        bank = BankManager(self.db)
        self.assertTrue(bank.open_account('citizen', 'INZSV001', 'pass1234', 15000)[0])
        account = self.db.accounts.execute_one("SELECT * FROM accounts WHERE user_id='citizen' AND account_type='saving'")
        self.assertEqual(account['balance_units'], 15000 * SCALE)
        self.assertEqual(account['balance'], 15000)
        with self.assertRaisesRegex(Exception, 'balance_units'):
            self.db.accounts.execute("UPDATE accounts SET balance=99999 WHERE account_number=?", (account['account_number'],))
        self.db.accounts.execute("UPDATE accounts SET balance_units=? WHERE account_number=?", (16000 * SCALE, account['account_number']))
        self.assertEqual(self.db.accounts.execute_one("SELECT balance FROM accounts WHERE account_number=?", (account['account_number'],))[0], 16000)

    def test_product_command_takes_saving_settings_for_fx_accounts(self):
        from managers.admin_commands_manager import AdminCommandsManager
        self.seed()
        commands = AdminCommandsManager(self.bot)
        commands.execute('admin', '상품생성', 'FXOLD currencyproduct "옛 형식" USD 1')
        commands.execute('admin', '상품생성', 'FXNEW currencyproduct "새 형식" USD 2 100 30 3600 1 Inazina')
        rows = {row['product_code']: row for row in self.db.products.execute(
            "SELECT * FROM saving WHERE product_code IN ('FXOLD','FXNEW')")}
        self.assertEqual((rows['FXOLD']['interest_cycle'], rows['FXOLD']['compound_type']), (86400, 0))
        new = rows['FXNEW']
        self.assertEqual((new['product_kind'], new['quote_currency'], new['interest_rate'], new['deposit_amount'],
                          new['maturity_days'], new['interest_cycle'], new['compound_type']),
                         ('currency_saving', 'USD', 2, 100, 30, 3600, 1))

    def test_supply_counts_accounts_in_their_own_currency_exactly(self):
        from managers.currency_manager import CurrencyReadSnapshot
        self.seed()
        self.assertTrue(BankManager(self.db).open_account('citizen', 'INZSV001', 'pass1234', 15000)[0])
        self.assertTrue(BankManager(self.db).open_account('citizen', 'INZCU001', 'pass1234', '3.5')[0])
        with self.db.atomic() as conn:
            nwx_before = CurrencyManager._liquidity_totals(conn, 'NWX')[0]
            usd_before = CurrencyManager._liquidity_totals(conn, 'USD')[0]
            # Moving the owner to another nationality must not move their NWX savings.
            conn.execute("INSERT OR IGNORE INTO country_currencies(nationality,currency_code) VALUES ('달러국','USD')")
            conn.execute("UPDATE users SET cash=0,nationality='달러국' WHERE user_id='citizen'")
            nwx_cash = conn.execute("SELECT COALESCE(SUM(cash),0) FROM users u JOIN country_currencies n ON n.nationality=u.nationality WHERE n.currency_code='NWX'").fetchone()[0]
            nwx_after = CurrencyManager._liquidity_totals(conn, 'NWX')[0]
            usd_after = CurrencyManager._liquidity_totals(conn, 'USD')[0]
            snapshot = CurrencyReadSnapshot(conn).rows['NWX']
        self.assertEqual(nwx_after, int(nwx_cash * SCALE) + 15000 * SCALE)
        self.assertEqual(usd_after, usd_before)
        self.assertEqual(snapshot['foreign_total'], 15000 * SCALE)
        self.assertGreater(nwx_before, nwx_after)


if __name__ == "__main__":
    unittest.main()
