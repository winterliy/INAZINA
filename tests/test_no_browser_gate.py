"""The data service (4313) has no browser pages and no login of its own: a request without the internal token gets a raw JSON
401, whatever it accepts, and the old gate (landing pages, the browser-access login, the emergency passcode, its attempt log)
is gone."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from web_api import server

ROOT = Path(__file__).resolve().parents[1]


class NoBrowserGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_token = server.INTERNAL_TOKEN
        server.INTERNAL_TOKEN = "test-token"
        cls.httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.INTERNAL_TOKEN = cls.old_token

    def ask(self, path, method="GET", headers=None, data=None):
        request = urllib.request.Request(self.base + path, method=method, headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers.get("content-type", ""), response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("content-type", ""), error.read().decode("utf-8")

    def test_a_browser_without_the_token_gets_raw_json_401_everywhere(self):
        for path in ("/", "/internal", "/internal/health", "/internal/me", "/favicon.ico", "/internal/admin/security", "/anything"):
            status, kind, body = self.ask(path, headers={"Accept": "text/html,application/xhtml+xml"})
            self.assertEqual(status, 401, path)
            self.assertTrue(kind.startswith("application/json"), (path, kind))
            self.assertEqual(json.loads(body), {"error": "internal_authentication_required"}, path)

    def test_the_old_browser_access_login_is_just_another_unauthenticated_post(self):
        status, kind, body = self.ask("/__browser-access", method="POST", data=b"loginId=a&password=0000000",
                                      headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "internal_authentication_required"})

    def test_with_the_token_a_browser_still_gets_json_not_a_page(self):
        status, kind, body = self.ask("/internal/no-such-path", headers={"x-inazina-internal-token": "test-token", "Accept": "text/html"})
        self.assertTrue(kind.startswith("application/json"), kind)
        json.loads(body)

    def test_the_gate_code_is_gone(self):
        for name in ("EMERGENCY_PASSCODE", "_browser_login_attempts", "security_overview"):
            self.assertFalse(hasattr(server, name), name)
        for name in ("browser_access_login", "send_browser_access_page", "send_landing"):
            self.assertFalse(hasattr(server.Handler, name), name)
        for relative in ("web_api/landing.py", "web_api/security_store.py", "tests/test_landing.py", "tests/test_security_store.py"):
            self.assertFalse((ROOT / relative).exists(), relative)


if __name__ == "__main__":
    unittest.main()
