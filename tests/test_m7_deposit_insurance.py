"""M7: deposit insurance coverage, assessed premium and resolution allocation preview (read-only), and its admin API."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from managers.deposit_insurance_fund import preview
from web_api import server
import test_alm_manager


class Fixture(unittest.TestCase):
    setUp = test_alm_manager.ALMManagerTests.setUp
    tearDown = test_alm_manager.ALMManagerTests.tearDown


class DepositInsuranceTests(Fixture):
    def preview(self, **kw):
        values = dict(premium_bps=50, proceeds_minor_units=0, general_claims_minor_units=0) | kw
        with self.db.readonly() as conn:
            return preview(conn, "Bank", **values)

    # the fixture: u1 500 insured, u2 100 uninsured, company 400 insured, limit 300
    def test_coverage_is_per_depositor_within_the_limit(self):
        result = self.preview()
        self.assertEqual((result["coveredMinorUnits"], result["uninsuredMinorUnits"], result["totalDepositsMinorUnits"]), (600, 400, 1000))
        self.assertEqual((result["depositorCount"], result["accountCount"]), (3, 3))

    def test_premium_is_assessed_on_covered_deposits_and_nothing_is_posted(self):
        before = self.db.accounts.execute_one("SELECT COUNT(*) FROM bank_journal_entries")[0]
        self.assertEqual(self.preview(premium_bps=250)["assessedPremiumMinorUnits"], 15)
        self.assertEqual(self.db.accounts.execute_one("SELECT COUNT(*) FROM bank_journal_entries")[0], before)

    def test_allocation_follows_priority_and_conserves_proceeds(self):
        result = self.preview(proceeds_minor_units=800, general_claims_minor_units=300)
        self.assertEqual(result["allocations"], {"insurer_subrogation": 600, "uninsured_depositors": 200, "general_creditors": 0})
        self.assertEqual(sum(result["allocations"].values()) + result["unallocatedMinorUnits"], 800)
        self.assertEqual(result["shortfalls"]["uninsured_depositors"], 200)

    def test_bad_inputs_and_unknown_bank_are_rejected(self):
        for bad in (dict(premium_bps=-1), dict(premium_bps=10001), dict(proceeds_minor_units=True), dict(general_claims_minor_units=1.5)):
            with self.assertRaises(ValueError):
                self.preview(**bad)
        with self.db.readonly() as conn, self.assertRaises(ValueError):
            preview(conn, "Nope", premium_bps=1, proceeds_minor_units=0, general_claims_minor_units=0)


class DepositInsuranceApiTests(Fixture):
    def get(self, query, role="admin"):
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "a"},
                                  path="/internal/admin/deposit-insurance?" + query, rfile=io.BytesIO(b""),
                                  send_json=lambda status, value: response.update(status=status, value=value))
        db = self.db
        with patch.object(server, "INTERNAL_TOKEN", "secret"), patch.object(server, "identity", return_value={"userId": "a", "role": role}), \
             patch.object(server, "audit_admin_access", audit), patch.object(server, "connect_readonly", lambda: db.readonly()), \
             patch.object(server, "LegacyNames", lambda c: c), patch("functions.banks.bank_issuer", lambda c, bank_id: "Bank"):
            server.Handler.do_GET(handler)
        return response, audit

    def test_admin_reads_and_the_read_is_audited(self):
        response, audit = self.get("bank_id=BNK1&premium_bps=100&proceeds=500&general_claims=0")
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(response["value"]["coveredMinorUnits"], 600)
        self.assertEqual(audit.call_args[0][1], "deposit_insurance.preview")

    def test_non_admin_and_bad_filters_are_refused_without_audit(self):
        response, audit = self.get("bank_id=BNK1", role="user")
        self.assertEqual(response["status"], 403)
        for query in ("", "bank_id=BNK1&premium_bps=x", "bank_id=BNK1&premium_bps=10001", "bank_id=BNK1&proceeds=-1", "bank_id=BNK1&general_claims=1e3"):
            response, audit = self.get(query)
            self.assertEqual(response["status"], 400, query)
            audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
