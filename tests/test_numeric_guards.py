"""NaN, infinity and overflow never reach a numeric cell."""
import sqlite3
import unittest

import test_inazina as fixtures
from database.numeric_guards import MONEY


class NumericGuardTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_nan_infinity_and_overflow_are_refused(self):
        for value in (float('nan'), float('inf'), float('-inf'), MONEY * 2):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                self.db.users.execute("UPDATE users SET cash=? WHERE user_id='citizen'", (value,))
        self.assertEqual(self.db.users.get_user('citizen')['cash'], 1000000)
        self.db.assets.register_asset('NUM', '숫자', 'EX', 100, 'stock')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.assets.execute("UPDATE market_assets SET price=? WHERE code='NUM'", (float('nan'),))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.assets.execute("INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) VALUES ('citizen','NUM',?,1)",
                                   (float('inf'),))
        with self.assertRaises((sqlite3.IntegrityError, OverflowError)):
            self.db.accounts.execute("UPDATE accounts SET balance_units=? WHERE 1", (2**64,))

    def test_ordinary_values_still_pass(self):
        self.db.users.execute("UPDATE users SET cash=? WHERE user_id='citizen'", (MONEY,))
        self.assertEqual(self.db.users.get_user('citizen')['cash'], MONEY)


if __name__ == "__main__":
    unittest.main()
