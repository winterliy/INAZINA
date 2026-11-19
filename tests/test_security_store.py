"""Browser-access passwords for the private service ports."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from web_api import security_store as store


class SecurityStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.saved = (store.STORE, store.ATTEMPTS)
        store.STORE, store.ATTEMPTS = root / "security.json", root / "attempts.jsonl"
        self.addCleanup(lambda: setattr(store, "STORE", self.saved[0]) or setattr(store, "ATTEMPTS", self.saved[1]))

    def test_default_hash_verifies_until_changed_and_is_flagged(self):
        env_hash = hashlib.sha256(b"old-password").hexdigest()
        import os
        os.environ["INAZINA_BROWSER_PASSWORD_SHA256"] = env_hash
        self.addCleanup(os.environ.pop, "INAZINA_BROWSER_PASSWORD_SHA256", None)
        self.assertTrue(store.verify("api", "old-password"))
        self.assertFalse(store.verify("api", "nope"))
        self.assertFalse(store.verify("other", "old-password"))
        self.assertFalse(next(s for s in store.overview()["services"] if s["id"] == "api")["isDefault"])

    def test_change_is_per_service_salted_and_applies_at_once(self):
        import os
        os.environ["INAZINA_BROWSER_PASSWORD_SHA256"] = hashlib.sha256(b"start-pass").hexdigest()
        self.addCleanup(os.environ.pop, "INAZINA_BROWSER_PASSWORD_SHA256", None)
        store.set_password("python", "start-pass", "a-new-password", "admin")
        self.assertTrue(store.verify("python", "a-new-password"))
        self.assertFalse(store.verify("python", "start-pass"))
        self.assertTrue(store.verify("api", "start-pass"))  # the other service is untouched
        saved = json.loads(store.STORE.read_text(encoding="utf-8"))["python"]
        self.assertTrue(saved["hash"].startswith("pbkdf2$"))
        self.assertNotIn("a-new-password", store.STORE.read_text(encoding="utf-8"))
        self.assertEqual((saved["updatedBy"], oct(store.STORE.stat().st_mode & 0o777)), ("admin", "0o600"))

    def test_refuses_wrong_current_short_same_and_unknown_service(self):
        import os
        os.environ["INAZINA_BROWSER_PASSWORD_SHA256"] = hashlib.sha256(b"start-pass").hexdigest()
        self.addCleanup(os.environ.pop, "INAZINA_BROWSER_PASSWORD_SHA256", None)
        for service, current, new, message in (("api", "wrong", "long-enough-1", "현재"), ("api", "start-pass", "short", "8"),
                                               ("api", "start-pass", "start-pass", "다른"), ("x", "start-pass", "long-enough-1", "서비스")):
            with self.assertRaisesRegex(ValueError, message):
                store.set_password(service, current, new, "admin")
        self.assertFalse(store.STORE.exists())

    def test_attempt_log_keeps_no_password_and_reports_locks(self):
        for _ in range(store.LOCK_ATTEMPTS):
            store.log_attempt("api", "203.0.113.9", False)
        store.log_attempt("api", "198.51.100.2", True)
        info = store.overview()
        self.assertEqual(info["attempts"][0]["ip"], "198.51.100.2")
        self.assertEqual(info["last24h"], {"total": store.LOCK_ATTEMPTS + 1, "failed": store.LOCK_ATTEMPTS})
        self.assertEqual(next(s for s in info["services"] if s["id"] == "api")["locked"], ["203.0.113.9"])
        self.assertNotIn("password", store.ATTEMPTS.read_text(encoding="utf-8"))

    def test_dashboard_action_changes_one_service_and_audits_without_secrets(self):
        import os
        from unittest import mock
        from web_api import server
        os.environ["INAZINA_BROWSER_PASSWORD_SHA256"] = hashlib.sha256(b"start-pass").hexdigest()
        self.addCleanup(os.environ.pop, "INAZINA_BROWSER_PASSWORD_SHA256", None)
        with mock.patch.object(server, "audit_admin_access") as audit:
            message = server.security_action("admin", {"action": "browser_password", "service": "api", "current": "start-pass", "password": "a-new-password"})
            self.assertIn("바로 적용", message)
            audit.assert_called_once_with("admin", "security.browser_password", {"service": "api"})
            for bad in ({"action": "browser_password", "service": "api", "current": "x", "password": "y", "extra": 1}, {"action": "other"}):
                with self.assertRaises(ValueError):
                    server.security_action("admin", bad)
        self.assertTrue(store.verify("api", "a-new-password"))
        self.assertEqual({s["id"]: s["isDefault"] for s in server.security_overview()["services"]}, {"api": False, "python": False})

    def test_reset_tool_sets_every_target_without_the_current_password(self):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("reset_tool", store.ROOT / "tools" / "reset_access_password.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        tool.WL_STORE = Path(self.dir.name) / "wl-security.json"
        store.reset_password("python", "brand-new-pass")
        self.assertTrue(store.verify("python", "brand-new-pass"))
        with self.assertRaises(ValueError):
            store.reset_password("python", "short")
        tool.reset_wl("wl-entry", "wl-entry-secret")
        tool.reset_wl("wl-code", "246810")
        saved = json.loads(tool.WL_STORE.read_text(encoding="utf-8"))
        self.assertTrue(saved["entry"]["hash"].startswith("pbkdf2$"))
        self.assertEqual(saved["logCode"]["length"], 6)
        with self.assertRaises(ValueError):
            tool.reset_wl("wl-code", "12ab")


if __name__ == "__main__":
    unittest.main()
