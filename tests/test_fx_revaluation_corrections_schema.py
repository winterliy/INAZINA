"""Schema generator contract for append-only FX revaluation corrections."""
import sqlite3
import unittest

from database.currency_db import ensure_fx_revaluation_runs


class FxRevaluationCorrectionsSchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        ensure_fx_revaluation_runs(self.conn.execute)

    def tearDown(self):
        self.conn.close()

    def insert(self, run_id=1, revision=1, places=2):
        return self.conn.execute(
            'INSERT INTO fx_revaluation_corrections '
            '(run_id,revision,decimal_places,position_minor_units,rate_text,pnl_base_minor_units,'
            'delta_base_minor_units,actor,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?)',
            (run_id, revision, places, 100, '3/2', 42, 2, 'admin', 1000))

    def test_uniqueness_constraints_and_append_only(self):
        first = self.insert().lastrowid
        self.assertGreater(first, 0)
        self.insert(revision=2)
        self.insert(run_id=2)
        for kwargs in ({}, {'revision': 0}, {'revision': -1}, {'revision': 3, 'places': 7}):
            with self.subTest(kwargs=kwargs), self.assertRaises(sqlite3.IntegrityError):
                self.insert(**kwargs)
        for sql in ('UPDATE fx_revaluation_corrections SET actor=\'other\'',
                    'DELETE FROM fx_revaluation_corrections'):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql)
        ensure_fx_revaluation_runs(self.conn.execute)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM fx_revaluation_corrections').fetchone()[0], 3)


if __name__ == '__main__':
    unittest.main()
