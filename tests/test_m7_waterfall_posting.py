"""M7: priority recovery across one borrower's charged-off loans is really posted, reconciled, idempotent and atomic."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.credit_risk_manager import CreditRiskManager
from managers.recovery_waterfall_posting import post_waterfall, reconcile

NOW = 1800000000


class Fixture(unittest.TestCase):
    def setUp(self):
        self.old = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "낙원", 0, role="admin")
        self.db.users.create_user("borrower", "Borrower", "낙원", 500)
        self.loans = []
        for code, issuer, balance in (("LA", "BankA", 100), ("LB", "BankB", 60), ("LC", "BankA", 40)):
            self.db.products.execute("INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency) "
                                     "VALUES (?,?,5,1000,365,86400,0,?,'NWX')", (code, code, issuer))
            self.db.accounts.execute("INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,created_at,collection_status,quote_currency) "
                                     "VALUES ('borrower',?,?,?,'defaulted',datetime('now'),'defaulted','NWX')", (code, balance, balance))
            self.loans.append(self.db.accounts.execute_one("SELECT loan_id FROM loans WHERE product_code=?", (code,))[0])
        with self.db.atomic() as conn:
            for loan in self.loans:
                CreditRiskManager.charge_off_loan(conn, loan_id=loan, event_key=f"co{loan}", actor_id="admin", now=NOW)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old
        self.tmp.cleanup()

    def post(self, conn, proceeds=120, ranks=(1, 2, 2), key="waterfall-1"):
        return post_waterfall(conn, user_id="borrower", proceeds_minor_units=proceeds, claims=list(zip(self.loans, ranks)), event_key=key,
                              actor_id="admin", now=NOW + 1)

    def balance(self, conn, loan):
        return conn.execute("SELECT charged_off_balance_minor_units FROM loans WHERE loan_id=?", (loan,)).fetchone()[0]


class WaterfallPostingTests(Fixture):
    def test_priority_then_pro_rata_is_posted_and_reconciles(self):
        with self.db.atomic() as conn:
            result = self.post(conn)             # 120: rank 1 takes 100, the 20 left is shared 60:40 -> 12 / 8
            self.assertEqual(result["allocations"], {str(self.loans[0]): 100, str(self.loans[1]): 12, str(self.loans[2]): 8})
            self.assertEqual([self.balance(conn, loan) for loan in self.loans], [0, 48, 32])
            self.assertEqual(conn.execute("SELECT cash_minor FROM users WHERE user_id='borrower'").fetchone()[0], 380)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM business_cash_events WHERE kind='funded_chargeoff_recovery'").fetchone()[0], 3)
            self.assertEqual(reconcile(conn), [])

    def test_replay_changes_nothing_and_a_different_replay_is_refused(self):
        with self.db.atomic() as conn:
            self.post(conn)
            again = self.post(conn)
            self.assertTrue(again["duplicate"])
            self.assertEqual(conn.execute("SELECT cash_minor FROM users WHERE user_id='borrower'").fetchone()[0], 380)
            with self.assertRaises(ValueError):
                self.post(conn, proceeds=119)

    def test_a_failing_leg_rolls_back_every_leg_and_the_parent(self):
        real = __import__("managers.funded_recovery_manager", fromlist=["x"]).record_funded_recovery
        calls = []

        def flaky(conn, **kw):
            calls.append(kw["event_key"])
            if len(calls) == 2:
                raise RuntimeError("leg failed")
            return real(conn, **kw)
        with self.db.atomic() as conn:
            with patch("managers.recovery_waterfall_posting.record_funded_recovery", flaky), self.assertRaises(RuntimeError):
                self.post(conn)
            self.assertEqual(conn.execute("SELECT cash_minor FROM users WHERE user_id='borrower'").fetchone()[0], 500)
            self.assertEqual([self.balance(conn, loan) for loan in self.loans], [100, 60, 40])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM recovery_waterfall_events").fetchone()[0], 0)
            self.assertEqual(self.post(conn)["allocatedMinorUnits"], 120)       # a retry then works

    def test_proceeds_beyond_the_claims_leave_the_rest_with_the_borrower(self):
        with self.db.atomic() as conn:
            result = self.post(conn, proceeds=300)
            self.assertEqual((result["allocatedMinorUnits"], result["unallocatedMinorUnits"]), (200, 100))
            self.assertEqual(conn.execute("SELECT cash_minor FROM users WHERE user_id='borrower'").fetchone()[0], 300)

    def test_bad_input_and_non_admin_are_refused_without_posting(self):
        self.db.users.create_user("other", "Other", "낙원", 50)
        with self.db.atomic() as conn:
            bad = [dict(proceeds=0), dict(proceeds=True), dict(key="short"), dict(ranks=(0, 1, 1)), dict(ranks=(1.5, 1, 1))]
            for values in bad:
                with self.subTest(values=values), self.assertRaises(ValueError):
                    self.post(conn, **values)
            with self.assertRaises(ValueError):          # someone else's loans
                post_waterfall(conn, user_id="other", proceeds_minor_units=10, claims=[(self.loans[0], 1)], event_key="waterfall-x", actor_id="admin", now=NOW)
            with self.assertRaises(ValueError):          # not an administrator
                post_waterfall(conn, user_id="borrower", proceeds_minor_units=10, claims=[(self.loans[0], 1)], event_key="waterfall-y", actor_id="other", now=NOW)
            self.assertEqual(conn.execute("SELECT cash_minor FROM users WHERE user_id='borrower'").fetchone()[0], 500)

    def test_the_parent_record_is_append_only(self):
        with self.db.atomic() as conn:
            self.post(conn)
        for sql in ("UPDATE recovery_waterfall_events SET proceeds_minor_units=1", "DELETE FROM recovery_waterfall_events"):
            with self.assertRaises(Exception), self.db.atomic() as conn:
                conn.execute(sql)


if __name__ == "__main__":
    unittest.main()


class WaterfallApiTests(Fixture):
    def call(self, payload, role="admin"):
        import io, json
        from types import SimpleNamespace
        from unittest.mock import Mock
        from web_api import server
        body = json.dumps(payload).encode()
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "admin", "content-length": str(len(body))},
                                  path="/internal/admin/recovery-waterfall", rfile=io.BytesIO(body),
                                  send_json=lambda status, value: response.update(status=status, value=value))
        with patch.object(server, "INTERNAL_TOKEN", "secret"), patch.object(server, "identity", return_value={"userId": "admin", "role": role}), \
             patch.object(server, "domain", return_value=__import__("types").SimpleNamespace(db=self.db)), patch.object(server, "audit_admin_access", audit):
            server.Handler.do_POST(handler)
        return response, audit

    def payload(self, **kw):
        return {"action": "post", "userId": "borrower", "proceedsMinorUnits": 120, "eventKey": "api-waterfall-1",
                "claims": [{"loanId": loan, "rank": rank} for loan, rank in zip(self.loans, (1, 2, 2))]} | kw

    def test_admin_posts_once_and_it_is_audited(self):
        response, audit = self.call(self.payload())
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args[0][1], "recovery_waterfall.post")
        again, _ = self.call(self.payload())
        self.assertTrue(again["value"]["result"]["duplicate"])
        self.assertEqual(self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id='borrower'")[0], 380)

    def test_non_admin_and_bad_payloads_are_refused_without_posting_or_audit(self):
        response, audit = self.call(self.payload(), role="user")
        self.assertEqual(response["status"], 403)
        for bad in (self.payload(extra=1), self.payload(action="x"), self.payload(proceedsMinorUnits="120"), self.payload(claims=[]),
                    self.payload(claims=[{"loanId": 1}]), self.payload(eventKey="short"), self.payload(proceedsMinorUnits=0)):
            response, audit = self.call(bad)
            self.assertEqual(response["status"], 400, bad)
            audit.assert_not_called()
        self.assertEqual(self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id='borrower'")[0], 500)


class WaterfallAmountAuditTests(Fixture):
    def test_the_parent_table_leaves_no_unclassified_amount_field(self):
        from functions.money import audit_amount_field_coverage
        from managers.recovery_waterfall_posting import ensure_tables
        with self.db.atomic() as conn:
            ensure_tables(conn)
            audit = audit_amount_field_coverage(conn)
        self.assertEqual([r for r in audit["unclassified"] if r.get("source_table") == "recovery_waterfall_events"], [])
