import unittest

import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from managers.currency_manager import CurrencyManager, SCALE


class CurrencyLiquidityTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_restart_preserves_baseline_with_large_existing_supply(self):
        self.db.users.execute("UPDATE users SET nationality='낙원',cash=5000000000000000 WHERE user_id='citizen'")
        before = dict(self.db.currency.execute_one("SELECT * FROM currency_liquidity_state WHERE currency_code='NWX'"))
        self.db.currency._seed_liquidity_states()
        after = dict(self.db.currency.execute_one("SELECT * FROM currency_liquidity_state WHERE currency_code='NWX'"))
        self.assertEqual(after, before)

    def test_seed_preserves_new_large_baseline_exactly(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('BIG','큰 통화','1')")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('큰나라','BIG')")
        self.db.users.execute("UPDATE users SET nationality='큰나라',cash=5000000000000000 WHERE user_id='citizen'")
        self.db.currency._seed_liquidity_states()
        row = self.db.currency.execute_one("SELECT * FROM currency_liquidity_state WHERE currency_code='BIG'")
        self.assertEqual(int(row['baseline_supply_units']), 5000000000000000000000)
        self.assertEqual(row['baseline_circulation_ratio'], 1)

    def test_hundred_quadrillion_supply_is_exact_and_neutral(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('BIG','큰 통화','1')")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('큰나라','BIG')")
        for index in range(200):
            self.db.users.execute("INSERT INTO users(user_id,nickname,nationality,cash) VALUES (?,?,'큰나라',5000000000000000)", (f'big{index}', f'big{index}'))
        self.db.currency._seed_liquidity_states()
        with self.db.atomic() as conn:
            row = conn.execute("SELECT baseline_supply_units FROM currency_liquidity_state WHERE currency_code='BIG'").fetchone()
            self.assertEqual(int(row[0]), 10**24)  # 100경 × 1,000,000
            self.assertEqual(CurrencyManager._liquidity_totals(conn, 'BIG'), (10**24, 10**24))
            self.assertEqual(CurrencyManager.liquidity_multiplier(conn, 'BIG'), 1)
            from managers.currency_manager import CurrencyReadSnapshot
            self.assertEqual(CurrencyReadSnapshot(conn)._liquidity_totals['BIG'], (10**24, 10**24))

    def test_runtime_baseline_creation_preserves_large_supply(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('BIG','큰 통화','1')")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('큰나라','BIG')")
        self.db.users.execute("UPDATE users SET nationality='큰나라',cash=5000000000000000 WHERE user_id='citizen'")
        with self.db.atomic() as conn:
            CurrencyManager.ensure_liquidity_state(conn, 'BIG')
            row = conn.execute("SELECT * FROM currency_liquidity_state WHERE currency_code='BIG'").fetchone()
            self.assertEqual(int(row['baseline_supply_units']), 5 * 10**21)
            self.assertEqual(CurrencyManager.liquidity_multiplier(conn, 'BIG'), 1)

    def test_baseline_migration_preserves_existing_state_and_is_idempotent(self):
        before = dict(self.db.currency.execute_one("SELECT * FROM currency_liquidity_state WHERE currency_code='NWX'"))
        with self.db.atomic() as conn:
            conn.execute("CREATE TABLE liquidity_old (currency_code TEXT PRIMARY KEY REFERENCES currencies(code), "
                         "baseline_supply_units INTEGER NOT NULL DEFAULT 0 CHECK(baseline_supply_units>=0), "
                         "baseline_circulation_ratio REAL,issuance_elasticity REAL,circulation_elasticity REAL, "
                         "min_multiplier REAL,max_multiplier REAL,updated_epoch REAL)")
            conn.execute("INSERT INTO liquidity_old SELECT * FROM currency_liquidity_state")
            conn.execute("DROP TABLE currency_liquidity_state")
            conn.execute("ALTER TABLE liquidity_old RENAME TO currency_liquidity_state")
        self.db.currency._upgrade_liquidity_baseline_storage()
        self.db.currency._upgrade_liquidity_baseline_storage()
        after = dict(self.db.currency.execute_one("SELECT * FROM currency_liquidity_state WHERE currency_code='NWX'"))
        self.assertEqual(after, before)
        self.assertIsInstance(after['baseline_supply_units'], int)

    def _currency(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate) VALUES ('LQD','유동화폐','100')"
        )
        # Foreign balances are now held in explicit currency-saving accounts.
        self.db.accounts.execute("INSERT INTO saving(product_code,name,product_kind,quote_currency,interest_rate,issuer) VALUES ('LQD_SAVE','유동화폐 통장','saving','LQD',0,'Bank')")
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
