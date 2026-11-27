"""M7: partial prepayment. Default (and every existing contract) pays installments ahead as before; the optional product mode
'reduce_installment' keeps the term and recasts the remaining installments on the lower balance."""
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_manager import BankManager

T0 = 1_800_000_000


class PrepaymentBase(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "낙원", 1_000_000, role="admin")
        self.db.users.create_user("user", "User", "낙원", 5_000_000)
        self.db.currency.execute("UPDATE country_currencies SET legal_max_rate=100 WHERE nationality='낙원'")
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency,"
            "rate_type,rate_nationality,base_rate_spread,repricing_cycle_seconds,amortization_type,payment_frequency_days) "
            "VALUES ('LP','LP',6,1000000,365,86400,0,'Bank','NWX','fixed','낙원',0,86400,'annuity',30)")
        self.bank = BankManager(self.db)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def sign(self, user="user"):
        self.assertTrue(self.bank.apply_loan(user, "LP", 100_000)[0])

    def schedule(self, user="user"):
        return [tuple(r) for r in self.db.accounts.execute(
            "SELECT installment_number,principal_due,interest_due,amount_paid,status FROM loan_payment_schedule WHERE loan_id="
            "(SELECT loan_id FROM loans WHERE user_id=?) ORDER BY installment_number", (user,))]

    def balance(self, user="user"):
        return self.db.accounts.execute_one("SELECT outstanding_balance_minor_units FROM loans WHERE user_id=?", (user,))[0]


class PrepaymentTests(PrepaymentBase):
    def set_mode(self, mode, actor="admin"):
        from managers.prepayment_mode import set_product_mode
        with self.db.atomic() as conn:
            set_product_mode(conn, "LP", mode, actor=actor, now=T0)

    def test_default_pays_installments_ahead_exactly_as_before(self):
        self.sign()
        original = self.schedule()
        self.assertTrue(self.bank.repay_loan("user", "LP", 30_000)[0])
        rows = self.schedule()
        self.assertEqual(self.balance(), 70_000)
        self.assertEqual(len(rows), len(original))
        self.assertEqual(sum(r[3] for r in rows), 30_000)                      # the whole payment went onto installments
        self.assertEqual([r[4] for r in rows[:4]], ["paid", "paid", "paid", "partial"])
        self.assertEqual([r[1:3] for r in rows], [r[1:3] for r in original])    # amounts due are not recast

    def test_reduce_installment_keeps_the_term_and_recasts_the_remaining_installments(self):
        self.set_mode("reduce_installment")
        self.sign()
        original = self.schedule()
        dues = [tuple(r) for r in self.db.accounts.execute("SELECT due_epoch FROM loan_payment_schedule ORDER BY installment_number")]
        self.assertTrue(self.bank.repay_loan("user", "LP", 30_000)[0])
        rows = self.schedule()
        self.assertEqual(len(rows), len(original))
        self.assertEqual([r[4] for r in rows], ["scheduled"] * len(rows))      # nothing is paid ahead
        self.assertEqual(sum(r[3] for r in rows), 0)
        self.assertEqual(sum(r[1] for r in rows), 70_000)                      # remaining principal due = new balance
        self.assertLess(rows[0][1] + rows[0][2], original[0][1] + original[0][2])
        self.assertEqual(dues, [tuple(r) for r in self.db.accounts.execute("SELECT due_epoch FROM loan_payment_schedule ORDER BY installment_number")])

    def test_a_loan_signed_before_the_mode_keeps_the_default_and_a_later_product_change_does_not_touch_a_signed_loan(self):
        self.sign()                                                            # signed with no mode
        self.set_mode("reduce_installment")
        self.assertTrue(self.bank.repay_loan("user", "LP", 30_000)[0])
        self.assertEqual([r[4] for r in self.schedule()[:3]], ["paid"] * 3)
        self.db.users.create_user("user2", "User2", "낙원", 5_000_000)
        self.sign("user2")                                                     # signed with the mode
        self.set_mode("shorten")                                               # product changed back afterwards
        self.assertTrue(self.bank.repay_loan("user2", "LP", 30_000)[0])
        self.assertEqual([r[4] for r in self.schedule("user2")], ["scheduled"] * 13)

    def test_only_an_admin_can_set_a_known_mode_on_a_known_product(self):
        from managers.prepayment_mode import set_product_mode
        with self.db.atomic() as conn:
            for args in (("LP", "turbo", "admin"), ("NOPE", "reduce_installment", "admin"), ("LP", "reduce_installment", "user")):
                with self.assertRaises(ValueError):
                    set_product_mode(conn, args[0], args[1], actor=args[2], now=T0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_prepayment_mode").fetchone()[0], 0)

    def test_the_contract_copy_is_append_only(self):
        self.set_mode("reduce_installment")
        self.sign()
        for sql in ("UPDATE contract_prepayment_mode SET mode='reduce_installment'", "DELETE FROM contract_prepayment_mode"):
            with self.assertRaises(Exception), self.db.atomic() as conn:
                conn.execute(sql)


class PrepaymentApiTests(PrepaymentBase):
    def call(self, method, payload=None, role="admin"):
        import io, json
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from web_api import server
        body = json.dumps(payload or {}).encode()
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "admin", "content-length": str(len(body))},
                                  path="/internal/admin/prepayment-mode", rfile=io.BytesIO(body), send_json=lambda status, value: response.update(status=status, value=value))
        db = self.db
        with patch.object(server, "INTERNAL_TOKEN", "secret"), patch.object(server, "identity", return_value={"userId": "admin", "role": role}), \
             patch.object(server, "domain", return_value=SimpleNamespace(db=db)), patch.object(server, "audit_admin_access", audit), \
             patch.object(server, "connect_readonly", lambda: db.readonly()), patch.object(server, "LegacyNames", lambda c: c):
            getattr(server.Handler, method)(handler)
        return response, audit

    PAYLOAD = {"action": "set", "productCode": "LP", "mode": "reduce_installment"}

    def test_admin_sets_and_lists_with_audit_and_bad_requests_are_refused(self):
        response, audit = self.call("do_POST", self.PAYLOAD)
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args[0][1], "prepayment_mode.set")
        self.assertEqual(self.call("do_GET")[0]["value"]["modes"][0]["mode"], "reduce_installment")
        self.assertEqual(self.call("do_POST", self.PAYLOAD, role="user")[0]["status"], 403)
        for bad in (dict(self.PAYLOAD, mode="turbo"), dict(self.PAYLOAD, productCode="NOPE"), dict(self.PAYLOAD, extra=1), dict(self.PAYLOAD, action="x")):
            response, audit = self.call("do_POST", bad)
            self.assertEqual(response["status"], 400, bad)
            audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
