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

    def test_clearing_audit_leaves_one_entry_for_the_deletion(self):
        for n in range(3):
            self.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','x','{}',?)", (n,))
        server.admin_log_delete("admin-1", "audit", {"all": True})
        self.assertEqual([row[0] for row in self.execute("SELECT action FROM admin_audit")], ["log.audit.delete"])

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
