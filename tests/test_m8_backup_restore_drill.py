"""An isolated restore exercise never overwrites the source backup."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / 'tools' / 'backup_restore_drill.py'


class BackupRestoreDrillTests(unittest.TestCase):
    def run_drill(self, path, *options):
        return subprocess.run([sys.executable, str(TOOL), str(path), *options], capture_output=True, text=True)

    def test_restores_and_verifies_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'backup.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE entries (id INTEGER PRIMARY KEY)')
                db.execute('INSERT INTO entries VALUES (42)')
            before = path.read_bytes()
            result = self.run_drill(path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['ok'])
            self.assertGreaterEqual(report['elapsed_seconds'], 0)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(list(Path(temp).iterdir()), [path])

    def test_required_tables_are_checked_on_restored_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'backup.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE users (id INTEGER PRIMARY KEY)')
            good = self.run_drill(path, '--require-table', 'users')
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
            missing = self.run_drill(path, '--require-table', 'users', '--require-table', 'accounts')
            self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
            self.assertEqual(json.loads(missing.stdout)['verification']['missing_tables'], ['accounts'])

    def test_business_reconciliation_rejects_incomplete_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'backup.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE users (id INTEGER PRIMARY KEY)')
            result = self.run_drill(path, '--reconcile')
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertFalse(json.loads(result.stdout)['ok'])

    def test_business_reconciliation_runs_on_isolated_restored_database(self):
        from database import DatabaseManager
        from database.backup import backup
        from database.db_manager import DBManager
        with tempfile.TemporaryDirectory() as temp:
            old_root = DBManager.database_dir
            try:
                DBManager.database_dir = Path(temp)
                manager = DatabaseManager()
                path = backup(Path(temp) / 'inazina.db')
                manager.close()
                before = path.read_bytes()
                result = self.run_drill(path, '--reconcile')
                report = json.loads(result.stdout)
                self.assertIn('reconciliation', report, result.stdout + result.stderr)
                self.assertIn(report['reconciliation']['status'], ('matched', 'mismatch'))
                self.assertIn('close_ready', report['reconciliation'])
                self.assertEqual(result.returncode, 0 if report['ok'] else 1)
                self.assertEqual(path.read_bytes(), before)
            finally:
                DBManager.database_dir = old_root

    def test_rejects_source_with_wal_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'backup.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE entries (id INTEGER)')
            Path(str(path) + '-wal').write_bytes(b'pending wal')
            result = self.run_drill(path)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)['ok'])

    def test_rejects_source_with_rollback_journal_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'backup.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE entries (id INTEGER)')
            Path(str(path) + '-journal').write_bytes(b'pending journal')
            result = self.run_drill(path)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)['ok'])

    def test_invalid_source_and_foreign_key_mismatch_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'backup.db'
            path.write_bytes(b'')
            self.assertNotEqual(self.run_drill(path).returncode, 0)
            path.unlink()
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
                db.execute('CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))')
                db.execute('INSERT INTO child VALUES (1)')
            result = self.run_drill(path)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)['verification']['foreign_key_violations'][0]['table'], 'child')


if __name__ == '__main__':
    unittest.main()
