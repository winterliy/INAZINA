import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import DatabaseManager
from database.db_manager import DBManager
from managers.admin_control_manager import AdminControlManager
from managers.bank_manager import BankManager
from managers.credit_score_manager import CreditScoreManager

DAY = 86400


class CreditScoreTests(unittest.TestCase):
    def setUp(self):
        self.original_database_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.bank = BankManager(self.db)
        self.db.users.create_user("borrower", "Borrower", "Nac", 1_000_000)
        self.db.users.create_user("admin", "Admin", "Nac", 0, role="admin")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.original_database_dir
        self.tempdir.cleanup()

    def score(self, user_id="borrower"):
        return self.db.users.get_user(user_id)["credit_score"]

    def refresh(self, now, user_id="borrower"):
        with self.db.atomic() as conn:
            return CreditScoreManager.refresh(conn, user_id, now)

    def record(self, event_type, reference, occurred, now):
        with self.db.atomic() as conn:
            CreditScoreManager.record(conn, "borrower", event_type, reference,
                                      occurred_epoch=occurred, now=now)

    def annuity_loan(self, code="CREDIT", amount=90_000):
        self.db.products.register_liability(code, "Credit Loan", 0, 0, 90, 3600, 0, "Bank")
        self.db.products.execute(
            "UPDATE liability SET amortization_type='annuity',payment_frequency_days=30 "
            "WHERE product_code=?", (code,),
        )
        success, message = self.bank.apply_loan("borrower", code, amount)
        self.assertTrue(success, message)
        loan = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code=?", (code,))["loan_id"]
        due = [row["due_epoch"] for row in self.db.accounts.execute(
            "SELECT due_epoch FROM loan_payment_schedule WHERE loan_id=? "
            "ORDER BY installment_number", (loan,))]
        return loan, due

    def drain_funds(self):
        self.db.users.execute("UPDATE users SET cash=0 WHERE user_id='borrower'")
        self.db.accounts.execute("UPDATE accounts SET balance_units=0 WHERE user_id='borrower'")

    def test_enrollment_keeps_granted_surplus_and_fades_legacy_deductions(self):
        now = time.time()
        self.db.users.execute("UPDATE users SET credit_score=820 WHERE user_id='borrower'")
        self.assertEqual(self.refresh(now), 820)
        baseline = self.db.users.execute_one(
            "SELECT points FROM credit_events WHERE user_id='borrower' "
            "AND event_type='legacy_baseline'")
        self.assertAlmostEqual(baseline["points"], 120)
        self.assertEqual(self.refresh(now+365*DAY), 820)

        self.db.users.create_user("penalized", "Penalized", "Nac", 0)
        self.db.users.execute("UPDATE users SET credit_score=600 WHERE user_id='penalized'")
        self.assertEqual(self.refresh(now, "penalized"), 600)
        # Half-life 365 days: half of the unexplained −100 remains after a year.
        self.assertEqual(self.refresh(now+365*DAY, "penalized"), 650)

    def test_missed_payment_decays_and_on_time_history_rebuilds_score(self):
        now = time.time()
        self.refresh(now)
        self.record("payment_missed", "m1", now, now)
        self.assertEqual(self.score(), 665)
        # 180-day half-life, then dropped after the 3-year retention window.
        self.assertEqual(self.refresh(now+180*DAY), 683)
        self.assertEqual(self.refresh(now+1100*DAY), 700)
        for month in range(12):
            epoch = now+(month+1)*30*DAY
            self.record("payment_on_time", f"p{month}", epoch, epoch)
        self.assertGreater(self.score(), 665)
        self.assertLessEqual(
            CreditScoreManager.compute(self.db.users.conn, "borrower", now+360*DAY)
            ["factors"]["on_time"], 120)

    def test_paying_a_missed_installment_cures_current_delinquency(self):
        loan, due = self.annuity_loan()
        self.drain_funds()
        self.bank.process_scheduled_loan_payments(now=due[0]+1)
        missed_score = self.score()
        status = self.db.accounts.execute_one(
            "SELECT status FROM loan_payment_schedule WHERE loan_id=? AND installment_number=1",
            (loan,))["status"]
        self.assertEqual(status, "missed")
        factors = CreditScoreManager.compute(self.db.users.conn, "borrower", due[0]+1)["factors"]
        self.assertEqual((factors["missed"] < 0, factors["unpaid_installments"]), (True, -20))

        self.db.users.execute("UPDATE users SET cash=1000000 WHERE user_id='borrower'")
        self.bank.process_scheduled_loan_payments(now=due[0]+2)
        cured = CreditScoreManager.compute(self.db.users.conn, "borrower", due[0]+2)["factors"]
        self.assertEqual(cured["unpaid_installments"], 0)
        self.assertGreater(cured["cured"], 0)
        self.assertGreater(self.score(), missed_score)
        self.assertEqual(self.db.users.execute_one(
            "SELECT COUNT(*) FROM credit_events WHERE event_type='payment_cured'")[0], 1)

    def test_scheduled_payments_and_payoff_record_positive_history(self):
        loan, due = self.annuity_loan()
        opened_score = self.score()
        paid, missed, _ = self.bank.process_scheduled_loan_payments(now=due[-1]+1)
        self.assertEqual((paid, missed), (3, 0))
        kinds = [row[0] for row in self.db.users.execute(
            "SELECT event_type FROM credit_events WHERE loan_id=? ORDER BY event_id", (loan,))]
        self.assertEqual(kinds, ["payment_on_time"]*3+["loan_closed"])
        self.assertGreater(self.score(), opened_score)

    def test_default_penalty_lifts_when_repaid_and_record_decays(self):
        loan, _due = self.annuity_loan(code="BULLET")
        self.db.products.execute(
            "UPDATE liability SET acceleration_after_days=5,default_after_days=10 "
            "WHERE product_code='BULLET'")
        maturity = self.db.accounts.execute_one(
            "SELECT maturity_epoch FROM loans WHERE loan_id=?", (loan,))["maturity_epoch"]
        self.drain_funds()
        later = maturity+20*DAY
        from managers.interest_manager import InterestManager
        InterestManager(self.db).process_user_interests("borrower", now=later)
        events = {row["event_type"]: row["occurred_epoch"] for row in self.db.users.execute(
            "SELECT event_type,occurred_epoch FROM credit_events WHERE loan_id=?", (loan,))}
        # Contractual trigger dates are reported, not the late settlement time.
        self.assertAlmostEqual(events["acceleration"], maturity+5*DAY)
        self.assertAlmostEqual(events["default"], maturity+10*DAY)
        defaulted = self.score()
        self.assertLess(defaulted, 400)

        balance = self.db.accounts.execute_one(
            "SELECT outstanding_balance FROM loans WHERE loan_id=?", (loan,))["outstanding_balance"]
        self.db.users.execute("UPDATE users SET cash=? WHERE user_id='borrower'", (balance*2,))
        success, message = self.bank.repay_loan("borrower", "BULLET", balance)
        self.assertTrue(success, message)
        repaid = self.score()
        self.assertGreater(repaid, defaulted)
        self.assertGreater(self.refresh(time.time()+2*365*DAY), repaid)

    def test_admin_correction_is_an_audited_event_and_score_keeps_moving(self):
        control = AdminControlManager(SimpleNamespace(db=self.db))
        control.user("admin", "borrower", {"credit_score": 900, "credit_score_reason": "오류 정정"})
        self.assertEqual(self.score(), 900)
        event = self.db.users.execute_one(
            "SELECT actor_id,reason FROM credit_events WHERE event_type='manual_adjustment'")
        self.assertEqual((event["actor_id"], event["reason"]), ("admin", "오류 정정"))
        now = time.time()
        self.record("payment_missed", "after-fix", now, now)
        self.assertEqual(self.score(), 865)

    def test_outside_edit_is_absorbed_as_external_adjustment(self):
        now = time.time()
        self.refresh(now)
        self.db.users.execute("UPDATE users SET credit_score=500 WHERE user_id='borrower'")
        self.assertEqual(self.refresh(now+1), 500)
        self.assertEqual(self.db.users.execute_one(
            "SELECT COUNT(*) FROM credit_events WHERE event_type='external_adjustment'")[0], 1)

    def test_credit_records_are_append_only_and_idempotent(self):
        now = time.time()
        self.record("payment_missed", "same", now, now)
        self.record("payment_missed", "same", now, now)
        self.assertEqual(self.score(), 665)
        for statement in ("UPDATE credit_events SET points=1",
                          "DELETE FROM credit_events",
                          "DELETE FROM credit_score_history"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.users.execute(statement)

    def test_periodic_refresh_applies_decay_without_user_activity(self):
        now = time.time()
        self.record("payment_missed", "old", now-180*DAY, now)
        self.assertEqual(self.score(), 683)
        self.assertGreaterEqual(CreditScoreManager(self.db).refresh_due(now+30*DAY), 1)
        self.assertEqual(self.score(), 684)  # 35 × 0.5^(210/180) ≈ 15.6
        history = self.db.users.execute_one(
            "SELECT trigger FROM credit_score_history ORDER BY history_id DESC LIMIT 1")
        self.assertEqual(history["trigger"], "periodic")

    def test_only_stacked_new_loans_lower_the_score(self):
        for code in ("N1", "N2", "N3"):
            self.db.products.register_liability(code, code, 0, 0, 365, 3600, 0, "Bank")
            self.assertTrue(self.bank.apply_loan("borrower", code, 1000)[0])
            factors = CreditScoreManager.compute(self.db.users.conn, "borrower")["factors"]
            if code == "N1":
                self.assertEqual(factors["new_credit"], 0)
        self.assertEqual(factors["new_credit"], -20)


if __name__ == "__main__":
    unittest.main()
