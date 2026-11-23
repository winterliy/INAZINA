"""The login endpoint must not reveal whether an id exists (same answer, same cost for an unknown id and a wrong password)."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web_api import server


class LoginEnumerationTests(unittest.TestCase):
    def login(self, auth_result, password="pw-guess"):
        body = json.dumps({"loginId": "someone", "password": password, "meta": {}}).encode()
        response = {}
        handler = SimpleNamespace(
            headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "", "content-length": str(len(body))},
            path="/internal/auth/login", rfile=io.BytesIO(body),
            send_json=lambda status, value: response.update(status=status, value=value))
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
             patch.object(server, "authenticate_login_identifier", return_value=auth_result), \
             patch.object(server, "record_admin_login") as record, \
             patch.object(server, "authenticate_password") as dummy_check:
            server.Handler.do_POST(handler)
        return response, record, dummy_check

    def test_unknown_id_and_wrong_password_look_identical(self):
        unknown, _, _ = self.login((None, False))
        wrong, _, _ = self.login((None, True))
        self.assertEqual(unknown, wrong)
        self.assertEqual(unknown, {"status": 401, "value": {"error": "invalid_credentials"}})

    def test_an_unknown_id_runs_the_same_credential_check_as_a_wrong_password(self):
        _, _, dummy_check = self.login((None, False))
        dummy_check.assert_called_once_with("", "pw-guess")

    def test_a_known_id_is_not_checked_a_second_time(self):
        _, _, dummy_check = self.login((None, True))
        dummy_check.assert_not_called()

    def test_a_missing_credential_row_still_pays_for_one_hash(self):
        import tempfile
        from pathlib import Path
        from database import DatabaseManager
        from database.db_manager import DBManager
        previous = DBManager.database_dir
        with tempfile.TemporaryDirectory() as root:
            DBManager.database_dir = Path(root)
            db = DatabaseManager()
            try:
                with patch.object(server, "password_hash") as hashed:
                    self.assertIsNone(server.authenticate_password("no-such-user", "pw-guess"))
                hashed.assert_called_once_with("pw-guess", server._LOGIN_DUMMY_SALT)
            finally:
                db.close()
                DBManager.database_dir = previous

    def test_the_admin_login_log_keeps_the_real_reason(self):
        _, record, _ = self.login((None, False))
        self.assertEqual(record.call_args.args[-1], "account_not_found")
        _, record, _ = self.login((None, True))
        self.assertEqual(record.call_args.args[-1], "invalid_credentials")

    def test_success_still_logs_in(self):
        user = {"userId": "u1", "role": "user"}
        response, _, _ = self.login((user, True))
        self.assertEqual(response, {"status": 200, "value": {"user": user}})


if __name__ == "__main__":
    unittest.main()
