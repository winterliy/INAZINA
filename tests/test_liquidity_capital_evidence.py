import json
import unittest

from managers.liquidity_manager import FIRMS, LiquidityManager
from tests import test_market_features as fixtures


class LiquidityCapitalEvidenceTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def test_new_provider_records_basis_and_legacy_row_stays_unverified_after_restart(self):
        manager = LiquidityManager(self.db, self.book)
        manager.provision_firms('admin', capital=50_000)
        first, second = FIRMS[0][1], FIRMS[1][1]
        with self.db.atomic() as conn:
            evidence = conn.execute('SELECT * FROM liquidity_capital_evidence WHERE company_code=?', (first,)).fetchone()
            self.assertEqual((evidence['currency_code'], evidence['capital_minor_units']), ('NWX', 50000))
            self.assertEqual(json.loads(evidence['components_json'])['cash_minor_units'], 50000)
            conn.execute('DELETE FROM liquidity_capital_evidence WHERE company_code=?', (second,))
        from database.currency_db import ensure_liquidity_providers
        ensure_liquidity_providers(self.db.currency.execute)
        with self.db.readonly() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM liquidity_capital_evidence WHERE company_code=?', (second,)).fetchone())


if __name__ == '__main__':
    unittest.main()
