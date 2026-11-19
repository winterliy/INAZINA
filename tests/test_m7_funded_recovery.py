import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.credit_risk_manager import CreditRiskManager
from managers.funded_recovery_manager import record_funded_recovery


class FundedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.old = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.users.create_user('admin', 'Admin', '낙원', 0, role='admin')
        self.db.users.create_user('borrower', 'Borrower', '낙원', 200)
        self.db.products.execute("INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency) VALUES ('LR','loan',5,1000,365,86400,0,'Bank','NWX')")
        self.db.accounts.execute("INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,created_at,collection_status,quote_currency) VALUES ('borrower','LR',100,100,'defaulted',datetime('now'),'defaulted','NWX')")
        self.loan = self.db.accounts.execute_one("SELECT loan_id FROM loans WHERE product_code='LR'")[0]
        with self.db.atomic() as conn:
            CreditRiskManager.charge_off_loan(conn, loan_id=self.loan, event_key='chargeoff', actor_id='admin', now=1800000000)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old
        self.tmp.cleanup()

    def test_cash_debit_recovery_journal_and_replay(self):
        with self.db.atomic() as conn:
            first = record_funded_recovery(conn, loan_id=self.loan, amount_minor_units=40, event_key='recovery', actor_id='admin', now=1800000001)
            replay = record_funded_recovery(conn, loan_id=self.loan, amount_minor_units=40, event_key='recovery', actor_id='admin', now=1800000002)
            self.assertEqual(conn.execute("SELECT cash_minor FROM users WHERE user_id='borrower'").fetchone()[0], 160)
            self.assertEqual(conn.execute('SELECT charged_off_balance_minor_units FROM loans WHERE loan_id=?', (self.loan,)).fetchone()[0], 60)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM business_cash_events WHERE kind='funded_chargeoff_recovery'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_journal_entries WHERE event_type='loan_chargeoff_recovery'").fetchone()[0], 1)
        self.assertFalse(first['duplicate'])
        self.assertTrue(replay['duplicate'])

    def test_insufficient_and_reused_key_do_not_debit(self):
        with self.db.atomic() as conn:
            with self.assertRaises(ValueError):
                record_funded_recovery(conn, loan_id=self.loan, amount_minor_units=201, event_key='bad', actor_id='admin', now=1800000001)
            record_funded_recovery(conn, loan_id=self.loan, amount_minor_units=40, event_key='same', actor_id='admin', now=1800000001)
            with self.assertRaises(ValueError):
                record_funded_recovery(conn, loan_id=self.loan, amount_minor_units=20, event_key='same', actor_id='admin', now=1800000002)
            self.assertEqual(conn.execute("SELECT cash_minor FROM users WHERE user_id='borrower'").fetchone()[0], 160)

    def test_failure_after_debit_rolls_back_and_insufficient_wallet_rejected(self):
        from unittest.mock import patch
        with self.db.atomic() as conn:
            with patch.object(CreditRiskManager, 'record_chargeoff_recovery', side_effect=RuntimeError('posting failed')):
                with self.assertRaises(RuntimeError):
                    record_funded_recovery(conn, loan_id=self.loan, amount_minor_units=40, event_key='fail', actor_id='admin', now=1800000001)
            self.assertEqual(conn.execute("SELECT cash_minor FROM users WHERE user_id='borrower'").fetchone()[0], 200)
            self.assertEqual(conn.execute('SELECT charged_off_balance_minor_units FROM loans WHERE loan_id=?', (self.loan,)).fetchone()[0], 100)
            conn.execute("UPDATE users SET cash_minor=10 WHERE user_id='borrower'")
            with self.assertRaises(ValueError):
                record_funded_recovery(conn, loan_id=self.loan, amount_minor_units=40, event_key='poor', actor_id='admin', now=1800000001)
