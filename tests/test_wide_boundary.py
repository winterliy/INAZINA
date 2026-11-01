"""Small, boundary and huge operands mix without a float detour."""
import itertools
import operator
import sqlite3
import unittest
from decimal import Decimal

from database import wide_values as wv
from database.unified import UnifiedConnection

SAFE = 2 ** 53 - 1
VALUES = [0, 1, -37242237308, SAFE, SAFE + 1, 10 ** 18 + 1, -(10 ** 18 + 1), 10 ** 24 - 1,
          '~M~' + str(10 ** 18 + 7), '~R~' + str(10 ** 20 + 3), '~M~-' + str(10 ** 19 + 5)]


class WideBoundaryTests(unittest.TestCase):
    def test_binary_operations_match_decimal_for_every_pair(self):
        for operation in (operator.add, operator.sub, operator.mul):
            for a, b in itertools.product(VALUES, VALUES):
                with self.subTest(op=operation.__name__, a=str(a), b=str(b)):
                    expected = operation(wv.number(a), wv.number(b))
                    self.assertEqual(wv.number(wv.binary(a, b, operation)), expected)

    def test_sum_matches_decimal_in_any_order(self):
        for rows in (VALUES, list(reversed(VALUES)), VALUES[2:] + VALUES[:2]):
            total = wv.Sum()
            for value in rows:
                total.step(value)
            self.assertEqual(wv.number(total.finalize()), sum(wv.number(v) for v in rows))

    def test_sql_update_crossing_the_safe_integer_boundary_is_exact(self):
        connection = sqlite3.connect(':memory:', factory=UnifiedConnection)
        connection.wide_money = True
        wv.register(connection)
        connection.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, amount MONEYREAL TEXT)')
        for start in (-37242237308, SAFE - 5, SAFE + 5, 0):
            connection.execute('DELETE FROM t')
            connection.execute('INSERT INTO t VALUES (1, ?)', (start,))
            for delta in (10 ** 18 + 1, -(10 ** 18 + 1), 7, 10 ** 23):
                connection.execute('UPDATE t SET amount = amount + ? WHERE id = 1', (delta,))
                start += delta
                value = connection.execute('SELECT amount FROM t').fetchone()[0]
                self.assertEqual(wv.number(value), start, (start, delta))


if __name__ == "__main__":
    unittest.main()
