import unittest

import test_inazina as fixtures
from managers.insolvency_manager import policy, set_policy


class InsolvencyPolicyUpdateTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_update_preserves_unspecified_values_and_single_row(self):
        with self.db.atomic() as conn:
            set_policy(conn, "admin", "낙원", {"rehab_repay_percent": 40})
            set_policy(conn, "admin", "낙원", {"rehab_weeks": 8})
            result = policy(conn, "낙원")
            self.assertEqual(result["rehab_repay_percent"], 40)
            self.assertEqual(result["rehab_weeks"], 8)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM insolvency_policies WHERE nationality=?", ("낙원",)
            ).fetchone()[0], 1)
