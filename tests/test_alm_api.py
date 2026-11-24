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


    def test_new_assumption_keys_go_through_the_api_and_out_of_range_values_do_not(self):
        base = {'action': 'prudential_parameter_set', 'nationality': '낙원'}
        for key, value in (('deposit_beta_bps', 7000), ('funding_auto_maturity_flag', 1), ('ftp_apply_enabled', 1), ('core_deposit_years', 5)):
            with self.subTest(stored=key), self.db.atomic() as conn:
                response, audit = self.post(dict(base, key=key, valueBps=value), connection=conn)
                self.assertEqual(response['status'], 200, response)
                self.assertEqual(audit.call_args.args[1], 'prudential_parameter.set')
                self.assertEqual(conn.execute("SELECT value_bps FROM prudential_parameters WHERE param_key=?", (key,)).fetchone()[0], value)
        for key, value in (('funding_auto_maturity_flag', 2), ('funding_auto_maturity_flag', True), ('deposit_beta_bps', 10001),
                           ('ftp_apply_enabled', 2), ('core_deposit_years', 31)):
            with self.subTest(rejected=(key, value)), self.db.atomic() as conn:
                response, audit = self.post(dict(base, key=key, valueBps=value), connection=conn)
                self.assertEqual(response['status'], 400, response)
                audit.assert_not_called()
                stored = conn.execute("SELECT value_bps FROM prudential_parameters WHERE param_key=?", (key,)).fetchone()
                self.assertTrue(stored is None or type(value) is bool or stored[0] != value)      # the valid value stored above is untouched


class AlmFtpRateActionTests(unittest.TestCase):
    """The FTP apply / revert actions: admin only, exact payload, switch off by default, audited."""
    def setUp(self):
        import tempfile
        from pathlib import Path
        from database import DatabaseManager
        from database.db_manager import DBManager
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO financial_institutions(institution_id,issuer,nationality,equity_capital,retained_earnings,deposit_insurance_limit) "
            "VALUES ('BNK1','Bank','낙원',1000,0,300)")
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('L1','loan',6,10000,730,86400,0,'Bank','NWX')")
        self.db.currency.execute("DELETE FROM market_rate_curve WHERE nationality='낙원' AND benchmark_key='funding'")
        for tenor, rate in ((365, 2.0), (730, 3.0)):
            self.db.currency.execute("INSERT INTO market_rate_curve VALUES ('낙원','funding',?,?,0.75,0)", (tenor, rate))

    def tearDown(self):
        from database.db_manager import DBManager
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    post = AlmApiTests.post
    apply_payload = {'action': 'ftp_rate_apply', 'bankId': 'BNK1', 'kind': 'loan', 'productCode': 'L1', 'marginBps': 150, 'reason': 'test'}

    def count(self, conn):
        return conn.execute("SELECT COUNT(*) FROM ftp_rate_applications").fetchone()[0]

    def test_non_admin_is_refused(self):
        with self.db.atomic() as conn:
            response, audit = self.post(self.apply_payload, role='user', connection=conn)
            self.assertEqual(response['status'], 403)
            audit.assert_not_called()

    def test_switch_off_and_bad_shapes_are_rejected_without_write_or_audit(self):
        good = self.apply_payload
        for bad in (good, {k: v for k, v in good.items() if k != 'reason'}, dict(good, extra=1), dict(good, marginBps='150'),
                    dict(good, bankId='NOPE'), {'action': 'ftp_rate_revert', 'bankId': 'BNK1', 'applicationId': 1}):
            with self.subTest(payload=bad), self.db.atomic() as conn:
                response, audit = self.post(bad, connection=conn)
                self.assertEqual(response['status'], 400, response)         # the first one fails because the switch is off
                audit.assert_not_called()
                self.assertEqual(self.count(conn), 0)
        self.assertEqual(self.db.products.execute("SELECT interest_rate FROM liability WHERE product_code='L1'")[0][0], 6)

    def test_apply_then_revert_are_stored_and_audited_with_the_actor(self):
        with self.db.atomic() as conn:
            response, _ = self.post({'action': 'prudential_parameter_set', 'nationality': '낙원', 'key': 'ftp_apply_enabled', 'valueBps': 1}, connection=conn)
            self.assertEqual(response['status'], 200)
            response, audit = self.post(self.apply_payload, connection=conn)
            self.assertEqual(response['status'], 200, response)
            self.assertEqual(audit.call_args.args[1], 'ftp_rate.apply')
            self.assertEqual(audit.call_args.args[2]['newRateBps'], 480)
            app_id = response['value']['result']['applicationId']
            self.assertEqual(conn.execute("SELECT actor FROM ftp_rate_applications WHERE app_id=?", (app_id,)).fetchone()[0], 'a')
            response, audit = self.post({'action': 'ftp_rate_revert', 'bankId': 'BNK1', 'applicationId': app_id, 'reason': 'undo'}, connection=conn)
            self.assertEqual(response['status'], 200, response)
            self.assertEqual(audit.call_args.args[1], 'ftp_rate.revert')
            self.assertEqual(self.count(conn), 2)
            response, audit = self.post({'action': 'ftp_rate_revert', 'bankId': 'BNK1', 'applicationId': app_id, 'reason': 'again'}, connection=conn)
            self.assertEqual(response['status'], 400)
            audit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
