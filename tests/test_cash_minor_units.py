"""Wallet cash is an exact integer of the home currency's minor unit (at most 2 decimals)."""
import unittest

import test_inazina as fixtures
from managers.currency_manager import CurrencyManager, SCALE


class CashMinorUnitTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1',2)")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','USD')")
        self.db.users.execute("UPDATE users SET cash=0 WHERE user_id='citizen'")  # citizen is 미국

    def row(self):
        return self.db.users.execute_one("SELECT cash,cash_minor FROM users WHERE user_id='citizen'")

    def test_many_cent_changes_stay_exact(self):
        self.prepare()
        with self.db.atomic() as conn:
            for _ in range(1000):
                CurrencyManager.change(conn, 'citizen', 'USD', 10_000)  # 0.01 USD
            for _ in range(3):
                CurrencyManager.change(conn, 'citizen', 'USD', 100_000)  # 0.10 USD
            self.assertEqual(CurrencyManager.balance(conn, 'citizen', 'USD'), 10_300_000)
        self.assertEqual(self.row()['cash_minor'], 1030)
        self.assertEqual(self.row()['cash'], 10.3)

    def test_legacy_float_writes_are_snapped_to_cents(self):
        self.prepare()
        for _ in range(10):
            self.db.users.execute("UPDATE users SET cash=cash+0.1 WHERE user_id='citizen'")
        self.assertEqual(self.row()['cash_minor'], 100)
        self.assertEqual(self.row()['cash'], 1)  # 0.1 x 10 without binary drift
        self.db.users.execute("UPDATE users SET cash=cash+0.004 WHERE user_id='citizen'")
        self.assertEqual(self.row()['cash_minor'], 100)  # below one cent is rounded away

    def test_places_are_limited_to_two(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('BBB','B','1')")
        self.assertEqual(self.db.currency.execute_one("SELECT decimal_places FROM currencies WHERE code='BBB'")[0], 2)
        with self.assertRaises(Exception):
            self.db.currency.execute("UPDATE currencies SET decimal_places=3 WHERE code='BBB'")

    def test_nwx_cash_stays_an_integer(self):
        self.db.users.execute("UPDATE users SET nationality='낙원' WHERE user_id='citizen'")
        with self.db.atomic() as conn:
            CurrencyManager.change(conn, 'citizen', 'NWX', 5 * SCALE)
        row = self.row()
        self.assertEqual((row['cash'], row['cash_minor']), (1000005, 1000005))
        self.assertIsInstance(row['cash'], int)


if __name__ == "__main__":
    unittest.main()
