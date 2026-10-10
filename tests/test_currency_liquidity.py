import unittest

import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from managers.currency_manager import CurrencyManager, SCALE


class CurrencyLiquidityTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def _currency(self):
        self.db.fx.execute(
            "INSERT INTO currencies(code,name,reference_rate) VALUES ('LQD','유동화폐','100')"
        )

    def test_supply_expansion_weakens_quote_from_first_issuance_baseline(self):
        self._currency()
        with self.db.atomic() as conn:
            CurrencyManager.change(conn, 'citizen', 'LQD', 100 * SCALE)
            neutral = float(CurrencyManager.rate(conn, 'LQD'))
            CurrencyManager.change(conn, 'citizen', 'LQD', 300 * SCALE)
            weakened = float(CurrencyManager.rate(conn, 'LQD'))
        self.assertEqual(neutral, 100.0)
        self.assertLess(weakened, neutral)
        self.assertAlmostEqual(weakened, 50.0)

    def test_treasury_reserves_reduce_circulation_multiplier(self):
        self._currency()
        with self.db.atomic() as conn:
            CurrencyManager.change(conn, 'citizen', 'LQD', 100 * SCALE)
            CurrencyManager.change(conn, 'citizen', 'LQD', -50 * SCALE)
            CurrencyManager.change(conn, TREASURY_USER_ID, 'LQD', 50 * SCALE)
            rate = float(CurrencyManager.rate(conn, 'LQD'))
        self.assertLess(rate, 100.0)
        self.assertAlmostEqual(rate, 100 * (0.5 ** 0.2), places=8)

    def test_snapshot_uses_the_same_liquidity_quote_as_settlement(self):
        self._currency()
        with self.db.atomic() as conn:
            CurrencyManager.change(conn, 'citizen', 'LQD', 100 * SCALE)
            CurrencyManager.change(conn, 'citizen', 'LQD', 100 * SCALE)
            direct = CurrencyManager.rate_fraction(conn, 'LQD')
            from managers.currency_manager import CurrencyReadSnapshot
            snap = CurrencyReadSnapshot(conn)
            self.assertEqual(snap.rate_fraction('LQD'), direct)


if __name__ == '__main__':
    unittest.main()
