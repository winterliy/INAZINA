import unittest

import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from managers.currency_manager import CurrencyManager, SCALE


class CurrencyLiquidityTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def _currency(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate) VALUES ('LQD','유동화폐','100')"
        )
        # Foreign balances are now held in explicit currency-saving accounts.
        self.db.accounts.execute("INSERT INTO saving(product_code,name,product_kind,quote_currency,interest_rate,issuer) VALUES ('LQD_SAVE','유동화폐 통장','currency_saving','LQD',0,'Bank')")
        self.bot.currency_mgr.open_account('citizen', 'LQD_SAVE')
        self.bot.currency_mgr.open_account(TREASURY_USER_ID, 'LQD_SAVE')

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
