import unittest

import test_inazina as fixtures
from managers.rate_manager import RateManager


class RateHistoryConflictTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_market_history_conflict_records_update(self):
        rates = RateManager(self.db)
        for value in (6, 7):
            ok, message = rates.set_market_rate("낙원", "bank_bond", 365, value, "admin", now=1_800_000_000)
            self.assertTrue(ok, message)
        rows = self.db.currency.execute(
            "SELECT operation FROM market_rate_curve_history_events "
            "WHERE nationality=? AND benchmark_key=? AND tenor_days=? "
            "AND before_rate=? AND after_rate=? ORDER BY event_id",
            ("낙원", "bank_bond", 365, 6, 7),
        )
        self.assertEqual([row[0] for row in rows], ["replace", "update"])
