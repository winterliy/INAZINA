import unittest
from contextlib import contextmanager
from unittest.mock import patch

import test_inazina as fixtures


class PortfolioSnapshotTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_read_only_portfolio_uses_one_snapshot_and_no_manager_queries(self):
        original_readonly = self.db.readonly
        snapshot_count = 0

        @contextmanager
        def counted_readonly():
            nonlocal snapshot_count
            snapshot_count += 1
            with original_readonly() as connection:
                yield connection

        managers = (
            self.db.users, self.db.assets, self.db.products, self.db.companies,
            self.db.accounts, self.db.system, self.db.currency, self.db.corporate,
            self.db.els,
        )
        patches = []
        for manager in managers:
            patches.extend((
                patch.object(manager, "execute", side_effect=AssertionError("snapshot bypass")),
                patch.object(manager, "execute_one", side_effect=AssertionError("snapshot bypass")),
            ))
        self.db.readonly = counted_readonly
        try:
            for active_patch in patches:
                active_patch.start()
            result = self.bot.user_mgr.get_total_assets("citizen", settle=False)
        finally:
            for active_patch in reversed(patches):
                active_patch.stop()
            self.db.readonly = original_readonly

        self.assertEqual(snapshot_count, 1)
        self.assertEqual(result["cash"], 1_000_000)
        self.assertEqual(result["net_worth"], result["grand_total"] - result["loan_total"])


if __name__ == "__main__":
    unittest.main()
