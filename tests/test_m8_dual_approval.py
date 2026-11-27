"""M8 two-person approval of high-risk admin writes, the administrator role-change record, and the static check that
server.py routes the high-risk actions through the gate. Off by default; every table is append-only."""
import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import DatabaseManager
from database.db_manager import DBManager
from functions import admin_approval as approval
from managers.admin_control_manager import AdminControlManager

ROOT = Path(__file__).resolve().parents[1]


class DualApprovalTests(unittest.TestCase):
    def setUp(self):
        self.old = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        for name in ("adm1", "adm2"):
            self.db.users.create_user(name, name, "Nac", 0, role="admin")
        self.db.users.create_user("citizen", "citizen", "Nac", 0)
        self.path, self.body = "/internal/admin/funding", {"action": "repay", "bankId": "B", "fundingId": 1}

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old
        self.tmp.cleanup()

    def conn(self):
        return self.db.atomic(("users", "system"))

    def turn_on(self):
        with self.conn() as conn:
            approval.set_switch(conn, "adm1", True)

    def request(self, body=None):
        with self.conn() as conn:
            state, request_id = approval.gate(conn, "adm1", self.path, dict(body or self.body))
        self.assertEqual(state, "needs")
        return request_id

    def test_risk_actions_are_recognised_by_path_and_payload(self):
        self.assertEqual(approval.risk_action("/internal/admin/funding", {"action": "repay"}), "funding.repay")
        self.assertEqual(approval.risk_action("/internal/admin/db/tables/t/rows/3", {}), "db.update")
        self.assertEqual(approval.risk_action("/internal/admin/db/tables/t/rows/3/delete", {}), "db.delete")
        self.assertEqual(approval.risk_action("/internal/admin/users/u1", {"role": "admin"}), "user.role")
        self.assertEqual(approval.risk_action("/internal/admin/users/u1", {"nickname": "x"}), None)
        # the administrator UI posts {"changes": {...}} to /internal/admin/user/<id>
        self.assertEqual(approval.risk_action("/internal/admin/user/u1", {"changes": {"role": "admin"}}), "user.role")
        self.assertEqual(approval.risk_action("/internal/admin/user/u1", {"changes": {"cash": "5"}}), "user.cash")
        self.assertIsNone(approval.risk_action("/internal/admin/user/u1", {"changes": {"nickname": "n"}}))
        self.assertIsNone(approval.risk_action("/internal/admin/user/u1/web", {"action": "logout"}))
        self.assertEqual(approval.risk_action("/internal/admin/compliance", {"action": "prudential_parameter_set", "key": "ftp_apply_enabled"}), "prudential_parameter.set")
        self.assertIsNone(approval.risk_action("/internal/admin/compliance", {"action": "prudential_parameter_set", "key": "lcr_floor_bps"}))
        self.assertEqual(approval.risk_action("/internal/admin/approvals", {"action": "set_switch", "enabled": False}), "approval.switch_off")
        self.assertIsNone(approval.risk_action("/internal/admin/approvals", {"action": "set_switch", "enabled": True}))

    def test_switch_off_changes_nothing_and_strips_the_approval_id(self):
        body = {**self.body, "approvalId": 5}
        with self.conn() as conn:
            self.assertEqual(approval.gate(conn, "adm1", self.path, body), ("ok", None))
        self.assertNotIn("approvalId", body)
        with self.conn() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM admin_approval_requests").fetchone()[0], 0)

    def test_turning_it_on_needs_two_administrators(self):
        self.db.users.update_role("adm2", "user")
        with self.assertRaisesRegex(ValueError, "two_administrators"):
            with self.conn() as conn:
                approval.set_switch(conn, "adm1", True)

    def test_full_flow_approve_then_use_once(self):
        self.turn_on()
        request_id = self.request()
        with self.conn() as conn:
            with self.assertRaisesRegex(ValueError, "not_granted"):
                approval.gate(conn, "adm1", self.path, {**self.body, "approvalId": request_id})
            with self.assertRaisesRegex(ValueError, "requester_cannot_decide"):
                approval.decide(conn, "adm1", request_id, "approve")
        with self.conn() as conn:
            with self.assertRaisesRegex(ValueError, "must_be_administrator"):
                approval.decide(conn, "citizen", request_id, "approve")
        with self.conn() as conn:
            approval.decide(conn, "adm2", request_id, "approve", "ok")
        with self.conn() as conn:
            with self.assertRaisesRegex(ValueError, "already_decided"):
                approval.decide(conn, "adm2", request_id, "approve")
            with self.assertRaisesRegex(ValueError, "does_not_match"):
                approval.gate(conn, "adm1", self.path, {**self.body, "fundingId": 2, "approvalId": request_id})     # another payload
            with self.assertRaisesRegex(ValueError, "does_not_match"):
                approval.gate(conn, "adm2", self.path, {**self.body, "approvalId": request_id})                     # another requester
            state, used = approval.gate(conn, "adm1", self.path, {**self.body, "approvalId": request_id})
            self.assertEqual((state, used, approval.current_approver()), ("ok", request_id, "adm2"))
        with self.conn() as conn:
            approval.consume(conn, request_id, "adm1")
        with self.conn() as conn:
            with self.assertRaisesRegex(ValueError, "already_used"):
                approval.gate(conn, "adm1", self.path, {**self.body, "approvalId": request_id})

    def test_rejected_and_expired_requests_cannot_be_used(self):
        self.turn_on()
        rejected = self.request()
        with self.conn() as conn:
            approval.decide(conn, "adm2", rejected, "reject")
        with self.conn() as conn:
            with self.assertRaisesRegex(ValueError, "not_granted"):
                approval.gate(conn, "adm1", self.path, {**self.body, "approvalId": rejected})
        old = self.request()
        with self.conn() as conn:
            approval.decide(conn, "adm2", old, "approve")
        with self.conn() as conn:
            with self.assertRaisesRegex(ValueError, "expired"):
                approval.gate(conn, "adm1", self.path, {**self.body, "approvalId": old}, now=9e12)

    def test_an_approver_who_lost_admin_rights_no_longer_counts(self):
        self.turn_on()
        request_id = self.request()
        with self.conn() as conn:
            approval.decide(conn, "adm2", request_id, "approve")
        self.db.users.update_role("adm2", "user")
        with self.conn() as conn:
            with self.assertRaisesRegex(ValueError, "approver_not_valid"):
                approval.gate(conn, "adm1", self.path, {**self.body, "approvalId": request_id})

    def test_every_step_is_audited_and_the_tables_are_append_only(self):
        self.turn_on()
        request_id = self.request()
        with self.conn() as conn:
            approval.decide(conn, "adm2", request_id, "approve")
            actions = [row[0] for row in conn.execute("SELECT action FROM admin_audit WHERE action LIKE 'approval.%' ORDER BY audit_id")]
        self.assertEqual(actions, ["approval.switch", "approval.request", "approval.approve"])
        with self.conn() as conn:
            approval.consume(conn, request_id, "adm1")
            approval.record_role_change(conn, "adm1", "citizen", "user", "admin", "x")
        updates = {"admin_approval_requests": "reason", "admin_approval_decisions": "note", "admin_approval_uses": "used_by", "admin_role_changes": "reason"}
        for table, column in updates.items():
            for statement in (f"UPDATE {table} SET {column}='x'", f"DELETE FROM {table}"):
                with self.assertRaises(Exception, msg=statement):
                    with self.conn() as conn:
                        conn.execute(statement)

    def test_admin_role_changes_are_recorded_with_reason_and_approver(self):
        control = AdminControlManager(SimpleNamespace(db=self.db))
        control.user("adm1", "citizen", {"role": "admin", "role_reason": "새 운영자"})
        control.user("adm1", "citizen", {"role": "user"})                # off: a reason is recorded when given, null otherwise
        with self.conn() as conn:
            rows = [tuple(row) for row in conn.execute("SELECT target_id,old_role,new_role,actor_id,reason,approver_id FROM admin_role_changes ORDER BY change_id")]
        self.assertEqual(rows, [("citizen", "user", "admin", "adm1", "새 운영자", None), ("citizen", "admin", "user", "adm1", None, None)])
        self.turn_on()
        with self.assertRaisesRegex(ValueError, "role_reason_required"):
            control.user("adm1", "citizen", {"role": "admin"})
        self.assertEqual(self.db.users.get_user("citizen")["role"], "user")        # refused: nothing changed


