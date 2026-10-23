import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from web_api import admin_logins


class AdminLoginTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE users(user_id TEXT PRIMARY KEY, nickname TEXT, role TEXT);
            CREATE TABLE web_auth_credentials(user_id TEXT PRIMARY KEY, hts_id TEXT);
            INSERT INTO users VALUES ('111','관리자','admin'),('222','시민','user'),('333','부트스트랩','user');
            INSERT INTO web_auth_credentials VALUES ('111','boss'),('222','citizen'),('333',NULL);
        """)
        self.meta = {"clientIp": "127.0.0.1", "app": "dashboard",
                     "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"}

    def tearDown(self):
        self.conn.close()

    def test_records_admin_logins_by_discord_or_hts_id_but_not_other_users(self):
        self.assertIsNotNone(admin_logins.record(self.conn, "111", True, self.meta, now=1000))
        self.assertIsNotNone(admin_logins.record(self.conn, "BOSS", False, self.meta, reason="invalid_credentials", now=1001))
        self.assertIsNone(admin_logins.record(self.conn, "222", True, self.meta, now=1002))
        self.assertIsNone(admin_logins.record(self.conn, "citizen", True, self.meta, now=1003))
        self.assertIsNone(admin_logins.record(self.conn, "nobody", False, self.meta, now=1004))
        self.assertIsNotNone(admin_logins.record(self.conn, "333", True, self.meta, bootstrap_ids={"333"}, now=1005))
        events = admin_logins.history(self.conn)
        self.assertEqual([(e["userId"], e["success"]) for e in events], [("333", True), ("111", False), ("111", True)])
        failed = events[1]
        self.assertEqual((failed["loginId"], failed["reason"], failed["nickname"]), ("BOSS", "invalid_credentials", "관리자"))
        self.assertEqual((failed["ip"], failed["location"], failed["app"], failed["device"]),
                         ("127.0.0.1", "이 컴퓨터 (로컬)", "대시보드", "macOS · Chrome"))
        self.assertEqual([e["userId"] for e in admin_logins.history(self.conn, "111")], ["111", "111"])

    def test_location_device_and_untrusted_meta(self):
        self.assertEqual(admin_logins.location("::ffff:127.0.0.1"), "이 컴퓨터 (로컬)")
        self.assertEqual(admin_logins.location("192.168.0.12"), "내부 네트워크")
        with patch.object(admin_logins, "GEOIP_PATH", Path("/nonexistent/geoip.mmdb")):
            self.assertEqual(admin_logins.location("8.8.8.8"), "외부 인터넷")  # no database: no place
        self.assertEqual(admin_logins.location("not-an-ip"), "알 수 없음")
        self.assertEqual(admin_logins.device("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1"), "iPhone · Safari")
        self.assertEqual(admin_logins.device("Mozilla/5.0 (Windows NT 10.0) Chrome/142 Edg/142"), "Windows · Edge")
        self.assertEqual(admin_logins.device(""), "알 수 없음")
        admin_logins.record(self.conn, "111", True, {"clientIp": "x" * 500, "app": "<script>", "userAgent": "y" * 900}, now=5)
        event = admin_logins.history(self.conn)[0]
        self.assertEqual(len(event["ip"]), 64)
        self.assertIsNone(event["app"])  # only known apps are stored
        self.assertEqual(len(event["userAgent"]), 300)
        admin_logins.record(self.conn, "111", True, None, now=6)
        self.assertEqual(admin_logins.history(self.conn)[0]["ip"], "unknown")

    @unittest.skipUnless(admin_logins.GEOIP_PATH.exists(), "GeoIP database not installed (npm run geoip:update)")
    def test_public_addresses_get_country_region_and_city(self):
        self.assertEqual(admin_logins.location("8.8.8.8").split(" · ")[0], "미국")
        self.assertTrue(admin_logins.location("211.234.1.1").startswith("대한민국 · "))
        self.assertEqual(admin_logins.location("127.0.0.1"), "이 컴퓨터 (로컬)")  # never looked up
        admin_logins.record(self.conn, "111", True, dict(self.meta, clientIp="211.234.1.1"), now=7)
        self.assertTrue(admin_logins.history(self.conn)[0]["location"].startswith("대한민국"))

    def test_old_records_are_deleted_after_retention(self):
        admin_logins.record(self.conn, "111", True, self.meta, now=100)
        admin_logins.record(self.conn, "111", True, self.meta, now=100 + admin_logins.RETENTION_SECONDS + 1)
        self.assertEqual(len(admin_logins.history(self.conn)), 1)


if __name__ == "__main__":
    unittest.main()
