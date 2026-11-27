"""M7: dated lending rules by country, customer and product type: resolution, contract-date behaviour, admin API (permission, validation, audit)."""
import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_inazina as fixtures
from functions import jurisdiction_rules as rules
from functions.money import audit_amount_field_coverage
from managers.interest_manager import InterestManager
from web_api import server

DAY = 86400


class RuleBase(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    country = "낙원"

    def add(self, conn, epoch=100, customer="*", product="*", **values):
        values = {"max_rate_bps": None, "apr_display": None, "tax_bps": None, **values}
        return rules.add_rule(conn, nationality=self.country, customer_type=customer, product_type=product, effective_epoch=epoch,
                              actor="admin", now=1, **values)


class ResolutionTests(RuleBase):
    def test_no_rule_means_none(self):
        with self.db.readonly() as conn:
            self.assertIsNone(rules.resolve(conn, "낙원", epoch=10**9))
            self.assertIsNone(rules.max_rate_percent(conn, "낙원", epoch=10**9))

    def test_the_rule_in_force_on_the_contract_date_applies(self):
        with self.db.atomic() as conn:
            self.add(conn, 100, max_rate_bps=2000)
            self.add(conn, 200, max_rate_bps=1500)
        with self.db.readonly() as conn:
            self.assertIsNone(rules.max_rate_percent(conn, "낙원", epoch=99))
            self.assertEqual(rules.max_rate_percent(conn, "낙원", epoch=150), 20)
            self.assertEqual(rules.max_rate_percent(conn, "낙원", epoch=200), 15)

    def test_a_specific_rule_beats_a_general_one_and_empty_fields_fall_through(self):
        with self.db.atomic() as conn:
            self.add(conn, 100, max_rate_bps=2000, apr_display="required", tax_bps=1540)
            self.add(conn, 100, customer="corporate", product="loan", max_rate_bps=900)
        with self.db.readonly() as conn:
            corporate = rules.resolve(conn, "낙원", customer_type="corporate", product_type="loan", epoch=150)
            self.assertEqual((corporate["max_rate_bps"], corporate["apr_display"], corporate["tax_bps"]), (900, "required", 1540))
            person = rules.resolve(conn, "낙원", customer_type="individual", product_type="loan", epoch=150)
            self.assertEqual(person["max_rate_bps"], 2000)
            deposit = rules.resolve(conn, "낙원", customer_type="corporate", product_type="deposit", epoch=150)
            self.assertEqual(deposit["max_rate_bps"], 2000)

    def test_rules_are_append_only_and_duplicates_refused(self):
        with self.db.atomic() as conn:
            rule_id = self.add(conn, 100, max_rate_bps=2000)
            with self.assertRaises(ValueError):
                self.add(conn, 100, max_rate_bps=1000)
        for sql in ("UPDATE jurisdiction_rate_rules SET max_rate_bps=1", "DELETE FROM jurisdiction_rate_rules"):
            with self.assertRaises(Exception):
                with self.db.atomic() as conn:
                    conn.execute(sql)
        with self.db.readonly() as conn:
            self.assertEqual(rules.list_rules(conn)[0]["rule_id"], rule_id)

    def test_bad_values_are_refused(self):
        bad = [dict(max_rate_bps=10001), dict(max_rate_bps=-1), dict(max_rate_bps=True), dict(max_rate_bps=5.5), dict(tax_bps=10001),
               dict(apr_display="maybe", max_rate_bps=1), dict(), dict(customer="alien", max_rate_bps=1), dict(product="swap", max_rate_bps=1),
               dict(epoch=-1, max_rate_bps=1), dict(epoch=True, max_rate_bps=1)]
        for values in bad:
            with self.subTest(values=values):
                with self.db.atomic() as conn:
                    with self.assertRaises(ValueError):
                        self.add(conn, **values)
        with self.db.atomic() as conn:
            with self.assertRaises(ValueError):
                rules.add_rule(conn, nationality="없는나라", customer_type="*", product_type="*", effective_epoch=1, max_rate_bps=1, actor="a", now=1)

    def test_the_table_leaves_no_unclassified_amount_field(self):
        with self.db.atomic() as conn:
            audit = audit_amount_field_coverage(conn)
        self.assertEqual([row for row in audit["unclassified"] if row.get("source_table") == "jurisdiction_rate_rules"], [])


class ContractTests(RuleBase):
    """A new loan uses the rule of its contract date; a signed loan never changes; no rule keeps the old behaviour."""

    country = "낙원"

    def sign(self, code):
        self.db.currency.execute("UPDATE country_currencies SET legal_max_rate=100")
        self.db.users.execute("UPDATE users SET nationality='낙원' WHERE user_id='citizen'")
        AdminCommands = __import__("managers.admin_commands_manager", fromlist=["x"])
        AdminCommands.AdminCommandsManager(self.bot).execute("admin", "상품생성", f"{code} loan 대출 10 1000000 2 86400 0 Inazina 0 7 20")
        self.assertTrue(self.bot.bank_mgr.apply_loan("citizen", code, 100000)[0])
        return self.db.accounts.execute_one("SELECT contract_rate FROM loans WHERE product_code=?", (code,))[0]

    def test_no_rule_keeps_the_existing_rate(self):
        self.assertEqual(self.sign("L0"), 10)

    def test_a_rule_in_force_caps_a_new_loan(self):
        with self.db.atomic() as conn:
            self.add(conn, 0, max_rate_bps=500)
        self.assertEqual(self.sign("L1"), 5)

    def test_a_rule_effective_later_does_not_touch_this_contract_and_a_signed_loan_is_unchanged(self):
        rate = self.sign("L2")
        with self.db.atomic() as conn:
            self.add(conn, int(time.time()) - 10, max_rate_bps=300)
        self.assertEqual(self.db.accounts.execute_one("SELECT contract_rate FROM loans WHERE product_code='L2'")[0], rate)
        with self.db.atomic() as conn:
            self.add(conn, int(time.time()) + 10 * DAY, customer="corporate", max_rate_bps=100)
        self.assertEqual(self.db.accounts.execute_one("SELECT contract_rate FROM loans WHERE product_code='L2'")[0], rate)


class DatedRuleOnSignedLoanTests(ContractTests):
    """Overdue interest and extension use the cap in force on the contract date; without a rule they behave as before."""

    def aged_loan(self, code):
        self.sign(code)
        start = time.time() - 10 * DAY
        self.db.accounts.execute("UPDATE loans SET created_at=datetime(?,'unixepoch'),last_payment_epoch=?,maturity_epoch=? WHERE product_code=?",
                                 (start, start + 9 * DAY, start + 9 * DAY, code))

    def overdue_growth(self):
        before = self.db.accounts.execute_one("SELECT outstanding_balance FROM loans")[0]
        InterestManager(self.db).process_user_interests("citizen", now=time.time())
        return self.db.accounts.execute_one("SELECT outstanding_balance FROM loans")[0] - before

    def test_overdue_interest_without_a_rule_is_unchanged(self):
        self.aged_loan("O0")
        self.assertGreater(self.overdue_growth(), 0)

    def test_overdue_interest_is_capped_by_the_rule_of_the_contract_date(self):
        self.aged_loan("O1")
        with self.db.atomic() as conn:
            self.add(conn, 0, max_rate_bps=0)
        self.assertEqual(self.overdue_growth(), 0)

    def test_a_backdated_rule_registered_after_signing_never_changes_a_signed_loan(self):
        self.aged_loan("B1")                 # signed 10 days ago
        start = time.time() - 10 * DAY
        with self.db.atomic() as conn:       # dated last year, registered just now
            rules.add_rule(conn, nationality="낙원", customer_type="*", product_type="*", effective_epoch=int(start) - 365 * DAY,
                           max_rate_bps=0, actor="admin", now=int(time.time()))
        self.assertGreater(self.overdue_growth(), 0)

    def test_a_rule_registered_before_signing_binds_a_loan_read_from_the_database(self):
        with self.db.atomic() as conn:
            rules.add_rule(conn, nationality="낙원", customer_type="*", product_type="*", effective_epoch=0, max_rate_bps=700,
                           actor="admin", now=int(time.time()) - 5)
        self.assertEqual(self.sign("B2"), 7)
        with self.db.readonly() as conn:
            row = conn.execute("SELECT user_id,created_at FROM loans WHERE product_code='B2'").fetchone()
            self.assertEqual(rules.contract_max_rate_percent(conn, "낙원", row["user_id"], row["created_at"]), 7)

    def test_a_rule_that_started_after_the_contract_does_not_cap_it(self):
        self.aged_loan("O2")
        with self.db.atomic() as conn:
            self.add(conn, int(time.time()) - DAY, max_rate_bps=0)    # the loan was signed 10 days ago
        self.assertGreater(self.overdue_growth(), 0)

    def test_extension_checks_the_rule_of_the_contract_date(self):
        self.aged_loan("E1")                           # extension rate 20
        with self.db.atomic() as conn:
            self.add(conn, 0, max_rate_bps=1500)
        ok, message = self.bot.bank_mgr.extend_loan("citizen", "E1", now=time.time())
        self.assertFalse(ok)
        self.assertIn("최고금리", message)

    def test_a_corporate_rule_does_not_touch_an_individual_loan(self):
        with self.db.atomic() as conn:
            self.add(conn, 0, customer="corporate", max_rate_bps=100)
        self.assertEqual(self.sign("C1"), 10)

    def test_customer_type_follows_the_account_kind(self):
        self.assertEqual(rules.customer_type_for("company:ACME"), "corporate")
        self.assertEqual(rules.customer_type_for("u1"), "individual")


class AdminApiTests(RuleBase):
    def call(self, method, path, role="admin", payload=None):
        body = json.dumps(payload or {}).encode()
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "a", "content-length": str(len(body))},
                                  path=path, rfile=io.BytesIO(body), send_json=lambda status, value: response.update(status=status, value=value))
        db = self.db
        with patch.object(server, "INTERNAL_TOKEN", "secret"), patch.object(server, "identity", return_value={"userId": "a", "role": role}), \
             patch.object(server, "domain", return_value=SimpleNamespace(db=db)), patch.object(server, "audit_admin_access", audit), \
             patch.object(server, "connect_readonly", lambda: db.readonly()), patch.object(server, "LegacyNames", lambda c: c):
            getattr(server.Handler, method)(handler)
        return response, audit

    PAYLOAD = {"action": "add_rule", "nationality": "낙원", "customerType": "*", "productType": "loan", "effectiveEpoch": 1000,
               "maxRateBps": 2000, "aprDisplay": "required", "taxBps": None, "note": "test"}

    def test_admin_adds_and_reads_a_rule_with_audit(self):
        response, audit = self.call("do_POST", "/internal/admin/rate-rules", payload=self.PAYLOAD)
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args[0][1], "rate_rules.add")
        response, audit = self.call("do_GET", "/internal/admin/rate-rules")
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(response["value"]["rules"][0]["max_rate_bps"], 2000)
        self.assertEqual(audit.call_args[0][1], "rate_rules.read")

    def test_non_admin_is_refused_without_write_read_or_audit(self):
        for method, payload in (("do_POST", self.PAYLOAD), ("do_GET", None)):
            response, audit = self.call(method, "/internal/admin/rate-rules", role="user", payload=payload)
            self.assertEqual(response["status"], 403)
            audit.assert_not_called()
        with self.db.readonly() as conn:
            self.assertEqual(rules.list_rules(conn), [])

    def test_bad_payloads_are_rejected_without_writing_or_auditing(self):
        bad = [dict(self.PAYLOAD, action="x"), dict(self.PAYLOAD, extra=1), {k: v for k, v in self.PAYLOAD.items() if k != "note"},
               dict(self.PAYLOAD, maxRateBps="20"), dict(self.PAYLOAD, maxRateBps=10001), dict(self.PAYLOAD, nationality="없음"),
               dict(self.PAYLOAD, customerType="alien"), dict(self.PAYLOAD, aprDisplay="x"), dict(self.PAYLOAD, effectiveEpoch=-5),
               dict(self.PAYLOAD, maxRateBps=None, aprDisplay=None)]
        for payload in bad:
            with self.subTest(payload={k: v for k, v in payload.items() if self.PAYLOAD.get(k) != v}):
                response, audit = self.call("do_POST", "/internal/admin/rate-rules", payload=payload)
                self.assertEqual(response["status"], 400, response)
                audit.assert_not_called()
        with self.db.readonly() as conn:
            self.assertEqual(rules.list_rules(conn), [])


