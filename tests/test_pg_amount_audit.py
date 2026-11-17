"""PostgreSQL has no snapshot copy of money; the audit checks NUMERIC minor-unit columns directly."""
import unittest

from database import pg_backend
from functions.money import audit_amount_snapshots
from tests import test_market_features as fixtures


@unittest.skipUnless(pg_backend.enabled(), "PostgreSQL-specific audit")
class PostgresAmountAuditTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def test_clean_database_has_no_findings(self):
        with self.db.readonly() as conn:
            self.assertEqual(audit_amount_snapshots(conn), [])

    def test_fractional_minor_units_are_reported(self):
        with self.db.atomic() as conn:
            conn.execute("CREATE TABLE audit_probe (id INTEGER PRIMARY KEY, amount_minor_units NUMERIC)")
            conn.execute("INSERT INTO audit_probe VALUES (1, 100), (2, 100.5), (3, NULL)")
        with self.db.readonly() as conn:
            findings = audit_amount_snapshots(conn)
        self.assertEqual([(f["source_table"], f["amount_field"], f["status"], f["rows"]) for f in findings],
                         [("audit_probe", "amount_minor_units", "fractional_minor_units", 1)])
