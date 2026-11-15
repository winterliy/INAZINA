"""A legacy account insert receives its currency before wallet bookkeeping."""
import unittest

import test_inazina as fixtures


class AccountCurrencyDefaultTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def test_funded_account_without_currency_records_wallet_entry(self):
        self.seed()
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO accounts(account_number,user_id,balance,principal,account_type) "
                         "VALUES ('DEFAULT-CURRENCY','citizen',100,100,'saving')")
            account = conn.execute("SELECT quote_currency FROM accounts WHERE account_number='DEFAULT-CURRENCY'").fetchone()
            ledger = conn.execute("SELECT currency_code FROM customer_wallet_ledger WHERE source_account='DEFAULT-CURRENCY'").fetchone()
        self.assertEqual(account[0], 'NWX')
        self.assertEqual(ledger[0], 'NWX')
