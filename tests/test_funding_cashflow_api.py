import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web_api import server


class FundingCashflowApiTests(unittest.TestCase):
    def request(self, path, role='admin'):
        result = {}
        handler = SimpleNamespace(headers={'x-inazina-internal-token': 'secret', 'x-inazina-user-id': 'a'},
                                  path=path, send_json=lambda status, value: result.update(status=status, value=value))
        with patch.object(server, 'INTERNAL_TOKEN', 'secret'), \
             patch.object(server, 'identity', return_value={'userId': 'a', 'role': role}), \
             patch.object(server, 'admin_funding_cashflow', return_value={'cashflows': [{'funding_id': 1}]}) as report, \
             patch.object(server, 'audit_admin_access') as audit:
            server.Handler.do_GET(handler)
        return result, report, audit

    def test_admin_filters_and_audits_read(self):
        response, report, audit = self.request('/internal/admin/funding-cashflow?bank_id=bank&currency=usd')
        self.assertEqual(response['status'], 200)
        report.assert_called_once_with('BANK', 'USD')
        audit.assert_called_once_with('a', 'funding_cashflow.read',
                                      {'bank_id': 'BANK', 'currency': 'USD', 'result_count': 1})

    def test_non_admin_and_invalid_filter_are_rejected_without_read_or_audit(self):
        for path, role, status in [('/internal/admin/funding-cashflow?bank_id=BANK', 'user', 403),
                                   ('/internal/admin/funding-cashflow', 'admin', 400),
                                   ('/internal/admin/funding-cashflow?bank_id=BANK&currency=abcdefghijklmn', 'admin', 400)]:
            with self.subTest(path=path, role=role):
                response, report, audit = self.request(path, role)
                self.assertEqual(response['status'], status)
                report.assert_not_called()
                audit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
