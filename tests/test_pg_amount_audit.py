"""PostgreSQL has no snapshot copy of money; the audit checks NUMERIC minor-unit columns directly."""
import unittest
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from database import pg_backend
from functions.money import audit_amount_snapshots
from tests import test_market_features as fixtures
from tools.migrate_postgres_amount_registry import migrate


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

    def test_inventory_migration_is_idempotent_and_preserves_postgres_entries(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "inventory.db"
            with closing(sqlite3.connect(path)) as source, source:
                source.execute("CREATE TABLE financial_amount_field_registry "
                               "(source_table TEXT,source_key_field TEXT,amount_field TEXT,currency_expression TEXT)")
                source.execute("CREATE TABLE financial_amount_field_exemptions "
                               "(source_table TEXT,amount_field TEXT,reason TEXT)")
                source.execute("INSERT INTO financial_amount_field_registry VALUES ('audit_source','id','amount','NEW.currency')")
                source.execute("INSERT INTO financial_amount_field_exemptions VALUES ('audit_source','value','not money')")
            self.db.currency.execute("INSERT INTO financial_amount_field_exemptions "
                                     "(source_table,amount_field,reason) VALUES ('pg_only','value','existing')")
            self.assertEqual(migrate(path, self.db.currency.db_path), (1, 1))
            self.assertEqual(migrate(path, self.db.currency.db_path), (1, 1))
            rows = self.db.currency.execute("SELECT source_table,reason FROM financial_amount_field_exemptions "
                                            "WHERE source_table IN ('audit_source','pg_only') ORDER BY source_table")
            self.assertEqual([tuple(row) for row in rows], [("audit_source", "not money"), ("pg_only", "existing")])
