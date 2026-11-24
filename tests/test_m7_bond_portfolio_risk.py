"""M7: read-only treasury bond portfolio risk (sum of per-bond DV01, value-weighted durations) and its admin API."""
import io
import json
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_treasury_bond_fx_settlement as fixture
from managers.bond_portfolio_risk import portfolio_risk
from web_api import server


class Fixture(unittest.TestCase):
    setUp = fixture.TreasuryBondPurchaseCurrencyTests.setUp
    tearDown = fixture.TreasuryBondPurchaseCurrencyTests.tearDown

    def buy(self, user="nwx-user", amount=500):
        ok, message = self.bonds.buy(user, "GOV", amount)
        self.assertTrue(ok, message)

    def risk(self, holder="nwx-user"):
        with self.db.readonly() as conn:
            return portfolio_risk(conn, holder, now=time.time())


class PortfolioRiskTests(Fixture):
    def test_an_empty_portfolio_is_all_zero(self):
        result = self.risk()
        self.assertEqual((result["positionCount"], result["marketValue"], result["dv01"], result["modifiedDuration"]), (0, "0.000000", "0.000000", "0"))

    def test_totals_are_the_sum_of_the_positions(self):
        self.buy(amount=300)
        self.buy(amount=200)
        result = self.risk()
        self.assertGreaterEqual(result["positionCount"], 1)
        self.assertEqual(Decimal(result["dv01"]), sum((Decimal(p["dv01"]) for p in result["positions"]), Decimal(0)))
        self.assertEqual(Decimal(result["marketValue"]), sum((Decimal(p["marketValue"]) for p in result["positions"]), Decimal(0)))
        self.assertGreater(Decimal(result["dv01"]), 0)
        self.assertTrue(0 < Decimal(result["modifiedDuration"]) <= 1.01)          # a one-year bullet bond

    def test_another_holders_bonds_are_not_included_and_nothing_is_written(self):
        self.buy("nwx-user")
        before = self.db.accounts.execute_one("SELECT COUNT(*) FROM treasury_bond_holdings")[0]
        self.assertEqual(self.risk("us-user")["positionCount"], 0)
        self.assertEqual(self.db.accounts.execute_one("SELECT COUNT(*) FROM treasury_bond_holdings")[0], before)


class PortfolioRiskApiTests(Fixture):
    def get(self, query, role="admin"):
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "a"}, path="/internal/admin/bond-risk?" + query,
                                  rfile=io.BytesIO(b""), send_json=lambda status, value: response.update(status=status, value=value))
        db = self.db
        with patch.object(server, "INTERNAL_TOKEN", "secret"), patch.object(server, "identity", return_value={"userId": "a", "role": role}), \
             patch.object(server, "audit_admin_access", audit), patch.object(server, "connect_readonly", lambda: db.readonly()), patch.object(server, "LegacyNames", lambda c: c):
            server.Handler.do_GET(handler)
        return response, audit

    def test_admin_reads_and_it_is_audited(self):
        self.buy()
        response, audit = self.get("holder=nwx-user")
        self.assertEqual(response["status"], 200, response)
        self.assertGreater(Decimal(response["value"]["dv01"]), 0)
        self.assertEqual(audit.call_args[0][1], "bond_risk.read")

    def test_non_admin_and_bad_filters_are_refused_without_audit(self):
        self.assertEqual(self.get("holder=nwx-user", role="user")[0]["status"], 403)
        for query in ("", "holder=" + "x" * 200):
            response, audit = self.get(query)
            self.assertEqual(response["status"], 400, query)
            audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
