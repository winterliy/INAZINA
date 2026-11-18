"""The daily backup follows the database the app runs on: a pg_dump file on PostgreSQL."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from database import pg_backend
from database.backup import backup_current, backup_postgres
from tests import test_market_features as fixtures


@unittest.skipUnless(pg_backend.enabled(), "PostgreSQL-specific backup")
class PostgresBackupTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def test_pg_dump_file_is_written_listable_and_pruned(self):
        with tempfile.TemporaryDirectory() as folder:
            first = backup_current(Path(folder) / "inazina.db", folder, keep=1)
            self.assertTrue(first.name.startswith("inazina-pg-") and first.stat().st_size > 0)
            listing = subprocess.run(["pg_restore", "--list", str(first)], capture_output=True, text=True)
            self.assertEqual(listing.returncode, 0, listing.stderr)
            self.assertIn("company", listing.stdout)
            import time
            time.sleep(1.1)
            second = backup_postgres(folder, keep=1)
            self.assertEqual(sorted(path.name for path in Path(folder).glob("inazina-pg-*.dump")), [second.name])