if __name__ == "__main__":
    unittest.main()


class DepositTaxRuleTests(unittest.TestCase):
    """tax_bps of a country rule replaces the deposit interest tax rate only when a rule exists for the contract date."""
    T0 = 1_800_000_000

    def setUp(self):
        import tempfile
        from pathlib import Path
        from database import DatabaseManager
        from database.db_manager import DBManager
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "낙원", 1_000_000, role="admin")
        self.db.users.create_user("user", "User", "낙원", 5_000_000)
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,compound_type,issuer,quote_currency,rate_type,rate_nationality,"
            "base_rate_spread,repricing_cycle_seconds) VALUES ('SX','SX',10,1,365,86400,0,'Bank','NWX','fixed','낙원',0,86400)")

    def tearDown(self):
        from database.db_manager import DBManager
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def interest(self, rule_bps=None):
        from managers.bank_manager import BankManager
        if rule_bps is not None:
            with self.db.atomic() as conn:
                rules.add_rule(conn, nationality="낙원", customer_type="*", product_type="deposit", effective_epoch=0, tax_bps=rule_bps,
                               actor="admin", now=int(time.time()) - 5)
        self.assertTrue(BankManager(self.db).open_account("user", "SX", "1234", 100_000)[0])
        self.db.accounts.execute("UPDATE accounts SET created_at=datetime(?,'unixepoch'),last_payment_epoch=?", (self.T0, self.T0))
        before = self.db.accounts.execute_one("SELECT balance FROM accounts")[0]
        InterestManager(self.db).process_user_interests(now=self.T0 + 30 * DAY, user_id="user")
        return self.db.accounts.execute_one("SELECT balance FROM accounts")[0] - before

    def test_no_rule_keeps_the_current_country_tax(self):
        self.assertEqual(self.interest(), 797)          # 821.9 gross, 3% withheld

    def test_a_rule_in_force_at_signing_sets_the_tax_rate(self):
        self.assertEqual(self.interest(rule_bps=0), 822)
