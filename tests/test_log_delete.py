import time
import unittest

import test_web_api as fixtures
from web_api import server


class LogDeleteTests(unittest.TestCase):
    setUp = fixtures.WebApiTests.setUp
    tearDown = fixtures.WebApiTests.tearDown
    execute = fixtures.WebApiTests.execute

    def add_events(self):
        now = int(time.time())
        self.execute("INSERT INTO market_events(asset_code,event_type,severity,starts_epoch,ends_epoch,created_by) VALUES "
                     "('A','good',5,?,?, 'admin-1'),('B','bad',5,?,?,'admin-1'),('C','good',5,?,?,'admin-1')",
                     (now - 100, now - 50, now - 100, now - 10, now - 10, now + 1000))

    def test_selected_events_are_deleted_but_running_ones_stay(self):
        self.add_events()
        message = server.admin_log_delete("admin-1", "events", {"ids": [1, 3]})
        self.assertIn("1건", message)
        self.assertEqual([row[0] for row in self.execute("SELECT event_id FROM market_events ORDER BY 1")], [2, 3])

    def test_clear_all_events_keeps_running_event_and_audits(self):
        self.add_events()
        server.admin_log_delete("admin-1", "events", {"all": True})
        self.assertEqual([row[0] for row in self.execute("SELECT event_id FROM market_events")], [3])
        self.assertEqual(self.execute("SELECT action FROM admin_audit")[-1][0], "log.events.delete")

    def test_audit_trail_cannot_be_cleared(self):
        for n in range(3):
            self.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','x','{}',?)", (n,))
        with self.assertRaises(ValueError):
            server.admin_log_delete("admin-1", "audit", {"all": True})
        self.assertEqual([row[0] for row in self.execute("SELECT action FROM admin_audit")], ["x", "x", "x"])

    def test_flows_are_listed_and_deleted(self):
        self.execute("CREATE TABLE market_auto_target_history (history_id INTEGER PRIMARY KEY AUTOINCREMENT, asset_code TEXT NOT NULL, "
                     "anchor_price REAL NOT NULL, target_price REAL NOT NULL, start_epoch INTEGER NOT NULL, end_epoch INTEGER NOT NULL)")
        self.execute("INSERT INTO market_auto_target_history(asset_code,anchor_price,target_price,start_epoch,end_epoch) "
                     "VALUES ('A',100,110,0,10),('B',100,90,0,10)")
        flows = server.admin_market_flows()
        self.assertEqual([flow["asset_code"] for flow in flows], ["B", "A"])
        self.assertAlmostEqual(flows[1]["change_percent"], 10)
        server.admin_log_delete("admin-1", "flows", {"ids": [2]})
        self.assertEqual(self.execute("SELECT asset_code FROM market_auto_target_history"), [("A",)])

    def test_invalid_requests_are_rejected(self):
        with self.assertRaises(ValueError):
            server.admin_log_delete("admin-1", "users", {"all": True})
        with self.assertRaises(ValueError):
            server.admin_log_delete("admin-1", "events", {"ids": ["1; DROP TABLE users"]})


class AuditDeleteGuardTests(unittest.TestCase):
    """The real schema refuses audit edits and deletes from every client."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from database import DatabaseManager
        from database.db_manager import DBManager
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir, self.old_database = DBManager.database_dir, server.DATABASE
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        server.DATABASE = Path(self.tmp.name) / "inazina.db"
        for n in range(3):
            self.db.system.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('a','x','{}',?)", (n,))

    def tearDown(self):
        from database.db_manager import DBManager
        self.db.close()
        DBManager.database_dir, server.DATABASE = self.old_dir, self.old_database
        self.tmp.cleanup()

    def test_direct_edit_and_delete_are_refused(self):
        import sqlite3
        with self.assertRaisesRegex(sqlite3.DatabaseError, "edited"):
            self.db.system.execute("UPDATE admin_audit SET action='y'")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "cannot be deleted"):
            self.db.system.execute("DELETE FROM admin_audit")

    def test_work_log_endpoint_cannot_delete(self):
        with self.assertRaises(ValueError):
            server.admin_log_delete("admin-1", "audit", {"all": True})
        actions = [row[0] for row in self.db.system.execute("SELECT action FROM admin_audit ORDER BY audit_id")]
        self.assertEqual(actions, ["x", "x", "x"])
        import sqlite3
        with self.assertRaisesRegex(sqlite3.DatabaseError, "cannot be deleted"):
            self.db.system.execute("DELETE FROM admin_audit")

    def test_sensitive_read_access_appends_to_immutable_audit_trail(self):
        server.audit_admin_access(
            "admin-1", "compliance.read", {"nationality": "한국"},
        )
        row = self.db.system.execute_one(
            "SELECT actor_id,action,payload FROM admin_audit ORDER BY audit_id DESC LIMIT 1"
        )
        self.assertEqual(row[0:2], ("admin-1", "compliance.read"))
        import json
        self.assertEqual(json.loads(row[2]), {"nationality": "한국"})
