"""M7: optional mixed/stepped rate schedules on new loan and deposit contracts (off by default); signed contracts never change."""
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_manager import BankManager
from managers.interest_manager import InterestManager
from managers.rate_manager import RateManager
from managers.rate_schedule_contracts import set_product_schedule, snapshot

T0 = 1_800_000_000
DAY = 86400
SCHEDULE = {"segments": [{"from_day": 0, "kind": "fixed", "rate": "12"}, {"from_day": 10, "kind": "floating", "spread": "1"}],
            "floor": None, "cap": "20", "max_change_per_reset": None, "rounding_step": None}


class ScheduleContractTests(unittest.TestCase):
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
            "VALUES ('LS','LS',6,1000000,365,86400,0,'Bank','NWX','fixed','낙원',0,86400,'annuity',30)")
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,compound_type,issuer,quote_currency,"
            "rate_type,rate_nationality,base_rate_spread,repricing_cycle_seconds) VALUES ('SS','SS',5,1,365,86400,1,'Bank','NWX','fixed','낙원',0,86400)")
        self.bank = BankManager(self.db)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def enable(self, product_type, code, enabled=True):
        with self.db.atomic() as conn:
            set_product_schedule(conn, product_type=product_type, product_code=code, schedule=SCHEDULE, enabled=enabled, actor="admin", now=T0)

    def test_without_an_enabled_schedule_nothing_changes(self):
        self.assertTrue(self.bank.apply_loan("user", "LS", 100_000)[0])
        loan = self.db.accounts.execute_one("SELECT contract_rate,rate_type FROM loans")
        self.assertEqual((loan["contract_rate"], loan["rate_type"]), (6.0, "fixed"))
        self.enable("loan", "LS", enabled=False)
        self.assertTrue(self.bank.open_account("user", "SS", "1234", 1000)[0])
        self.assertEqual(self.db.accounts.execute_one("SELECT COUNT(*) FROM contract_rate_schedules")[0], 0)

    def test_new_loan_takes_day0_rate_and_snapshot_and_a_signed_loan_is_unchanged(self):
        self.assertTrue(self.bank.apply_loan("user", "LS", 100_000)[0])          # signed before the schedule exists
        before = tuple(self.db.accounts.execute_one("SELECT contract_rate,rate_type,next_repricing_epoch FROM loans"))
        self.enable("loan", "LS")
        self.db.users.create_user("user2", "User2", "낙원", 5_000_000)
        self.assertTrue(self.bank.apply_loan("user2", "LS", 100_000)[0])
        old = tuple(self.db.accounts.execute_one("SELECT contract_rate,rate_type,next_repricing_epoch FROM loans WHERE user_id='user'"))
        new = self.db.accounts.execute_one("SELECT loan_id,contract_rate,rate_type,next_repricing_epoch,created_at FROM loans WHERE user_id='user2'")
        self.assertEqual(old, before)
        self.assertEqual((new["contract_rate"], new["rate_type"]), (12.0, "floating"))
        with self.db.readonly() as conn:
            schedule, start = snapshot(conn, "loan", new["loan_id"])
            self.assertEqual(new["next_repricing_epoch"], start + 10 * DAY)
            self.assertIsNone(snapshot(conn, "loan", self.db.accounts.execute_one("SELECT loan_id FROM loans WHERE user_id='user'")[0]))
        self.enable("loan", "LS", enabled=False)       # editing the product later never touches the snapshot
        with self.db.readonly() as conn:
            self.assertIsNotNone(snapshot(conn, "loan", new["loan_id"]))

    def test_the_rate_changes_at_the_segment_boundary_and_the_table_is_recast_and_recorded(self):
        self.enable("loan", "LS")
        self.assertTrue(self.bank.apply_loan("user", "LS", 100_000)[0])
        loan = self.db.accounts.execute_one("SELECT loan_id FROM loans")
        start = self.db.accounts.execute_one("SELECT start_epoch FROM contract_rate_schedules")[0]
        self.db.accounts.execute("UPDATE loans SET last_payment_epoch=?,maturity_epoch=?", (start, start + 365 * DAY))
        InterestManager(self.db).process_user_interests(now=start + 30 * DAY, user_id="user")
        row = self.db.accounts.execute_one("SELECT contract_rate,next_repricing_epoch,outstanding_balance_minor_units FROM loans")
        history = [tuple(r) for r in self.db.accounts.execute("SELECT old_rate,new_rate,scheduled_epoch,installments_recast FROM contract_rate_reset_history")]
        self.assertTrue(history and history[0][0] == 12.0 and history[0][2] == start + 10 * DAY)
        with self.db.readonly() as conn:
            expected = RateManager.market_rate(conn, "낙원", "policy", 365, epoch=start + 10 * DAY) + 1
        self.assertAlmostEqual(row["contract_rate"], min(20.0, expected), places=9)
        self.assertGreater(row["outstanding_balance_minor_units"], 100_000)

    def test_the_holder_is_told_once_per_rate_change_and_plain_floating_contracts_are_not(self):
        self.enable("loan", "LS")
        self.assertTrue(self.bank.apply_loan("user", "LS", 100_000)[0])
        start = self.db.accounts.execute_one("SELECT start_epoch FROM contract_rate_schedules")[0]
        self.db.accounts.execute("UPDATE loans SET last_payment_epoch=?,maturity_epoch=?", (start, start + 365 * DAY))
        InterestManager(self.db).process_user_interests(now=start + 30 * DAY, user_id="user")
        rows = [tuple(r) for r in self.db.users.execute("SELECT kind,title FROM notifications WHERE user_id='user' AND kind='rate_schedule'")]
        self.assertEqual(len(rows), 1)
        InterestManager(self.db).process_user_interests(now=start + 31 * DAY, user_id="user")
        self.assertEqual(self.db.users.execute_one("SELECT COUNT(*) FROM notifications WHERE kind='rate_schedule'")[0], 1)

    def test_a_deposit_follows_the_schedule_too(self):
        self.enable("deposit", "SS")
        self.assertTrue(self.bank.open_account("user", "SS", "1234", 100_000)[0])
        account = self.db.accounts.execute_one("SELECT account_number,contract_rate,rate_type FROM accounts")
        self.assertEqual((account["contract_rate"], account["rate_type"]), (12.0, "floating"))
        start = self.db.accounts.execute_one("SELECT start_epoch FROM contract_rate_schedules")[0]
        self.db.accounts.execute("UPDATE accounts SET last_payment_epoch=?,created_at=datetime(?,'unixepoch')", (start, start))
        InterestManager(self.db).process_user_interests(now=start + 30 * DAY, user_id="user")
        after = self.db.accounts.execute_one("SELECT balance,contract_rate FROM accounts")
        self.assertGreater(after["balance"], 100_000)
        self.assertNotEqual(after["contract_rate"], 12.0)
        self.assertEqual(self.db.accounts.execute_one("SELECT COUNT(*) FROM contract_rate_reset_history")[0] > 0, True)

    def test_bad_schedules_and_unknown_products_are_refused(self):
        with self.db.atomic() as conn:
            for bad in ({"segments": []}, {"segments": [{"from_day": 5, "kind": "fixed", "rate": "1"}]},
                        dict(SCHEDULE, floor="30", cap="20")):
                with self.assertRaises(ValueError):
                    set_product_schedule(conn, product_type="loan", product_code="LS", schedule=bad, enabled=True, actor="admin", now=T0)
            with self.assertRaises(ValueError):
                set_product_schedule(conn, product_type="loan", product_code="NOPE", schedule=SCHEDULE, enabled=True, actor="admin", now=T0)

    def test_the_snapshot_is_append_only(self):
        self.enable("loan", "LS")
        self.assertTrue(self.bank.apply_loan("user", "LS", 100_000)[0])
        for sql in ("UPDATE contract_rate_schedules SET schedule_json='{}'", "DELETE FROM contract_rate_schedules"):
            with self.assertRaises(Exception), self.db.atomic() as conn:
                conn.execute(sql)


