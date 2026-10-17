import unittest
from decimal import Decimal

from functions.money import from_minor_units, to_minor_units


class MoneyMinorUnitTests(unittest.TestCase):
    def test_currency_scales_round_trip_without_float(self):
        cases = (("123.45", 2, 12345), ("123", 0, 123), ("-0.005", 2, -1))
        for amount, places, expected in cases:
            with self.subTest(amount=amount, places=places):
                units = to_minor_units(Decimal(amount), places)
                self.assertEqual(units, expected)
                self.assertEqual(from_minor_units(units, places),
                                 Decimal(expected).scaleb(-places))

    def test_rejects_non_finite_and_invalid_scales(self):
        for amount in ("NaN", "Infinity"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                to_minor_units(amount, 2)
        for places in (-1, 7):
            with self.subTest(places=places), self.assertRaises(ValueError):
                to_minor_units("1", places)
        with self.assertRaises(ValueError):
            from_minor_units(1.5, 2)


if __name__ == "__main__":
    unittest.main()
