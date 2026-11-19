"""Verification of standalone SQLite backups (tools/verify_backup.py): retired 2026-11-20 with SQLite support; the tests are kept
below as a comment. Restored PostgreSQL dumps are verified by tests/test_m8_backup_restore_drill.py."""

# """Recovery checks operate on snapshots, without changing the inspected file."""
# import json
# import sqlite3
# import subprocess
# import sys
# import tempfile
# import unittest
# from pathlib import Path
#
# ROOT = Path(__file__).resolve().parents[1]
# TOOL = ROOT / 'tools' / 'verify_backup.py'
#
#
# class BackupVerificationTests(unittest.TestCase):
#     def run_check(self, path, *options):
#         return subprocess.run([sys.executable, str(TOOL), str(path), *options], capture_output=True, text=True)
#
#     def test_healthy_snapshot_is_read_only(self):
#         with tempfile.TemporaryDirectory() as directory:
#             path = Path(directory) / 'backup with spaces.db'
#             with sqlite3.connect(path) as db:
#                 db.execute('CREATE TABLE items (id INTEGER PRIMARY KEY)')
#                 db.execute('INSERT INTO items VALUES (1)')
#             before = path.read_bytes()
#             result = self.run_check(path)
#             self.assertEqual(result.returncode, 0, result.stderr)
#             self.assertTrue(json.loads(result.stdout)['ok'])
#             self.assertEqual(path.read_bytes(), before)
#
#     def test_required_tables_must_exist_as_tables(self):
#         with tempfile.TemporaryDirectory() as directory:
#             path = Path(directory) / 'backup.db'
#             with sqlite3.connect(path) as db:
#                 db.execute('CREATE TABLE users (id INTEGER PRIMARY KEY)')
#                 db.execute('CREATE VIEW accounts AS SELECT * FROM users')
#             result = self.run_check(path, '--require-table', 'users', '--require-table', 'accounts')
#             self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
#             self.assertEqual(json.loads(result.stdout)['missing_tables'], ['accounts'])
#
#     def test_project_check_constraints_are_supported(self):
#         with tempfile.TemporaryDirectory() as directory:
#             path = Path(directory) / 'backup.db'
#             with sqlite3.connect(path) as db:
#                 db.create_function('valid_cost_basis', 1, lambda value: 1, deterministic=True)
#                 db.execute('CREATE TABLE assets (basis TEXT CHECK(valid_cost_basis(basis)))')
#                 db.execute("INSERT INTO assets VALUES ('12')")
#             result = self.run_check(path)
#             self.assertEqual(result.returncode, 0, result.stdout)
#
#     def test_foreign_key_violation_fails(self):
#         with tempfile.TemporaryDirectory() as directory:
#             path = Path(directory) / 'broken.db'
#             with sqlite3.connect(path) as db:
#                 db.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
#                 db.execute('CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))')
#                 db.execute('INSERT INTO child VALUES (10)')
#             result = self.run_check(path)
#             self.assertEqual(result.returncode, 1)
#             self.assertEqual(json.loads(result.stdout)['foreign_key_violations'][0]['table'], 'child')
#
#     def test_missing_or_non_database_fails_without_creating_file(self):
#         with tempfile.TemporaryDirectory() as directory:
#             path = Path(directory) / 'missing.db'
#             result = self.run_check(path)
#             self.assertNotEqual(result.returncode, 0)
#             self.assertFalse(path.exists())
#             path.write_text('not sqlite')
#             self.assertNotEqual(self.run_check(path).returncode, 0)
#             path.write_bytes(b'')
#             self.assertNotEqual(self.run_check(path).returncode, 0)
#
#
# if __name__ == '__main__':
#     unittest.main()
