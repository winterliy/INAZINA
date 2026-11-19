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
    def run_drill(self, path):
        return subprocess.run([sys.executable, str(TOOL), str(path)], capture_output=True, text=True)

    def test_restores_and_verifies_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'backup.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE entries (id INTEGER PRIMARY KEY)')
                db.execute('INSERT INTO entries VALUES (42)')
            before = path.read_bytes()
            result = self.run_drill(path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'])
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(list(Path(temp).iterdir()), [path])

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
