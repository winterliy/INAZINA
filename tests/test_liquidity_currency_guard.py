import unittest
import sqlite3
from contextlib import closing
from unittest import mock

from managers.liquidity_manager import FIRMS, LiquidityManager
from tests import test_market_features as fixtures


class LiquidityCurrencyGuardTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def test_foreign_holding_blocks_new_provider_and_existing_provider_cancels_quotes(self):
        manager = LiquidityManager(self.db, self.book)
        manager.provision_firms('admin', capital=50_000)
        code = FIRMS[0][1]
        uid = 'company:' + code
        with mock.patch('random.random', return_value=1):
            manager.run_cycle(now=10000)
        with self.db.readonly() as conn:
            self.assertGreater(conn.execute("SELECT count(*) FROM market_orders WHERE user_id=? AND status IN ('open','partial')", (uid,)).fetchone()[0], 0)
        self.db.assets.add_user_asset(uid, 'CO', 1, unit_cost=100)
        self.db.companies.execute("UPDATE company SET quote_currency='USD' WHERE company_code='CO'")
        with self.assertRaisesRegex(ValueError, '통화'):
            manager.add_provider(code, 'low')
        manager.run_cycle(now=10001)  # even before the normal next quote cycle
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM market_orders WHERE user_id=? AND status IN ('open','partial')", (uid,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT halted FROM liquidity_providers WHERE company_code=?', (code,)).fetchone()[0], 1)
        row = next(row for row in manager.providers() if row['companyCode'] == code)
        self.assertIsNone(row['equity'])
        self.assertIsNone(row['pnl'])
        self.assertTrue(row['currencyError'])

    def test_non_base_cash_is_not_added_to_base_equity(self):
        manager = LiquidityManager(self.db, self.book)
        manager.provision_firms('admin', capital=50_000)
        uid = 'company:' + FIRMS[0][1]
        with self.db.atomic() as conn:
            conn.execute("INSERT OR IGNORE INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1',2)")
            conn.execute("INSERT OR REPLACE INTO country_currencies(nationality,currency_code) VALUES ('Foreign','USD')")
            conn.execute("UPDATE users SET nationality='Foreign' WHERE user_id=?", (uid,))
            with self.assertRaisesRegex(ValueError, '통화'):
                manager._standing(conn, uid)

    def test_targets_exclude_unknown_currency(self):
        manager = LiquidityManager(self.db, self.book)
        with closing(sqlite3.connect(':memory:')) as conn, mock.patch('managers.liquidity_manager.base_currency_code', return_value='NWX'):
            conn.row_factory = sqlite3.Row
            conn.executescript("CREATE TABLE company(company_code TEXT,share_price INTEGER,status TEXT,trading_status TEXT,exchange_id TEXT,quote_currency TEXT,Fractal INTEGER);"
                               "CREATE TABLE liquidity_providers(company_code TEXT);"
                               "INSERT INTO company VALUES ('NULL',100,'active','active','X',NULL,0),"
                               "('EMPTY',100,'active','active','X','',0),('BASE',100,'active','active','X','NWX',0);")
            self.assertEqual([code for code, _ in manager._targets(conn, 'OTHER', 100)], ['BASE'])


if __name__ == '__main__':
    unittest.main()
