"""Expected funding repayments use the same rounding as actual settlement."""
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.funding_manager import FundingManager

NOW = 2_000_000_000
DAY = 86400


class FundingCashflowTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.currency.execute("INSERT INTO financial_institutions(issuer,nationality) VALUES ('Bank','낙원')")
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1500',2)")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def test_projected_cashflow_matches_actual_repayment_and_separates_currencies(self):
        with self.db.atomic() as conn:
            for currency, amount in [('NWX', 10000), ('USD', '10000.00')]:
                FundingManager.set_limit(conn, 'Bank', 'interbank', currency, amount, actor='admin', now=NOW)
            nwx = FundingManager.draw(conn, 'Bank', 'interbank', 'NWX', 1000, reference_key='nwx',
                                      rate_bps=365, maturity_days=10, now=NOW)
            FundingManager.draw(conn, 'Bank', 'interbank', 'USD', '123.45', reference_key='usd',
                                rate_bps=500, maturity_days=73, now=NOW)
            report = FundingManager.cashflow_projection(conn, 'Bank', now=NOW)
            rows = {row['funding_id']: row for row in report['cashflows']}
            self.assertEqual((rows[nwx]['principal_minor_units'], rows[nwx]['interest_minor_units']), (1000, 1))
            usd = next(row for row in rows.values() if row['currency_code'] == 'USD')
            self.assertEqual((usd['principal_minor_units'], usd['interest_minor_units']), (12345, 123))
            actual = FundingManager.repay(conn, nwx, now=NOW + 10 * DAY)
            self.assertEqual(int(actual['interest']), rows[nwx]['interest_minor_units'])
            self.assertEqual(len(FundingManager.cashflow_projection(conn, 'Bank', now=NOW + 10 * DAY)['cashflows']), 1)

    def test_overdue_projection_accrues_to_as_of_and_excludes_future_draws(self):
        with self.db.atomic() as conn:
            FundingManager.set_limit(conn, 'Bank', 'interbank', 'NWX', 10000, actor='admin', now=NOW)
            overdue = FundingManager.draw(conn, 'Bank', 'interbank', 'NWX', 1000, reference_key='old',
                                         rate_bps=365, maturity_days=1, now=NOW)
            FundingManager.draw(conn, 'Bank', 'interbank', 'NWX', 1000, reference_key='future',
                                rate_bps=365, maturity_days=1, now=NOW + 30 * DAY)
            rows = FundingManager.cashflow_projection(conn, 'Bank', now=NOW + 10 * DAY)['cashflows']
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['funding_id'], rows[0]['interest_minor_units'], rows[0]['overdue']),
                             (overdue, 1, True))

    def test_admin_projection_uses_bank_id_and_filters_real_rows(self):
        from web_api import server
        with self.db.atomic() as conn:
            conn.execute("UPDATE financial_institutions SET institution_id='BANK' WHERE issuer='Bank'")
            for currency, amount in [('NWX', 10000), ('USD', '10000.00')]:
                FundingManager.set_limit(conn, 'Bank', 'interbank', currency, amount, actor='admin', now=NOW)
                FundingManager.draw(conn, 'Bank', 'interbank', currency, 1000, reference_key=currency,
                                    rate_bps=365, maturity_days=10, now=NOW)
            with patch.object(server, 'connect_readonly', return_value=nullcontext(conn)), \
                 patch.object(server.time, 'time', return_value=NOW):
                report = server.admin_funding_cashflow('bank', 'USD')
                self.assertEqual(report['issuer'], 'Bank')
                self.assertEqual(len(report['cashflows']), 1)
                self.assertEqual(report['cashflows'][0]['currency_code'], 'USD')
                self.assertEqual(report['cashflows'][0]['principal_minor_units'], '100000')
                with self.assertRaises(ValueError):
                    server.admin_funding_cashflow('UNKNOWN')


if __name__ == '__main__':
    unittest.main()
