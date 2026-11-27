"""M8 read-only warnings: anomalous admin access, secret-rotation status, the backup-age check and the rotation tool."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from functions.admin_audit import record_admin_audit
from web_api import admin_logins, controls_overview
from tools import secret_rotation_check

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0                     # 2027-01-15 08:00:00 UTC = 17:00 KST
NIGHT = NOW - 14 * 3600 + 3600 * 0.5      # 03:30 KST that day (UTC 18:30 the day before)


class AnomalyTests(unittest.TestCase):
    def setUp(self):
        self.old = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.users.create_user("adm", "adm", "Nac", 0, role="admin")
        self.db.users.create_user("citizen", "citizen", "Nac", 0)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old
        self.tmp.cleanup()

    def alerts(self):
        with self.db.atomic(("users", "system")) as conn:
            admin_logins.ensure_table(conn)
            return {row["key"]: row for row in controls_overview.anomalies(conn, now=NOW)}

    def login(self, user, success, epoch):
        with self.db.atomic(("users", "system")) as conn:
            admin_logins.ensure_table(conn)
            conn.execute("INSERT INTO admin_login_events(user_id,login_id,success,ip,location,created_epoch) VALUES (?,?,?,?,?,?)",
                         (user, user, success, "127.0.0.1", "local", int(epoch)))

    def test_quiet_system_has_no_warnings(self):
        self.assertEqual({row["status"] for row in self.alerts().values()}, {"ok"})

    def test_failed_login_spike(self):
        for index in range(5):
            self.login("adm", 0, NOW - 60 * index)
        self.assertEqual(self.alerts()["login_failures"]["status"], "warn")
        self.login("adm", 0, NOW - 7200)                         # outside the window: not counted again
        self.assertIn("5건", self.alerts()["login_failures"]["detail"].split("실패")[1])

    def test_a_recently_first_seen_admin_is_flagged_but_an_old_one_is_not(self):
        self.login("adm", 1, NOW - 86400)
        self.login("citizen", 1, NOW - 86400)                    # not an administrator: ignored
        self.assertEqual(self.alerts()["new_admin"]["status"], "warn")
        with self.db.atomic(("users", "system")) as conn:
            conn.execute("UPDATE admin_login_events SET created_epoch=?", (NOW - 30 * 86400,))
        self.login("adm", 1, NOW - 3600)                          # later logins do not make the admin new
        self.assertEqual(self.alerts()["new_admin"]["status"], "ok")

    def test_off_hours_writes_ignore_reads_and_automation(self):
        with self.db.atomic(("users", "system")) as conn:
            record_admin_audit(conn, "adm", "funding.repay", {}, recorded_epoch=NIGHT)
            record_admin_audit(conn, "adm", "admin_users.read", {}, recorded_epoch=NIGHT)
            record_admin_audit(conn, "system:funding_maturity", "funding.auto_process_due", {}, recorded_epoch=NIGHT)
            record_admin_audit(conn, "adm", "funding.repay", {}, recorded_epoch=NOW - 3600)          # daytime
        alert = self.alerts()["off_hours_writes"]
        self.assertEqual(alert["status"], "warn")
        self.assertIn("쓰기 1건", alert["detail"])

    def test_write_heuristic(self):
        self.assertFalse(controls_overview._is_write("login_history.read"))
        self.assertFalse(controls_overview._is_write("dr_voting_meetings.list"))
        self.assertTrue(controls_overview._is_write("prudential_parameter.set"))


class RotationAndBackupTests(unittest.TestCase):
    def test_rotation_status_by_age(self):
        rows = controls_overview.rotation_status([{"name": "A", "last_rotated": "2026-11-01"}, {"name": "B", "last_rotated": "2026-01-01"},
                                                  {"name": "C", "last_rotated": None}], date(2026, 11, 27))
        self.assertEqual([row["status"] for row in rows], ["ok", "overdue", "missing"])

    def test_rotation_check_never_prints_a_value_and_flags_short_tokens(self):
        secret = "short-value-xyz"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "logs").mkdir()
            (root / "logs" / "api.log").write_text("ok line\n", encoding="utf-8")
            (root / "docs" / "m8_records").mkdir(parents=True)
            (root / "docs" / "m8_records" / "drills.json").write_text(json.dumps({"secret_rotations": [{"name": "T", "last_rotated": "2026-11-01"}]}), encoding="utf-8")
            report, problems = secret_rotation_check.run({"INAZINA_INTERNAL_TOKEN": secret}, root, date(2026, 11, 27))
        self.assertIn("shorter than 32", " ".join(problems))
        self.assertNotIn(secret, json.dumps(report) + " ".join(problems))
        self.assertEqual(report["token"]["length"], len(secret))

    def test_rotation_check_finds_a_credential_pattern_in_a_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "logs").mkdir()
            (root / "logs" / "bot.log").write_text("token ghp_" + "a" * 36 + "\n", encoding="utf-8")
            report, problems = secret_rotation_check.run({"INAZINA_INTERNAL_TOKEN": "x" * 40}, root, date(2026, 11, 27))
        self.assertEqual(report["patterns_found_in"], {"logs/bot.log": ["github_token"]})
        self.assertTrue(any("credential patterns" in item for item in problems))

    def test_backup_check_exits_nonzero_when_stale(self):
        run = subprocess.run([sys.executable, str(ROOT / "tools" / "backup_check.py"), "--max-hours", "0.0000001"], capture_output=True, text=True, cwd=ROOT)
        status = json.loads(run.stdout)
        self.assertEqual(run.returncode, 0 if status["status"] == "ok" else 1)
        self.assertEqual(status["status"], "stale" if status["count"] else "missing")


if __name__ == "__main__":
    unittest.main()
