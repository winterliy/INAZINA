"""Online backups are consistent snapshots and old ones are pruned."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from database.backup import backup


class BackupTests(unittest.TestCase):
    def test_backup_copies_and_prunes(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "inazina.db"
            with sqlite3.connect(db) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE t (v INTEGER)")
                connection.execute("INSERT INTO t VALUES (42)")
            stamps = iter(f"2026010{i}-000000" for i in range(1, 6))
            with patch("database.backup.time.strftime", side_effect=lambda *_: next(stamps)):
                files = [backup(db, keep=3) for _ in range(5)]
            remaining = sorted((Path(root) / "backups").glob("*.db"))
            self.assertEqual(remaining, files[-3:])
            with sqlite3.connect(files[-1]) as copy:
                self.assertEqual(copy.execute("SELECT v FROM t").fetchone()[0], 42)


if __name__ == "__main__":
    unittest.main()
