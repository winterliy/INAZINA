"""M7: deposit insurance coverage, assessed premium and resolution allocation preview (read-only), and its admin API."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from managers import deposit_insurance_fund as fund
from managers.deposit_insurance_fund import preview
from web_api import server
import test_alm_manager


class Fixture(unittest.TestCase):
    setUp = test_alm_manager.ALMManagerTests.setUp
    tearDown = test_alm_manager.ALMManagerTests.tearDown


class DepositInsuranceTests(Fixture):
    def preview(self, **kw):
        values = dict(premium_bps=50, proceeds_minor_units=0, general_claims_minor_units=0) | kw
        with self.db.readonly() as conn:
            return preview(conn, "Bank", **values)

    # the fixture: u1 500 insured, u2 100 uninsured, company 400 insured, limit 300
    def test_coverage_is_per_depositor_within_the_limit(self):
        result = self.preview()
        self.assertEqual((result["coveredMinorUnits"], result["uninsuredMinorUnits"], result["totalDepositsMinorUnits"]), (600, 400, 1000))
        self.assertEqual((result["depositorCount"], result["accountCount"]), (3, 3))

    def test_premium_is_assessed_on_covered_deposits_and_nothing_is_posted(self):
        before = self.db.accounts.execute_one("SELECT COUNT(*) FROM bank_journal_entries")[0]
        self.assertEqual(self.preview(premium_bps=250)["assessedPremiumMinorUnits"], 15)
        self.assertEqual(self.db.accounts.execute_one("SELECT COUNT(*) FROM bank_journal_entries")[0], before)

    def test_allocation_follows_priority_and_conserves_proceeds(self):
        result = self.preview(proceeds_minor_units=800, general_claims_minor_units=300)
        self.assertEqual(result["allocations"], {"insurer_subrogation": 600, "uninsured_depositors": 200, "general_creditors": 0})
        self.assertEqual(sum(result["allocations"].values()) + result["unallocatedMinorUnits"], 800)
        self.assertEqual(result["shortfalls"]["uninsured_depositors"], 200)

    def test_bad_inputs_and_unknown_bank_are_rejected(self):
        for bad in (dict(premium_bps=-1), dict(premium_bps=10001), dict(proceeds_minor_units=True), dict(general_claims_minor_units=1.5)):
            with self.assertRaises(ValueError):
                self.preview(**bad)
        with self.db.readonly() as conn, self.assertRaises(ValueError):
            preview(conn, "Nope", premium_bps=1, proceeds_minor_units=0, general_claims_minor_units=0)


class DepositInsuranceFundLedgerTests(Fixture):
    NOW = 1_800_000_000

    def setUp(self):
        super().setUp()
        self.db.users.create_user("admin", "Admin", "낙원", 0, role="admin")

    def collect(self, conn, key="premium-2027-q1", bps=250, actor="admin"):
        return fund.collect_premium(conn, "Bank", premium_bps=bps, event_key=key, actor_id=actor, now=self.NOW)

    def test_a_premium_is_journaled_recorded_once_and_reconciles(self):
        with self.db.atomic() as conn:
            first = self.collect(conn)
            again = self.collect(conn)
            self.assertEqual((first["amountMinorUnits"], first["duplicate"], again["duplicate"]), (15, False, True))
            self.assertEqual(fund.fund_overview(conn)["NWX"]["balanceMinorUnits"], 15)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_journal_entries WHERE event_type='deposit_insurance_premium'").fetchone()[0], 1)
            self.assertEqual(fund.reconcile(conn), [])

    def test_a_failed_posting_leaves_no_fund_event(self):
        with self.db.atomic() as conn:
            with patch("managers.deposit_insurance_fund.BankJournal.post", side_effect=RuntimeError("journal failed")), self.assertRaises(RuntimeError):
                self.collect(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deposit_insurance_fund_events").fetchone()[0], 0)
            self.assertEqual(self.collect(conn)["amountMinorUnits"], 15)             # a retry then works

    def test_non_admin_bad_key_and_zero_premium_are_refused(self):
        with self.db.atomic() as conn:
            for kwargs in (dict(actor="u1"), dict(key="short"), dict(bps=0)):
                with self.assertRaises(ValueError):
                    self.collect(conn, **kwargs)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deposit_insurance_fund_events").fetchone()[0], 0)

    def test_claim_payouts_are_recorded_once_and_a_shortfall_is_shown(self):
        self.db.accounts.execute("INSERT INTO deposit_insurance_claims(issuer,user_id,account_number,covered_amount,uninsured_amount,payout_currency,payout_amount,"
                                 "payout_amount_minor_units,payout_decimal_places,resolved_epoch) VALUES ('Bank','u1','A1',300,200,'NWX',300,300,0,?)", (self.NOW,))
        with self.db.atomic() as conn:
            self.collect(conn)
            self.assertEqual(fund.record_claim_payouts(conn, "Bank", actor_id="admin", now=self.NOW)["recorded"], 1)
            self.assertEqual(fund.record_claim_payouts(conn, "Bank", actor_id="admin", now=self.NOW)["recorded"], 0)
            overview = fund.fund_overview(conn)["NWX"]
            self.assertEqual((overview["balanceMinorUnits"], overview["shortfallMinorUnits"]), (-285, 285))
            self.assertEqual(fund.reconcile(conn), [])

    def test_the_fund_events_are_append_only(self):
        with self.db.atomic() as conn:
            self.collect(conn)
        for sql in ("UPDATE deposit_insurance_fund_events SET amount_minor_units=1", "DELETE FROM deposit_insurance_fund_events"):
            with self.assertRaises(Exception), self.db.atomic() as conn:
                conn.execute(sql)


class DepositInsuranceApiTests(Fixture):
    def get(self, query, role="admin"):
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "a"},
                                  path="/internal/admin/deposit-insurance?" + query, rfile=io.BytesIO(b""),
                                  send_json=lambda status, value: response.update(status=status, value=value))
        db = self.db
        with patch.object(server, "INTERNAL_TOKEN", "secret"), patch.object(server, "identity", return_value={"userId": "a", "role": role}), \
             patch.object(server, "audit_admin_access", audit), patch.object(server, "connect_readonly", lambda: db.readonly()), \
             patch.object(server, "LegacyNames", lambda c: c), patch("functions.banks.bank_issuer", lambda c, bank_id: "Bank"):
            server.Handler.do_GET(handler)
        return response, audit

    def test_admin_reads_and_the_read_is_audited(self):
        response, audit = self.get("bank_id=BNK1&premium_bps=100&proceeds=500&general_claims=0")
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(response["value"]["coveredMinorUnits"], 600)
        self.assertEqual(audit.call_args[0][1], "deposit_insurance.preview")

    def test_non_admin_and_bad_filters_are_refused_without_audit(self):
        response, audit = self.get("bank_id=BNK1", role="user")
        self.assertEqual(response["status"], 403)
        for query in ("", "bank_id=BNK1&premium_bps=x", "bank_id=BNK1&premium_bps=10001", "bank_id=BNK1&proceeds=-1", "bank_id=BNK1&general_claims=1e3"):
            response, audit = self.get(query)
            self.assertEqual(response["status"], 400, query)
            audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class DepositInsuranceFundApiTests(Fixture):
    def setUp(self):
        super().setUp()
        self.db.users.create_user("admin", "Admin", "낙원", 0, role="admin")

    def call(self, method, payload=None, role="admin"):
        body = json.dumps(payload or {}).encode()
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "admin", "content-length": str(len(body))},
                                  path="/internal/admin/deposit-insurance-fund", rfile=io.BytesIO(body),
                                  send_json=lambda status, value: response.update(status=status, value=value))
        db = self.db
        with patch.object(server, "INTERNAL_TOKEN", "secret"), patch.object(server, "identity", return_value={"userId": "admin", "role": role}), \
             patch.object(server, "domain", return_value=SimpleNamespace(db=db)), patch.object(server, "audit_admin_access", audit), \
             patch.object(server, "connect_readonly", lambda: db.readonly()), patch.object(server, "LegacyNames", lambda c: c), \
             patch("functions.banks.bank_issuer", lambda c, bank_id: "Bank"):
            getattr(server.Handler, method)(handler)
        return response, audit

    PAYLOAD = {"action": "collect_premium", "bankId": "BNK1", "premiumBps": 250, "eventKey": "api-premium-0001"}

    def test_admin_collects_once_and_reads_with_audit(self):
        response, audit = self.call("do_POST", self.PAYLOAD)
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args[0][1], "deposit_insurance_fund.collect_premium")
        again, _ = self.call("do_POST", self.PAYLOAD)
        self.assertTrue(again["value"]["result"]["duplicate"])
        response, audit = self.call("do_GET")
        self.assertEqual(response["value"]["fund"]["NWX"]["balanceMinorUnits"], 15)
        self.assertEqual(response["value"]["problems"], [])
        self.assertEqual(audit.call_args[0][1], "deposit_insurance_fund.read")

    def test_admin_opts_a_jurisdiction_in_with_audit_and_bad_policy_is_refused(self):
        policy = {"action": "set_policy", "nationality": "낙원", "autoRecord": True}
        response, audit = self.call("do_POST", policy)
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args[0][1], "deposit_insurance_fund.set_policy")
        self.assertEqual(self.db.currency.execute_one("SELECT auto_record FROM deposit_insurance_fund_policy WHERE nationality='낙원'")[0], 1)
        for bad in (dict(policy, autoRecord="yes"), dict(policy, nationality="없는나라"), dict(policy, extra=1)):
            response, audit = self.call("do_POST", bad)
            self.assertEqual(response["status"], 400, bad)
            audit.assert_not_called()
        self.assertEqual(self.call("do_POST", policy, role="user")[0]["status"], 403)

    def test_backstop_policy_and_cover_through_the_api_are_audited_and_validated(self):
        self.db.accounts.execute("INSERT INTO deposit_insurance_claims(issuer,user_id,account_number,covered_amount,uninsured_amount,payout_currency,payout_amount,"
                                 "payout_amount_minor_units,payout_decimal_places,resolved_epoch) VALUES ('Bank','u1','A1',300,0,'NWX',300,300,0,1800000000)")
        self.assertEqual(self.call("do_POST", {"action": "record_payouts", "bankId": "BNK1"})[0]["status"], 200)
        policy = {"action": "set_backstop_policy", "nationality": "낙원", "enabled": True, "limitMinorUnits": 250}
        response, audit = self.call("do_POST", policy)
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args[0][1], "deposit_insurance_fund.set_backstop_policy")
        cover = {"action": "cover_shortfall", "nationality": "낙원", "eventKey": "api-cover-0001"}
        response, audit = self.call("do_POST", cover)
        self.assertEqual(response["value"]["result"]["amountMinorUnits"], 250)
        self.assertEqual(audit.call_args[0][1], "deposit_insurance_fund.cover_shortfall")
        report = self.call("do_GET")[0]["value"]
        self.assertEqual([p["backstop_used_minor_units"] for p in report["policies"] if p["nationality"] == "낙원"], [250])
        for bad in (dict(policy, enabled="yes"), dict(policy, limitMinorUnits=-1), dict(policy, extra=1), dict(cover, eventKey="short"), dict(cover, nationality="없는나라")):
            response, audit = self.call("do_POST", bad)
            self.assertEqual(response["status"], 400, bad)
            audit.assert_not_called()
        self.assertEqual(self.call("do_POST", cover, role="user")[0]["status"], 403)

    def test_non_admin_and_bad_payloads_are_refused_without_writing_or_audit(self):
        self.assertEqual(self.call("do_POST", self.PAYLOAD, role="user")[0]["status"], 403)
        self.assertEqual(self.call("do_GET", role="user")[0]["status"], 403)
        for bad in (dict(self.PAYLOAD, extra=1), dict(self.PAYLOAD, premiumBps=0), dict(self.PAYLOAD, premiumBps="5"), dict(self.PAYLOAD, premiumBps=True),
                    dict(self.PAYLOAD, eventKey="short"), dict(self.PAYLOAD, action="x")):
            response, audit = self.call("do_POST", bad)
            self.assertEqual(response["status"], 400, bad)
            audit.assert_not_called()
        self.assertEqual(self.db.currency.execute_one("SELECT COUNT(*) FROM deposit_insurance_fund_events")[0], 0)


class FundAutoRecordTests(Fixture):
    NOW = 1_800_000_000

    def fail_and_resolve(self):
        self.db.currency.execute("UPDATE financial_institutions SET status='failed' WHERE issuer='Bank'")
        from managers.bank_manager import BankManager
        with self.db.atomic() as conn:
            return BankManager.resolve_failed_institution(conn, "Bank", now=self.NOW)

    def events(self):
        return [tuple(r) for r in self.db.currency.execute("SELECT event_key,kind,amount_minor_units FROM deposit_insurance_fund_events ORDER BY event_key")]

    def test_off_by_default_resolution_records_nothing_in_the_fund(self):
        self.fail_and_resolve()
        self.assertEqual(self.events(), [])
        self.assertGreater(self.db.accounts.execute_one("SELECT COUNT(*) FROM deposit_insurance_claims")[0], 0)

    def test_opted_in_resolution_records_each_claim_payout_once_and_reconciles(self):
        self.db.users.create_user("admin", "Admin", "낙원", 0, role="admin")
        with self.db.atomic() as conn:
            fund.set_auto_record(conn, "낙원", True, actor_id="admin", now=self.NOW)
        self.fail_and_resolve()
        paid = [tuple(r) for r in self.db.accounts.execute("SELECT claim_id,payout_amount_minor_units FROM deposit_insurance_claims WHERE payout_amount_minor_units>0")]
        self.assertTrue(paid)
        self.assertEqual(sorted((f"claim:{c}", "payout", a) for c, a in paid), self.events())
        with self.db.atomic() as conn:                 # running the recorder again (or the manual action) adds nothing
            self.assertEqual(fund.auto_record_if_enabled(conn, "Bank", now=self.NOW), 0)
            self.assertEqual(fund.record_claim_payouts(conn, "Bank", actor_id="admin", now=self.NOW)["recorded"], 0)
            self.assertEqual(fund.reconcile(conn), [])

    def test_the_policy_needs_an_admin_and_a_known_country(self):
        self.db.users.create_user("admin", "Admin", "낙원", 0, role="admin")
        with self.db.atomic() as conn:
            for args in (dict(nationality="없는나라", enabled=True, actor_id="admin"), dict(nationality="낙원", enabled="yes", actor_id="admin"),
                         dict(nationality="낙원", enabled=True, actor_id="u1")):
                with self.assertRaises(ValueError):
                    fund.set_auto_record(conn, now=self.NOW, **args)


class FundBackstopTests(Fixture):
    NOW = 1_800_000_000

    def setUp(self):
        super().setUp()
        self.db.users.create_user("admin", "Admin", "낙원", 0, role="admin")
        self.db.accounts.execute("INSERT INTO deposit_insurance_claims(issuer,user_id,account_number,covered_amount,uninsured_amount,payout_currency,payout_amount,"
                                 "payout_amount_minor_units,payout_decimal_places,resolved_epoch) VALUES ('Bank','u1','A1',300,0,'NWX',300,300,0,?)", (self.NOW,))
        with self.db.atomic() as conn:
            fund.record_claim_payouts(conn, "Bank", actor_id="admin", now=self.NOW)         # fund balance -300

    def policy(self, conn, enabled=True, limit=200):
        fund.set_backstop_policy(conn, "낙원", enabled, limit, actor_id="admin", now=self.NOW)

    def treasury_cash(self):
        from managers.currency_manager import CurrencyManager
        with self.db.readonly() as conn:
            return CurrencyManager.balance(conn, CurrencyManager.treasury_id(conn, "낙원"), "NWX")

    def test_off_by_default_nothing_is_covered(self):
        with self.db.atomic() as conn:
            with self.assertRaises(ValueError):
                fund.cover_shortfall(conn, "낙원", event_key="cover-0001-aaaa", actor_id="admin", now=self.NOW)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deposit_insurance_backstops").fetchone()[0], 0)

    def test_cover_is_capped_paid_from_the_treasury_recorded_once_and_reduces_the_shortfall(self):
        before = self.treasury_cash()
        with self.db.atomic() as conn:
            self.policy(conn, limit=200)
            first = fund.cover_shortfall(conn, "낙원", event_key="cover-0001-aaaa", actor_id="admin", now=self.NOW)
            again = fund.cover_shortfall(conn, "낙원", event_key="cover-0001-aaaa", actor_id="admin", now=self.NOW)
            overview = fund.fund_overview(conn)["NWX"]
        self.assertEqual((first["amountMinorUnits"], first["duplicate"], again["duplicate"]), (200, False, True))   # shortfall 300, cap 200
        self.assertEqual((overview["balanceMinorUnits"], overview["shortfallMinorUnits"], overview["backstopsMinorUnits"]), (-100, 100, 200))
        self.assertEqual(before - self.treasury_cash(), 200 * 10 ** 6)
        with self.db.atomic() as conn, self.assertRaises(ValueError):     # the cap is used up
            fund.cover_shortfall(conn, "낙원", event_key="cover-0002-bbbb", actor_id="admin", now=self.NOW)
        with self.db.atomic() as conn:
            self.assertEqual(fund.reconcile(conn), [])

    def test_a_failing_payment_rolls_back_and_a_retry_works(self):
        before = self.treasury_cash()
        from managers.currency_manager import CurrencyManager
        with self.db.atomic() as conn:
            self.policy(conn)
            with patch.object(CurrencyManager, "treasury_pay", side_effect=RuntimeError("wallet failed")), self.assertRaises(RuntimeError):
                fund.cover_shortfall(conn, "낙원", event_key="cover-0001-aaaa", actor_id="admin", now=self.NOW)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deposit_insurance_backstops").fetchone()[0], 0)
            self.assertEqual(fund.cover_shortfall(conn, "낙원", event_key="cover-0001-aaaa", actor_id="admin", now=self.NOW)["amountMinorUnits"], 200)
        self.assertEqual(before - self.treasury_cash(), 200 * 10 ** 6)

    def test_policy_and_cover_need_an_admin_and_valid_input_and_the_record_is_append_only(self):
        with self.db.atomic() as conn:
            for args in (("낙원", True, 5), ("없는나라", True, 5), ("낙원", "yes", 5), ("낙원", True, -1), ("낙원", True, True)):
                with self.assertRaises(ValueError):
                    fund.set_backstop_policy(conn, *args, actor_id="admin" if args != ("낙원", True, 5) else "u1", now=self.NOW)
            self.policy(conn)
            with self.assertRaises(ValueError):
                fund.cover_shortfall(conn, "낙원", event_key="short", actor_id="admin", now=self.NOW)
            with self.assertRaises(ValueError):
                fund.cover_shortfall(conn, "낙원", event_key="cover-0001-aaaa", actor_id="u1", now=self.NOW)
            fund.cover_shortfall(conn, "낙원", event_key="cover-0001-aaaa", actor_id="admin", now=self.NOW)
        for sql in ("UPDATE deposit_insurance_backstops SET amount_minor_units=1", "DELETE FROM deposit_insurance_backstops"):
            with self.assertRaises(Exception), self.db.atomic() as conn:
                conn.execute(sql)
