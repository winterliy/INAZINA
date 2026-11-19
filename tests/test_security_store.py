"""The attempt log shared by the browser-access pages of the API (4312) and data-service (4313) ports."""
import json
import tempfile
import unittest
from pathlib import Path

from web_api import security_store as store


class SecurityStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.saved = store.ATTEMPTS
        store.ATTEMPTS = Path(self.dir.name) / "attempts.jsonl"
        self.addCleanup(lambda: setattr(store, "ATTEMPTS", self.saved))

    def test_log_keeps_who_and_how_but_never_a_password(self):
        store.log_attempt("api", "203.0.113.9", False, "someone")
        store.log_attempt("python", "198.51.100.2", True, "", "emergency")
        rows = store.recent_attempts(10)
        self.assertEqual([(r["service"], r["ok"], r["via"], r["loginId"]) for r in rows],
                         [("python", True, "emergency", ""), ("api", False, "login", "someone")])
        self.assertNotIn("password", store.ATTEMPTS.read_text(encoding="utf-8"))

    def test_overview_counts_a_day_and_reports_locked_addresses_per_service(self):
        for _ in range(store.LOCK_ATTEMPTS):
            store.log_attempt("api", "203.0.113.9", False, "guess")
        store.log_attempt("python", "203.0.113.9", False, "guess")      # fewer than the limit on the other service
        store.log_attempt("api", "198.51.100.2", True, "admin")
        store.log_attempt("python", "127.0.0.1", True, "", "emergency")
        info = store.overview()
        self.assertEqual(info["last24h"], {"total": store.LOCK_ATTEMPTS + 3, "failed": store.LOCK_ATTEMPTS + 1, "emergency": 1})
        self.assertEqual(info["locked"], [{"service": "api", "ip": "203.0.113.9"}])
        self.assertEqual(info["attempts"][0]["loginId"], "")
        self.assertEqual([s["port"] for s in info["services"]], [4312, 4313])

    def test_a_large_log_is_trimmed_and_unreadable_lines_are_skipped(self):
        store.ATTEMPTS.write_text("not json\n" + "\n".join(json.dumps({"at": 1, "service": "api", "ip": "x", "ok": True}) for _ in range(5)) + "\n", encoding="utf-8")
        self.assertEqual(len(store.recent_attempts(100)), 5)
        self.assertEqual(store.recent_attempts(100)[0]["service"], "api")


if __name__ == "__main__":
    unittest.main()