class ScheduleAdminApiTests(unittest.TestCase):
    setUp = ScheduleContractTests.setUp
    tearDown = ScheduleContractTests.tearDown

    def call(self, method, payload=None, role="admin", query=""):
        import io, json
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from web_api import server
        body = json.dumps(payload or {}).encode()
        response, audit = {}, Mock()
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "admin", "content-length": str(len(body))},
                                  path="/internal/admin/product-rate-schedule" + query, rfile=io.BytesIO(body),
                                  send_json=lambda status, value: response.update(status=status, value=value))
        db = self.db
        with patch.object(server, "INTERNAL_TOKEN", "secret"), patch.object(server, "identity", return_value={"userId": "admin", "role": role}), \
             patch.object(server, "domain", return_value=SimpleNamespace(db=db)), patch.object(server, "audit_admin_access", audit), \
             patch.object(server, "connect_readonly", lambda: db.readonly()), patch.object(server, "LegacyNames", lambda c: c):
            getattr(server.Handler, method)(handler)
        return response, audit

    PAYLOAD = {"action": "set", "productType": "loan", "productCode": "LS", "schedule": SCHEDULE, "enabled": True}

    def test_admin_enables_lists_and_it_is_audited(self):
        response, audit = self.call("do_POST", self.PAYLOAD)
        self.assertEqual(response["status"], 200, response)
        self.assertEqual(audit.call_args[0][1], "product_rate_schedule.set")
        response, audit = self.call("do_GET")
        self.assertEqual(response["value"]["schedules"][0]["product_code"], "LS")
        self.assertEqual(audit.call_args[0][1], "product_rate_schedule.read")

    def test_non_admin_and_bad_payloads_are_refused_without_writing_or_audit(self):
        response, audit = self.call("do_POST", self.PAYLOAD, role="user")
        self.assertEqual(response["status"], 403)
        for bad in (dict(self.PAYLOAD, extra=1), dict(self.PAYLOAD, enabled="yes"), dict(self.PAYLOAD, productType="swap"),
                    dict(self.PAYLOAD, productCode="NOPE"), dict(self.PAYLOAD, schedule={"segments": []}), dict(self.PAYLOAD, action="x")):
            response, audit = self.call("do_POST", bad)
            self.assertEqual(response["status"], 400, bad)
            audit.assert_not_called()
        self.assertEqual(self.db.products.execute_one("SELECT COUNT(*) FROM product_rate_schedules")[0], 0)


if __name__ == "__main__":
    unittest.main()
