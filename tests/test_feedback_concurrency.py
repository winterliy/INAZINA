import tempfile
import threading
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from web_api import server


class FeedbackConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_dir, self.old_database = DBManager.database_dir, server.DATABASE
        DBManager.database_dir = Path(self.tempdir.name)
        server.DATABASE = Path(self.tempdir.name) / "inazina.db"
        self.db = DatabaseManager()
        server._feedback_state.update(ready=None, purged=0.0)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir, server.DATABASE = self.old_dir, self.old_database
        self.tempdir.cleanup()

    def test_simultaneous_reads_do_not_deadlock(self):
        # The dashboard asks for the feedback list twice at once (two tabs, a refresh); on PostgreSQL the second
        # request used to die with "deadlock detected" inside the table setup.
        errors = []

        def read():
            try:
                server.admin_feedback()
            except Exception as error:  # noqa: BLE001
                errors.append(repr(error))

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(server.admin_feedback()["items"], [])


if __name__ == "__main__":
    unittest.main()
