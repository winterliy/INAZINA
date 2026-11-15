"""A real SQL literal keeps fractional results in wide arithmetic."""
import unittest

import test_inazina as fixtures


class RealDivisionTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_integral_valued_real_divisor_keeps_fraction(self):
        with self.db.readonly() as conn:
            result = conn.execute('SELECT WIDE_DIV(12500000,1000000.0)').fetchone()[0]
        self.assertEqual(result, 12.5)

    def test_real_factor_keeps_fraction_through_multiplication(self):
        with self.db.readonly() as conn:
            result = conn.execute('SELECT WIDE_DIV(WIDE_MUL(1030,1.0),100)').fetchone()[0]
        self.assertEqual(result, 10.3)
