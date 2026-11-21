"""M6: wholesale funding administration: overview numbers, per-bank maturity processing, and the admin API (permission, validation, audit)."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.funding_manager import FundingManager
from managers.funding_maturity import process_due_funding
from web_api import server

DAY = 86400
NOW = 2_000_000_000


class FundingAdminBase(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        for issuer, bank_id in (("Bank", "BNK1"), ("Other", "BNK2")):
            self.db.currency.execute("INSERT INTO financial_institutions(issuer,nationality,institution_id) VALUES (?,?,?)",
                                     (issuer, "낙원", bank_id))

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def fund(self, conn, issuer="Bank", source="interbank", amount=1000, days=10, key=None, **extra):
        FundingManager.set_limit(conn, issuer, source, "NWX", 100000, actor="admin", now=NOW)
        return FundingManager.draw(conn, issuer, source, "NWX", amount, reference_key=key or f"{issuer}-{source}-{days}",
                                   rate_bps=365, maturity_days=days, now=NOW, **extra)


class FundingOverviewTests(FundingAdminBase):
    def test_limits_show_what_is_drawn_and_what_is_left(self):
        with self.db.atomic() as conn:
            FundingManager.set_limit(conn, "Bank", "interbank", "NWX", 5000, actor="admin", now=NOW)
            self.fund(conn, source="repo", amount=300, days=100, collateral=400)
            FundingManager.set_limit(conn, "Bank", "interbank", "NWX", 5000, actor="admin", now=NOW)
            FundingManager.draw(conn, "Bank", "interbank", "NWX", 1000, reference_key="a", rate_bps=365, maturity_days=10, now=NOW)
            report = FundingManager.overview(conn, "Bank", now=NOW + 11 * DAY)
        limits = {(row["source_type"], row["currency_code"]): row for row in report["limits"]}
        interbank = limits[("interbank", "NWX")]
        self.assertEqual((interbank["limit_minor_units"], interbank["outstanding_minor_units"], interbank["available_minor_units"]), (5000, 1000, 4000))
        self.assertEqual(limits[("repo", "NWX")]["outstanding_minor_units"], 300)

    def test_draws_are_newest_first_and_overdue_is_flagged(self):
        with self.db.atomic() as conn:
            first = self.fund(conn, days=10)
            second = self.fund(conn, source="repo", days=100, collateral=2000)
            report = FundingManager.overview(conn, "Bank", now=NOW + 11 * DAY)
        self.assertEqual([row["funding_id"] for row in report["draws"]], [second, first])
        by_id = {row["funding_id"]: row for row in report["draws"]}
        self.assertTrue(by_id[first]["overdue"])
        self.assertFalse(by_id[second]["overdue"])
        self.assertEqual(report["overdue_count"], 1)
        self.assertEqual(by_id[first]["principal_minor_units"], 1000)
        self.assertEqual(report["profile"]["currencies"]["NWX"]["outstanding_minor_units"], 2000)

    def test_a_bank_sees_only_its_own_funding(self):
        with self.db.atomic() as conn:
            self.fund(conn, "Bank", key="own")
            self.fund(conn, "Other", key="theirs", amount=555)
            report = FundingManager.overview(conn, "Bank", now=NOW)
        self.assertEqual([row["principal_minor_units"] for row in report["draws"]], [1000])
        self.assertEqual([row["currency_code"] for row in report["limits"]], ["NWX"])

    def test_repaid_draws_stay_in_the_history_but_not_in_what_is_outstanding(self):
        with self.db.atomic() as conn:
            funding_id = self.fund(conn)
            FundingManager.repay(conn, funding_id, now=NOW + 5 * DAY)
            report = FundingManager.overview(conn, "Bank", now=NOW + 6 * DAY)
        self.assertEqual(report["draws"][0]["status"], "repaid")
        self.assertEqual(report["limits"][0]["outstanding_minor_units"], 0)
        self.assertEqual(report["overdue_count"], 0)


class ProcessDueForOneBankTests(FundingAdminBase):
    def test_only_the_named_bank_is_processed(self):
        with self.db.atomic() as conn:
            mine = self.fund(conn, "Bank", key="m")
            theirs = self.fund(conn, "Other", key="t")
            result = process_due_funding(conn, now=NOW + 11 * DAY, issuer="Bank")
            status = {row["funding_id"]: row["status"] for row in conn.execute("SELECT funding_id,status FROM institution_funding")}
        self.assertEqual([row["funding_id"] for row in result["repaid"]], [mine])
        self.assertEqual((status[mine], status[theirs]), ("repaid", "active"))

    def test_without_a_bank_every_due_row_is_processed(self):
        with self.db.atomic() as conn:
            self.fund(conn, "Bank", key="m")
            self.fund(conn, "Other", key="t")
            result = process_due_funding(conn, now=NOW + 11 * DAY)
        self.assertEqual(len(result["repaid"]), 2)


class FundingAdminApiTests(FundingAdminBase):
    def get(self, path, role="admin"):
        result = {}
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "a"}, path=path,
                                  send_json=lambda status, value: result.update(status=status, value=value))
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
             patch.object(server, "identity", return_value={"userId": "a", "role": role}), \
             patch.object(server, "admin_funding_overview", return_value={"draws": [1, 2], "overdue_count": 1}) as report, \
             patch.object(server, "audit_admin_access") as audit:
            server.Handler.do_GET(handler)
        return result, report, audit

    def test_admin_reads_and_the_read_is_audited(self):
        response, report, audit = self.get("/internal/admin/funding?bank_id=bnk1")
        self.assertEqual(response["status"], 200)
        report.assert_called_once_with("BNK1")
        audit.assert_called_once_with("a", "funding.read", {"bank_id": "BNK1", "draws": 2, "overdue": 1})

    def test_non_admin_and_bad_filters_are_rejected_without_read_or_audit(self):
        for path, role, status in (("/internal/admin/funding?bank_id=BNK1", "user", 403), ("/internal/admin/funding", "admin", 400),
                                   ("/internal/admin/funding?bank_id=" + "x" * 101, "admin", 400)):
            with self.subTest(path=path[:40], role=role):
                response, report, audit = self.get(path, role)
                self.assertEqual(response["status"], status)
                report.assert_not_called()
                audit.assert_not_called()

    def post(self, payload, role="admin"):
        body = json.dumps(payload).encode()
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "a", "content-length": str(len(body))},
                                  path="/internal/admin/funding", rfile=io.BytesIO(body),
                                  send_json=lambda status, value: response.update(status=status, value=value))
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
             patch.object(server, "identity", return_value={"userId": "a", "role": role}), \
             patch.object(server, "domain", return_value=SimpleNamespace(db=self.db)), \
             patch.object(server, "audit_admin_access", audit):
            server.Handler.do_POST(handler)
        return response, audit

    DRAW = {"action": "draw", "bankId": "BNK1", "sourceType": "interbank", "currency": "NWX", "amount": "1000", "rateBps": 365,
            "maturityDays": 10, "referenceKey": "ui-0001-abcdef", "counterparty": "Other Bank", "collateral": "0", "haircutBps": 0}

    def test_limit_then_draw_then_repay_work_end_to_end_and_are_audited(self):
        response, audit = self.post({"action": "set_limit", "bankId": "BNK1", "sourceType": "interbank", "currency": "NWX", "limit": "5000"})
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args.args[1], "funding.limit_set")
        response, audit = self.post(self.DRAW)
        self.assertEqual(response["status"], 200, response)
        funding_id = response["value"]["result"]["fundingId"]
        self.assertEqual(audit.call_args.args[1], "funding.draw")
        self.assertEqual(audit.call_args.args[2]["funding_id"], funding_id)
        again, _ = self.post(self.DRAW)                                   # the same reference key and terms: no second draw
        self.assertEqual(again["value"]["result"]["fundingId"], funding_id)
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM institution_funding").fetchone()[0], 1)
        response, audit = self.post({"action": "repay", "bankId": "BNK1", "fundingId": funding_id})
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args.args[1], "funding.repay")
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT status FROM institution_funding WHERE funding_id=?", (funding_id,)).fetchone()[0], "repaid")

    def test_process_due_repays_only_that_banks_due_rows(self):
        with self.db.atomic() as conn:
            self.fund(conn, "Bank", days=1, key="m")
            self.fund(conn, "Other", days=1, key="t")
        with patch("web_api.server.time.time", return_value=NOW + 3 * DAY):
            response, audit = self.post({"action": "process_due", "bankId": "BNK1"})
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(len(response["value"]["result"]["repaid"]), 1)
        self.assertEqual(audit.call_args.args[1], "funding.process_due")
        with self.db.readonly() as conn:
            rows = {row["issuer"]: row["status"] for row in conn.execute("SELECT issuer,status FROM institution_funding")}
        self.assertEqual(rows, {"Bank": "repaid", "Other": "active"})

    def test_a_bank_cannot_repay_another_banks_funding(self):
        with self.db.atomic() as conn:
            theirs = self.fund(conn, "Other", key="t")
        response, audit = self.post({"action": "repay", "bankId": "BNK1", "fundingId": theirs})
        self.assertEqual(response["status"], 400)
        audit.assert_not_called()
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT status FROM institution_funding WHERE funding_id=?", (theirs,)).fetchone()[0], "active")

    def test_non_admin_is_refused_and_nothing_is_written_or_audited(self):
        response, audit = self.post(self.DRAW, role="user")
        self.assertEqual(response["status"], 403)
        audit.assert_not_called()
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM institution_funding").fetchone()[0], 0)

    def test_bad_payloads_are_rejected_without_writing_or_auditing(self):
        self.post({"action": "set_limit", "bankId": "BNK1", "sourceType": "interbank", "currency": "NWX", "limit": "5000"})
        bad = [dict(self.DRAW, action="unknown"), dict(self.DRAW, extra=1), {k: v for k, v in self.DRAW.items() if k != "amount"},
               dict(self.DRAW, amount=1000), dict(self.DRAW, amount="-5"), dict(self.DRAW, amount="1e3"), dict(self.DRAW, amount="1.2345678"),
               dict(self.DRAW, rateBps=3.5), dict(self.DRAW, rateBps=-1), dict(self.DRAW, rateBps=True), dict(self.DRAW, maturityDays=0),
               dict(self.DRAW, maturityDays=36501), dict(self.DRAW, haircutBps=10001), dict(self.DRAW, referenceKey="short"),
               dict(self.DRAW, referenceKey="has space in it!"), dict(self.DRAW, sourceType="loan_shark"), dict(self.DRAW, currency="N W"),
               dict(self.DRAW, counterparty="x" * 101), dict(self.DRAW, bankId="NOPE"), dict(self.DRAW, amount="999999999")]   # over the limit
        for payload in bad:
            with self.subTest(payload={k: v for k, v in payload.items() if self.DRAW.get(k) != v}):
                response, audit = self.post(payload)
                self.assertEqual(response["status"], 400, response)
                audit.assert_not_called()
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM institution_funding").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
