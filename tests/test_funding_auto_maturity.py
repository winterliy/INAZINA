"""M6: automatic repayment of matured wholesale funding is opt-in per jurisdiction, off by default, audited and failure-isolated."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.alm_manager import ALMManager, PRUDENTIAL_DEFAULTS
from managers.funding_manager import FundingManager
from managers.funding_maturity import process_opted_in_funding

NOW = 2_000_000_000
DAY = 86400


class AutoMaturityTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        for issuer in ("A", "B"):
            self.db.currency.execute("INSERT INTO financial_institutions(issuer,nationality) VALUES (?,'낙원')", (issuer,))
        with self.db.atomic() as conn:
            self.ids = {}
            for issuer in ("A", "B"):
                FundingManager.set_limit(conn, issuer, "interbank", "NWX", 100000, actor="admin", now=NOW)
                self.ids[issuer] = FundingManager.draw(conn, issuer, "interbank", "NWX", 1000, reference_key=f"k-{issuer}",
                                                       rate_bps=3650, maturity_days=10, now=NOW)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def status(self, conn, issuer):
        return conn.execute("SELECT status FROM institution_funding WHERE funding_id=?", (self.ids[issuer],)).fetchone()[0]

    def test_it_is_off_by_default_and_repays_nothing(self):
        self.assertEqual(PRUDENTIAL_DEFAULTS["funding_auto_maturity_flag"], 0)
        with self.db.atomic() as conn:
            result = process_opted_in_funding(conn, now=NOW + 10 * DAY)
            self.assertEqual((result["skipped"], result["banks"]), (["A", "B"], []))
            self.assertEqual((self.status(conn, "A"), self.status(conn, "B")), ("active", "active"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admin_audit WHERE action='funding.auto_process_due'").fetchone()[0], 0)

    def test_opted_in_jurisdiction_repays_matured_funding_with_interest_and_an_audit_row(self):
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "funding_auto_maturity_flag", 1, actor="admin", now=NOW)
            early = process_opted_in_funding(conn, now=NOW + 9 * DAY)             # nothing is due yet
            self.assertEqual(early["banks"], [])
            result = process_opted_in_funding(conn, now=NOW + 10 * DAY)
            self.assertEqual({bank["issuer"] for bank in result["banks"]}, {"A", "B"})
            self.assertEqual((self.status(conn, "A"), self.status(conn, "B")), ("repaid", "repaid"))
            self.assertEqual(result["banks"][0]["repaid"][0]["interest"], Decimal(10))      # 1000 x 36.5% x 10/365
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admin_audit WHERE action='funding.auto_process_due' "
                                          "AND actor_id='system:funding_maturity'").fetchone()[0], 2)
            self.assertEqual(process_opted_in_funding(conn, now=NOW + 11 * DAY)["banks"], [])   # nothing runs twice

    def test_a_failing_bank_is_isolated_and_retried_later(self):
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "funding_auto_maturity_flag", 1, actor="admin", now=NOW)
            original = FundingManager.repay

            def repay(c, funding_id, *, now):
                result = original(c, funding_id, now=now)
                if funding_id == self.ids["A"]:
                    raise ValueError("simulated failure")
                return result
            with patch.object(FundingManager, "repay", side_effect=repay):
                result = process_opted_in_funding(conn, now=NOW + 10 * DAY)
            by_bank = {bank["issuer"]: bank for bank in result["banks"]}
            self.assertEqual([row["funding_id"] for row in by_bank["A"]["failed"]], [self.ids["A"]])
            self.assertEqual((self.status(conn, "A"), self.status(conn, "B")), ("active", "repaid"))
            self.assertEqual(len(process_opted_in_funding(conn, now=NOW + 10 * DAY)["banks"][0]["repaid"]), 1)
            self.assertEqual(self.status(conn, "A"), "repaid")

    def test_the_switch_accepts_only_zero_or_one(self):
        with self.db.atomic() as conn:
            for value in (2, -1, True, 1.0):
                with self.assertRaisesRegex(ValueError, "invalid_prudential_parameter_value"):
                    ALMManager.set_parameter(conn, "낙원", "funding_auto_maturity_flag", value, actor="admin", now=NOW)


if __name__ == "__main__":
    unittest.main()
