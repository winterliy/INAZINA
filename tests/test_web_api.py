import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database.db_manager import DBManager
from web_api import server


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_database_dir = DBManager.database_dir
        self.old_database = server.DATABASE
        root = Path(self.tempdir.name)
        DBManager.database_dir = root
        server.DATABASE = root / "inazina.db"
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.executescript("""
                CREATE TABLE users (
                    user_id TEXT PRIMARY KEY, nickname TEXT, nationality TEXT,
                    cash INTEGER DEFAULT 0, credit_score INTEGER DEFAULT 700,
                    status TEXT DEFAULT 'active', role TEXT DEFAULT 'user');
                CREATE TABLE admin_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, recorded_epoch REAL NOT NULL);
                CREATE TABLE web_auth_credentials (user_id TEXT PRIMARY KEY, salt TEXT, password_hash TEXT);
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TRIGGER settings_rate_check BEFORE UPDATE ON settings
                    WHEN NEW.key='base_rate' AND CAST(NEW.value AS REAL) NOT BETWEEN -100 AND 100
                    BEGIN SELECT RAISE(ABORT, 'Rate is outside its permitted range'); END;
                CREATE TABLE market_price_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT, asset_code TEXT NOT NULL,
                    price REAL NOT NULL, recorded_epoch INTEGER NOT NULL,
                    UNIQUE(asset_code, recorded_epoch));
                CREATE TABLE market_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, asset_code TEXT, event_type TEXT NOT NULL,
                    severity INTEGER NOT NULL, starts_epoch INTEGER NOT NULL, ends_epoch INTEGER NOT NULL,
                    created_by TEXT NOT NULL);
                INSERT INTO users(user_id,nickname,nationality,role) VALUES ('admin-1','Admin','낙원','admin');
                INSERT INTO settings(key,value) VALUES ('base_rate','3');
            """)

    def tearDown(self):
        DBManager.database_dir = self.old_database_dir
        server.DATABASE = self.old_database
        self.tempdir.cleanup()

    def execute(self, sql, params=()):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            return connection.execute(sql, params).fetchall()

    def test_candles_aggregate_open_high_low_close_per_bucket(self):
        ticks = [(0, 100), (10, 120), (30, 90), (59, 110), (60, 111), (119, 105)]
        for epoch, price in ticks:
            self.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES ('AAA',?,?)",
                         (price, 600 + epoch))
        self.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES ('BBB',999,610)")
        with server.connect_readonly() as connection:
            result = server.candles(connection, "AAA", 0, 60)
        self.assertEqual(result, [
            {"time": 600, "open": 100.0, "high": 120.0, "low": 90.0, "close": 110.0, "ticks": 4},
            {"time": 660, "open": 111.0, "high": 111.0, "low": 105.0, "close": 105.0, "ticks": 2},
        ])

    def test_chart_rejects_unknown_ranges_and_intervals(self):
        with patch.object(server, "instruments", return_value=[{"code": "AAA"}]):
            with self.assertRaisesRegex(ValueError, "unsupported_chart_range"):
                server.chart("AAA", "2y")
            with self.assertRaisesRegex(ValueError, "unsupported_candle_interval"):
                server.chart("AAA", "1d", 7)
            result = server.chart("AAA", "1w")
        self.assertEqual(result["interval"], 3600)
        self.assertEqual(result["candles"], [])

    def test_drawings_round_trip_per_user_and_instrument(self):
        drawings = [
            {"id": "a", "type": "trend", "anchors": [{"time": 1, "price": 10}, {"time": 2, "price": 12.5}], "extra": 1},
            {"id": "b", "type": "horizontal", "anchors": [{"time": 3, "price": 11}]},
        ]
        saved = server.save_drawings("admin-1", "AAA", drawings)
        self.assertNotIn("extra", saved[0])
        self.assertEqual(server.get_drawings("admin-1", "AAA"), saved)
        self.assertEqual(server.get_drawings("admin-1", "BBB"), [])
        self.assertEqual(server.get_drawings("other", "AAA"), [])
        server.save_drawings("admin-1", "AAA", [])
        self.assertEqual(self.execute("SELECT COUNT(*) FROM web_chart_drawings")[0][0], 0)

    def test_drawings_reject_invalid_shapes(self):
        invalid = [
            "not a list",
            [{"id": "a", "type": "circle", "anchors": [{"time": 1, "price": 1}]}],
            [{"id": "a", "type": "trend", "anchors": [{"time": 1, "price": 1}]}],
            [{"id": "a", "type": "horizontal", "anchors": [{"time": 1, "price": 0}]}],
            [{"id": "a", "type": "horizontal", "anchors": [{"time": float("nan"), "price": 1}]}],
            [{"id": "", "type": "horizontal", "anchors": [{"time": 1, "price": 1}]}],
            [{"id": str(index), "type": "horizontal", "anchors": [{"time": 1, "price": 1}]}
             for index in range(server.MAX_DRAWINGS + 1)],
        ]
        for value in invalid:
            with self.subTest(value=str(value)[:40]), self.assertRaisesRegex(ValueError, "invalid_drawing"):
                server.normalize_drawings(value)

    def test_audit_and_events_are_read_from_the_database(self):
        self.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','settings','{\"a\":1}',5)")
        self.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','legacy','not json',6)")
        self.execute("INSERT INTO market_events(asset_code,event_type,severity,starts_epoch,ends_epoch,created_by) "
                     "VALUES ('AAA','good',5,1,2,'admin-1')")
        audit = server.admin_audit()
        self.assertEqual(audit[0], {"actorId": "admin-1", "action": "legacy", "payload": {"raw": "not json"}, "time": 6.0})
        self.assertEqual(audit[1]["payload"], {"a": 1})
        events = server.admin_events()
        self.assertEqual(events[0]["asset_code"], "AAA")
        self.assertIsNone(events[0]["target_mode"])
        self.assertFalse(events[0]["active"])

    def test_db_editor_hides_credentials_and_audit(self):
        names = {table["name"] for table in server.db_tables()}
        self.assertIn("settings", names)
        self.assertTrue(names.isdisjoint({"web_auth_credentials", "admin_audit"}))
        for table in ("web_auth_credentials", "admin_audit", "sqlite_master"):
            with self.subTest(table=table), self.assertRaisesRegex(ValueError, "table_not_found"):
                server.db_rows(table)

    def test_db_editor_update_insert_delete_are_audited(self):
        page = server.db_rows("settings", search="base")
        self.assertEqual(page["total"], 1)
        rowid = page["rows"][0]["__rowid__"]
        updated = server.db_update("admin-1", "settings", rowid, {"value": "4.5"})
        self.assertEqual(updated["value"], "4.5")
        inserted = server.db_insert("admin-1", "settings", {"key": "garch_alpha", "value": "0.1"})
        server.db_delete("admin-1", "settings", inserted["__rowid__"])
        actions = [(row[0], json.loads(row[1])) for row in self.execute(
            "SELECT action,payload FROM admin_audit ORDER BY audit_id")]
        self.assertEqual([action for action, _ in actions], ["db.update", "db.insert", "db.delete"])
        self.assertEqual(actions[0][1]["before"], {"value": "3"})
        self.assertEqual(actions[0][1]["after"], {"value": "4.5"})
        self.assertEqual(self.execute("SELECT COUNT(*) FROM settings")[0][0], 1)

    def test_db_editor_keeps_database_triggers_and_validates_columns(self):
        rowid = server.db_rows("settings")["rows"][0]["__rowid__"]
        with self.assertRaises(server.sqlite3.IntegrityError):
            server.db_update("admin-1", "settings", rowid, {"value": "500"})
        self.assertEqual(self.execute("SELECT value FROM settings")[0][0], "3")
        self.assertEqual(self.execute("SELECT COUNT(*) FROM admin_audit")[0][0], 0)
        for values in ({}, {"missing": 1}, {"value": ["list"]}, {"value": float("inf")}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                server.db_update("admin-1", "settings", rowid, values)
        with self.assertRaisesRegex(ValueError, "row_not_found"):
            server.db_update("admin-1", "settings", 999, {"value": "1"})

    def test_admin_command_catalog_lists_usage(self):
        catalog = server.admin_command_catalog()
        names = {item["name"] for item in catalog["commands"]}
        self.assertIn("잔고수정", names)
        self.assertTrue(all("usage" in item for item in catalog["commands"]))
        with self.assertRaisesRegex(ValueError, "알 수 없는"):
            server.run_admin_command("admin-1", {"command": "rm", "arguments": ""})


if __name__ == "__main__":
    unittest.main()
