"""SQLite-compatible epoch extraction for bound account timestamps."""
import unittest

import test_inazina as fixtures


class EpochTranslationTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_strftime_epoch_uses_bound_timestamp(self):
        with self.db.readonly() as conn:
            epoch = conn.execute("SELECT CAST(strftime('%s',?) AS REAL)",
                                 ('2000-01-01 00:00:00',)).fetchone()[0]
        self.assertEqual(epoch, 946684800)
