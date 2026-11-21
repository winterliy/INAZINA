"""M4: the insolvency policy's exempt cash is a policy setting (explicitly exempt), so the amount-field audit has nothing unclassified."""
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from functions.money import audit_amount_field_coverage
from managers import insolvency_manager


class InsolvencyExemptAmountTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def test_the_policy_table_leaves_no_unclassified_amount_field(self):
        with self.db.atomic() as conn:
            insolvency_manager.ensure_tables(conn)       # the policy table is created on first use
            audit = audit_amount_field_coverage(conn)
        unclassified = [row for row in audit["unclassified"] if row.get("source_table") == "insolvency_policies"]
        self.assertEqual(unclassified, [])

    def test_the_exemption_says_why(self):
        with self.db.readonly() as conn:
            row = conn.execute("SELECT reason FROM financial_amount_field_exemptions "
                               "WHERE source_table='insolvency_policies' AND amount_field='exempt_amount'").fetchone()
        self.assertIsNotNone(row)
        self.assertIn("정책", row["reason"])

    def test_a_new_money_like_policy_column_is_still_reported(self):
        # The exemption is for exempt_amount only; any other money-shaped column of the table must still be classified.
        with self.db.atomic() as conn:
            insolvency_manager.ensure_tables(conn)
            conn.execute("ALTER TABLE insolvency_policies ADD COLUMN surprise_cash REAL")
            audit = audit_amount_field_coverage(conn)
        self.assertIn(("insolvency_policies", "surprise_cash"), {(row.get("source_table"), row.get("amount_field")) for row in audit["unclassified"]})


if __name__ == "__main__":
    unittest.main()
