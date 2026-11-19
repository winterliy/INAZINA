import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.funding_manager import FundingManager
from managers.funding_maturity import process_due_funding

NOW = 2_000_000_000
DAY = 86400


class FundingMaturityTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.currency.execute("INSERT INTO financial_institutions(issuer,nationality) VALUES ('Bank','낙원')")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def test_due_order_limit_and_retry(self):
        with self.db.atomic() as conn:
            FundingManager.set_limit(conn, 'Bank', 'interbank', 'NWX', 10000, actor='admin', now=NOW)
            ids = [FundingManager.draw(conn, 'Bank', 'interbank', 'NWX', 1000, reference_key=str(day),
                   rate_bps=365, maturity_days=day, now=NOW) for day in (2, 1, 3)]
            result = process_due_funding(conn, now=NOW + 2 * DAY, limit=1)
            self.assertEqual([row['funding_id'] for row in result['repaid']], [ids[1]])
            result = process_due_funding(conn, now=NOW + 2 * DAY, limit=10)
            self.assertEqual([row['funding_id'] for row in result['repaid']], [ids[0]])
            self.assertEqual(process_due_funding(conn, now=NOW + 2 * DAY)['repaid'], [])
            self.assertEqual(conn.execute('SELECT status FROM institution_funding WHERE funding_id=?', (ids[2],)).fetchone()[0], 'active')

    def test_failure_rolls_back_one_row_and_processes_next(self):
        with self.db.atomic() as conn:
            FundingManager.set_limit(conn, 'Bank', 'interbank', 'NWX', 10000, actor='admin', now=NOW)
            ids = [FundingManager.draw(conn, 'Bank', 'interbank', 'NWX', 1000, reference_key=str(i),
                   rate_bps=365, maturity_days=1, now=NOW) for i in range(2)]
            original = FundingManager.repay
            def repay(c, funding_id, *, now):
                result = original(c, funding_id, now=now)
                if funding_id == ids[0]:
                    raise ValueError('simulated failure')
                return result
            with patch.object(FundingManager, 'repay', side_effect=repay):
                result = process_due_funding(conn, now=NOW + DAY)
            self.assertEqual([x['funding_id'] for x in result['failed']], [ids[0]])
            self.assertEqual([x['funding_id'] for x in result['repaid']], [ids[1]])
            self.assertEqual(conn.execute('SELECT status FROM institution_funding WHERE funding_id=?', (ids[0],)).fetchone()[0], 'active')
            self.assertEqual(conn.execute("SELECT count(*) FROM institution_funding_events WHERE funding_id=? AND event_type='repay'", (ids[0],)).fetchone()[0], 0)
            self.assertEqual(len(process_due_funding(conn, now=NOW + DAY)['repaid']), 1)

    def test_outer_rollback_and_limit_validation(self):
        with self.db.atomic() as conn:
            FundingManager.set_limit(conn, 'Bank', 'interbank', 'NWX', 10000, actor='admin', now=NOW)
            funding_id = FundingManager.draw(conn, 'Bank', 'interbank', 'NWX', 1000, reference_key='rollback',
                                             rate_bps=365, maturity_days=1, now=NOW)
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            with self.db.atomic() as conn:
                for invalid in (0, 501, True, 1.5):
                    with self.assertRaises(ValueError):
                        process_due_funding(conn, now=NOW + DAY, limit=invalid)
                process_due_funding(conn, now=NOW + DAY)
                raise RuntimeError('abort')
        self.assertEqual(self.db.currency.execute('SELECT status FROM institution_funding WHERE funding_id=?',
                                                  (funding_id,))[0][0], 'active')

    def test_requires_caller_transaction(self):
        with self.assertRaisesRegex(ValueError, 'transaction'):
            process_due_funding(self.db.currency.conn, now=NOW)


if __name__ == '__main__':
    unittest.main()
