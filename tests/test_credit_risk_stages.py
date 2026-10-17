import unittest
import tempfile
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_journal import BankJournal
from managers.credit_risk_manager import CreditRiskManager


class CreditRiskStageTests(unittest.TestCase):
    def test_performing_and_significantly_deteriorated_loans(self):
        self.assertEqual(CreditRiskManager._stage("active", "current", 0, .5, False), 1)
        self.assertEqual(CreditRiskManager._stage("active", "overdue", 0, .5, True), 2)
        self.assertEqual(CreditRiskManager._stage("active", "current", 2, .5, False), 2)
        self.assertEqual(CreditRiskManager._stage("active", "current", 0, 1.0, False), 2)

    def test_defaulted_exposure_is_stage_three(self):
        self.assertEqual(CreditRiskManager._stage("defaulted", "defaulted", 0, 0, True), 3)
        self.assertEqual(CreditRiskManager._stage("active", "defaulted", 0, 0, True), 3)


class DefaultedLoanEclTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("ecl-borrower", "Borrower", "낙원", 0)
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('ECL3','ECL test',5,10000,365,86400,0,'Bank','NWX')"
        )
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
            "created_at,collection_status,quote_currency) "
            "VALUES ('ecl-borrower','ECL3',1000,1000,'defaulted',datetime('now'),'defaulted','NWX')"
        )
        self.db.currency.execute(
            "UPDATE credit_risk_scenarios SET pd_multiplier=0 WHERE nationality='낙원'"
        )

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def test_defaulted_loan_is_assessed_and_provisioned_as_stage_three(self):
        self.db.accounts.execute(
            "UPDATE loans SET contract_effective_rate=10 WHERE product_code='ECL3'"
        )
        with self.db.atomic() as conn:
            count = CreditRiskManager.assess_conn(conn, 1_800_000_000)
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, 1_800_000_000), 0)
            self.assertEqual(BankJournal.audit_unbalanced(conn), [])
        self.assertEqual(count, 1)
        row = self.db.accounts.execute_one(
            "SELECT stage,pd_12m,lifetime_pd,lgd,ead,expected_loss,discount_factor,scenario "
            "FROM loan_ecl_assessments"
        )
        self.assertEqual(row["stage"], 3)
        self.assertEqual(row["pd_12m"], 1)
        self.assertEqual(row["lifetime_pd"], 1)
        self.assertEqual(row["ead"], 1000)
        self.assertLess(row["discount_factor"], 1)
        self.assertGreater(row["discount_factor"], 0)
        self.assertLess(row["expected_loss"], row["ead"] * row["lgd"])
        self.assertIn('"discountFactor":', row["scenario"])
        self.assertGreater(row["expected_loss"], 0)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT ecl_allowance FROM loans WHERE product_code='ECL3'"
        )["ecl_allowance"], row["expected_loss"])
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT COUNT(*) count FROM bank_journal_entries WHERE event_type='expected_credit_loss'"
        )["count"], 1)

    def test_zero_effective_rate_has_no_discount(self):
        self.db.accounts.execute(
            "UPDATE loans SET contract_effective_rate=0 WHERE product_code='ECL3'"
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, 1_800_086_400), 1)
        row = self.db.accounts.execute_one(
            "SELECT discount_factor,ead,lgd,expected_loss FROM loan_ecl_assessments "
            "ORDER BY calculated_epoch DESC LIMIT 1"
        )
        self.assertEqual(row["discount_factor"], 1)
        self.assertGreater(row["expected_loss"], 0)


if __name__ == "__main__":
    unittest.main()
