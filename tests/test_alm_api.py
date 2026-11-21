"""M6: the administrator ALM report and the per-jurisdiction parameter action are admin-only, validated and audited."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web_api import server

REPORT = {'lcr': {'status': 'ok'}, 'nsfr': {'status': 'warning'}, 'capital': {}}


class AlmApiTests(unittest.TestCase):
    def get(self, path, role='admin'):
        result = {}
        handler = SimpleNamespace(headers={'x-inazina-internal-token': 'secret', 'x-inazina-user-id': 'a'},
                                  path=path, send_json=lambda status, value: result.update(status=status, value=value))
        with patch.object(server, 'INTERNAL_TOKEN', 'secret'), \
             patch.object(server, 'identity', return_value={'userId': 'a', 'role': role}), \
             patch.object(server, 'admin_alm_report', return_value=REPORT) as report, \
             patch.object(server, 'audit_admin_access') as audit:
            server.Handler.do_GET(handler)
        return result, report, audit

    def test_admin_reads_and_the_read_is_audited(self):
        response, report, audit = self.get('/internal/admin/alm?bank_id=bank')
        self.assertEqual(response['status'], 200)
        report.assert_called_once_with('BANK')
        audit.assert_called_once_with('a', 'alm.read', {'bank_id': 'BANK', 'lcr_status': 'ok', 'nsfr_status': 'warning'})

    def test_non_admin_and_missing_or_oversized_bank_id_are_rejected_without_read_or_audit(self):
        for path, role, status in [('/internal/admin/alm?bank_id=BANK', 'user', 403), ('/internal/admin/alm', 'admin', 400),
                                   ('/internal/admin/alm?bank_id=' + 'x' * 101, 'admin', 400)]:
            with self.subTest(path=path[:40], role=role):
                response, report, audit = self.get(path, role)
                self.assertEqual(response['status'], status)
                report.assert_not_called()
                audit.assert_not_called()

    def post(self, payload, role='admin', connection=None):
        import io
        import json
        from contextlib import nullcontext
        from unittest.mock import Mock
        body = json.dumps(payload).encode()
        response = {}
        handler = SimpleNamespace(headers={'x-inazina-internal-token': 'secret', 'x-inazina-user-id': 'a', 'content-length': str(len(body))},
                                  path='/internal/admin/compliance', rfile=io.BytesIO(body),
                                  send_json=lambda status, value: response.update(status=status, value=value))
        audit = Mock()
        with patch.object(server, 'INTERNAL_TOKEN', 'secret'), \
             patch.object(server, 'identity', return_value={'userId': 'a', 'role': role}), \
             patch.object(server, 'connect_db', return_value=nullcontext(connection or object())), \
             patch.object(server, 'audit_admin_access', audit):
            server.Handler.do_POST(handler)
        return response, audit


class AlmParameterActionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from database import DatabaseManager
        from database.db_manager import DBManager
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()

    def tearDown(self):
        from database.db_manager import DBManager
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    post = AlmApiTests.post

    def test_admin_sets_an_override_which_is_stored_and_audited(self):
        payload = {'action': 'prudential_parameter_set', 'nationality': '낙원', 'key': 'lcr_min_bps', 'valueBps': 12000}
        with self.db.atomic() as conn:
            response, audit = self.post(payload, connection=conn)
            self.assertEqual(response['status'], 200, response)
            self.assertEqual(response['value']['result'], {'nationality': '낙원', 'key': 'lcr_min_bps', 'valueBps': 12000})
            audit.assert_called_once()
            self.assertEqual(audit.call_args.args[1], 'prudential_parameter.set')
            row = conn.execute("SELECT value_bps,updated_by FROM prudential_parameters WHERE nationality='낙원' AND param_key='lcr_min_bps'").fetchone()
            self.assertEqual((row['value_bps'], row['updated_by']), (12000, 'a'))

    def test_non_admin_is_refused_and_nothing_is_written_or_audited(self):
        payload = {'action': 'prudential_parameter_set', 'nationality': '낙원', 'key': 'lcr_min_bps', 'valueBps': 12000}
        with self.db.atomic() as conn:
            response, audit = self.post(payload, role='user', connection=conn)
            self.assertEqual(response['status'], 403)
            audit.assert_not_called()
            self.assertIsNone(conn.execute("SELECT 1 FROM prudential_parameters").fetchone())

    def test_bad_payloads_are_rejected_without_writing(self):
        good = {'action': 'prudential_parameter_set', 'nationality': '낙원', 'key': 'lcr_min_bps', 'valueBps': 12000}
        for bad in (dict(good, key='not_a_parameter'), dict(good, valueBps=-5), dict(good, valueBps=1.5), dict(good, valueBps='12000'),
                    dict(good, nationality='  '), {k: v for k, v in good.items() if k != 'valueBps'}, dict(good, extra=1)):
            with self.subTest(payload=bad), self.db.atomic() as conn:
                response, audit = self.post(bad, connection=conn)
                self.assertEqual(response['status'], 400, response)
                audit.assert_not_called()
                self.assertIsNone(conn.execute("SELECT 1 FROM prudential_parameters").fetchone())


if __name__ == '__main__':
    unittest.main()