class BootstrapRoleRecordTests(unittest.TestCase):
    def test_the_bots_bootstrap_restore_is_recorded(self):
        import asyncio
        from main import NaconomicsBot
        old = DBManager.database_dir
        with tempfile.TemporaryDirectory() as folder:
            DBManager.database_dir = Path(folder)
            db = DatabaseManager()
            try:
                bot = object.__new__(NaconomicsBot)
                bot.db = db
                boot = sorted(approval.BOOTSTRAP_ADMIN_IDS)[0]
                asyncio.run(bot.ensure_user(boot, "Boot"))                  # first registration as administrator
                db.users.update_role(boot, "user")
                asyncio.run(bot.ensure_user(boot, "Boot"))                  # restored by the bot
                with db.atomic(("users", "system")) as conn:
                    rows = [tuple(row) for row in conn.execute("SELECT old_role,new_role,actor_id FROM admin_role_changes ORDER BY change_id")]
                self.assertEqual(rows, [("(없음)", "admin", "system:bootstrap"), ("user", "admin", "system:bootstrap")])
            finally:
                db.close()
                DBManager.database_dir = old


class GateWiringTests(unittest.TestCase):
    def test_server_routes_every_post_through_the_gate_and_consumes_only_on_success(self):
        text = (ROOT / "web_api" / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        do_post = next(f for c in ast.walk(tree) if isinstance(c, ast.ClassDef) for f in c.body if isinstance(f, ast.FunctionDef) and f.name == "do_POST")
        source = ast.get_source_segment(text, do_post)
        self.assertIn("admin_approval_gate(self, verified_id, path, payload)", source)
        self.assertLess(source.index("admin_approval_gate("), source.index("/internal/auth/login"))      # before any route runs
        send = next(f for c in ast.walk(tree) if isinstance(c, ast.ClassDef) for f in c.body if isinstance(f, ast.FunctionDef) and f.name == "send_json")
        send_source = ast.get_source_segment(text, send)
        self.assertIn("status < 400", send_source)
        self.assertIn("admin_approval.consume", send_source)

    def test_every_high_risk_action_key_is_produced_by_risk_action(self):
        cases = {
            "db.update": ("/internal/admin/db/tables/t/rows/1", {}), "db.delete": ("/internal/admin/db/tables/t/rows/1/delete", {}),
            "accounting_close.write": ("/internal/admin/accounting-close", {"action": "x"}),
            "user.role": ("/internal/admin/users/u", {"role": "admin"}), "user.cash": ("/internal/admin/users/u", {"cash": 1}),
            "prudential_parameter.set": ("/internal/admin/compliance", {"action": "prudential_parameter_set", "key": "funding_auto_maturity_flag"}),
            "funding.repay": ("/internal/admin/funding", {"action": "repay"}), "funding.process_due": ("/internal/admin/funding", {"action": "process_due"}),
            "funding.draw": ("/internal/admin/funding", {"action": "draw"}), "funding.set_limit": ("/internal/admin/funding", {"action": "set_limit"}),
            "ftp_rate.apply": ("/internal/admin/compliance", {"action": "ftp_rate_apply"}),
            "approval.switch_off": ("/internal/admin/approvals", {"action": "set_switch", "enabled": False}),
        }
        self.assertEqual(set(cases), set(approval.HIGH_RISK_ACTIONS))
        for key, (path, payload) in cases.items():
            self.assertEqual(approval.risk_action(path, payload), key)

    def test_every_post_route_named_by_a_risk_action_exists_in_the_server(self):
        text = (ROOT / "web_api" / "server.py").read_text(encoding="utf-8")
        for route in ("/internal/admin/funding", "/internal/admin/compliance", "/internal/admin/accounting-close", "/internal/admin/approvals",
                      "/internal/admin/db/tables/", "/internal/admin/users/"):
            self.assertIn(f'"{route}', text, route)


if __name__ == "__main__":
    unittest.main()
