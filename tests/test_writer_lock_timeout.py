import sqlite3
import unittest

import test_inazina as fixtures
from database.db_manager import _connect_unmanaged


class WriterLockTimeoutTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_timeout_leaves_connection_usable_for_retry(self):
        first = _connect_unmanaged(self.db.users.db_path, timeout=0.05)
        second = _connect_unmanaged(self.db.users.db_path, timeout=0.05)
        try:
            first.execute("PRAGMA busy_timeout=50")
            second.execute("PRAGMA busy_timeout=50")
            self.assertEqual(second.execute("PRAGMA busy_timeout").fetchone()[0], 50)
            first.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                second.execute("BEGIN IMMEDIATE")
            self.assertFalse(second.in_transaction)
            first.rollback()
            second.execute("BEGIN IMMEDIATE")
            second.commit()
        finally:
            first.close()
            second.close()
