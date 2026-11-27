"""M7: guarantor claims on charged-off loans: append-only guarantee, recovery paid from the guarantor's wallet, replay-safe, atomic, reconciled."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.credit_risk_manager import CreditRiskManager
from managers import guarantor_recovery as guarantor

NOW = 1800000000


class GuarantorBase(unittest.TestCase):
    def setUp(self):
        self.old = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "낙원", 0, role="admin")
        self.db.users.create_user("borrower", "Borrower", "낙원", 50)
        self.db.users.create_user("friend", "Friend", "낙원", 500)
        self.db.products.execute("INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency) "
                                 "VALUES ('LG','LG',5,1000,365,86400,0,'Bank','NWX')")
        self.db.accounts.execute("INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,created_at,collection_status,quote_currency) "
                                 "VALUES ('borrower','LG',200,200,'defaulted',datetime('now'),'defaulted','NWX')")
        self.loan = self.db.accounts.execute_one("SELECT loan_id FROM loans")[0]
        with self.db.atomic() as conn:
            CreditRiskManager.charge_off_loan(conn, loan_id=self.loan, event_key="co1", actor_id="admin", now=NOW)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old
        self.tmp.cleanup()

    def cash(self, user):
        return self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id=?", (user,))[0]

    def balance(self):
        return self.db.accounts.execute_one("SELECT charged_off_balance_minor_units FROM loans WHERE loan_id=?", (self.loan,))[0]

    def register(self, conn, maximum=150):
        return guarantor.register_guarantee(conn, loan_id=self.loan, guarantor_id="friend", max_minor_units=maximum, actor_id="admin", now=NOW)

    def claim(self, conn, amount=100, key="claim-0001-aaaa", actor="admin"):
        return guarantor.claim_from_guarantor(conn, loan_id=self.loan, guarantor_id="friend", amount_minor_units=amount, event_key=key, actor_id=actor, now=NOW + 1)


class GuarantorTests(GuarantorBase):
    def test_a_claim_is_paid_from_the_guarantors_wallet_reduces_the_loan_and_reconciles(self):
        with self.db.atomic() as conn:
            self.register(conn)
            result = self.claim(conn)
        self.assertEqual((result["duplicate"], result["remainingGuaranteeMinorUnits"]), (False, 50))
        self.assertEqual((self.cash("friend"), self.cash("borrower"), self.balance()), (400, 50, 100))
        with self.db.atomic() as conn:
            self.assertEqual(guarantor.reconcile(conn), [])

    def test_replay_changes_nothing_different_details_are_refused_and_the_cap_holds(self):
        with self.db.atomic() as conn:
            self.register(conn)
            self.claim(conn)
            self.assertTrue(self.claim(conn)["duplicate"])
            with self.assertRaises(ValueError):
                self.claim(conn, amount=99)
            with self.assertRaises(ValueError):                         # 100 + 60 > 150
                self.claim(conn, amount=60, key="claim-0002-bbbb")
        self.assertEqual((self.cash("friend"), self.balance()), (400, 100))

    def test_a_failing_recovery_rolls_back_the_payment_record_and_the_wallet(self):
        from managers.credit_risk_manager import CreditRiskManager as CRM
        with self.db.atomic() as conn:
            self.register(conn)
            with patch.object(CRM, "record_chargeoff_recovery", side_effect=RuntimeError("posting failed")), self.assertRaises(RuntimeError):
                self.claim(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM loan_guarantor_payments").fetchone()[0], 0)
            self.assertEqual(self.claim(conn)["amountMinorUnits"], 100)             # a retry then works
        self.assertEqual((self.cash("friend"), self.balance()), (400, 100))

    def test_guarantee_rules_admin_and_borrower_default_payer_unchanged(self):
        with self.db.atomic() as conn:
            for kwargs in (dict(guarantor_id="borrower"), dict(guarantor_id="nobody"), dict(max_minor_units=0), dict(max_minor_units=True), dict(loan_id=999)):
                args = dict(loan_id=self.loan, guarantor_id="friend", max_minor_units=100, actor_id="admin", now=NOW) | kwargs
                with self.assertRaises(ValueError):
                    guarantor.register_guarantee(conn, **args)
            with self.assertRaises(ValueError):
                guarantor.register_guarantee(conn, loan_id=self.loan, guarantor_id="friend", max_minor_units=100, actor_id="friend", now=NOW)
            self.register(conn)
            with self.assertRaises(ValueError):
                self.register(conn)                                     # one guarantee per guarantor and loan
            with self.assertRaises(ValueError):
                self.claim(conn, actor="friend")                       # not an administrator
            with self.assertRaises(ValueError):
                guarantor.claim_from_guarantor(conn, loan_id=self.loan, guarantor_id="borrower", amount_minor_units=1, event_key="claim-0009-zzzz", actor_id="admin", now=NOW)
            # the default payer is still the borrower
            from managers.funded_recovery_manager import record_funded_recovery
            record_funded_recovery(conn, loan_id=self.loan, amount_minor_units=10, event_key="plain-recovery", actor_id="admin", now=NOW + 2)
        self.assertEqual(self.cash("borrower"), 40)

    def test_the_records_are_append_only(self):
        with self.db.atomic() as conn:
            self.register(conn)
            self.claim(conn)
        for sql in ("UPDATE loan_guarantees SET max_minor_units=1", "DELETE FROM loan_guarantees", "UPDATE loan_guarantor_payments SET amount_minor_units=1",
                    "DELETE FROM loan_guarantor_payments"):
            with self.assertRaises(Exception), self.db.atomic() as conn:
                conn.execute(sql)


class GuarantorApiTests(GuarantorBase):
    def call(self, payload, role="admin"):
        import io, json
        from types import SimpleNamespace
        from unittest.mock import Mock
        from web_api import server
        body = json.dumps(payload).encode()
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "admin", "content-length": str(len(body))},
                                  path="/internal/admin/guarantor", rfile=io.BytesIO(body), send_json=lambda status, value: response.update(status=status, value=value))
        db = self.db
        with patch.object(server, "INTERNAL_TOKEN", "secret"), patch.object(server, "identity", return_value={"userId": "admin", "role": role}), \
             patch.object(server, "domain", return_value=SimpleNamespace(db=db)), patch.object(server, "audit_admin_access", audit):
            server.Handler.do_POST(handler)
        return response, audit

    def test_admin_registers_and_claims_with_audit_and_bad_requests_are_refused(self):
        register = {"action": "register", "loanId": self.loan, "guarantorId": "friend", "maxMinorUnits": 150}
        response, audit = self.call(register)
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args[0][1], "guarantor.register")
        claim = {"action": "claim", "loanId": self.loan, "guarantorId": "friend", "amountMinorUnits": 100, "eventKey": "api-claim-0001"}
        response, audit = self.call(claim)
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args[0][1], "guarantor.claim")
        self.assertEqual((self.cash("friend"), self.balance()), (400, 100))
        self.assertEqual(self.call(claim, role="user")[0]["status"], 403)
        for bad in (dict(claim, extra=1), dict(claim, amountMinorUnits=0), dict(claim, amountMinorUnits="5"), dict(claim, eventKey="short"), dict(claim, action="x"),
                    dict(claim, amountMinorUnits=60, eventKey="api-claim-0002")):
            response, audit = self.call(bad)
            self.assertEqual(response["status"], 400, bad)
            audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
