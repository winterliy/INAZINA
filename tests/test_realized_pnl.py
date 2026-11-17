"""Realized profit and loss is recorded where a sale, a futures close or a game settles."""
import unittest
from unittest import mock

from managers.pnl_ledger import PnlLedger
from tests import test_market_features as fixtures


class RealizedPnlLedgerTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue
    issue_future = fixtures.MarketFeatureTests.issue_future

    def _rows(self, user, kind=None):
        with self.db.readonly() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM user_realized_pnl WHERE user_id=?" + (" AND kind=?" if kind else "") + " ORDER BY event_id",
                (user, kind) if kind else (user,))]

    def test_a_sale_below_cost_is_recorded_as_a_loss_and_one_above_as_a_gain(self):
        self.db.assets.add_user_asset("alice", "CO", 4, unit_cost=100)
        self.assertTrue(self.book.place_limit_order("alice", "CO", "sell", 2, 90)[0])
        self.assertTrue(self.book.place_limit_order("bob", "CO", "buy", 2, 90)[0])
        self.db.companies.update_share_price("CO", 140)       # the treasury's primary quote is now above the 130 ask
        self.assertTrue(self.book.place_limit_order("alice", "CO", "sell", 2, 130)[0])
        self.assertTrue(self.book.place_limit_order("bob", "CO", "buy", 2, 130)[0])
        rows = self._rows("alice", "stock_sale")
        self.assertEqual([(r["proceeds_minor_units"], r["cost_minor_units"], r["pnl_minor_units"]) for r in rows],
                         [(180, 200, -20), (260, 200, 60)])
        self.assertEqual({r["asset_code"] for r in rows}, {"CO"})
        self.assertGreater(rows[1]["tax_minor_units"], 0)                      # the gain is taxed, the loss is not
        self.assertEqual(rows[0]["tax_minor_units"], 0)

    def test_a_futures_close_records_its_realized_result(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 2, 100)[0])
        self.db.companies.update_share_price("CO", 110)
        self.futures.settle_positions()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "sell", 2, 110)[0])
        rows = self._rows("alice", "futures")
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["pnl_minor_units"], 0)
        self.assertEqual(rows[0]["cost_minor_units"], 0)

    def test_a_game_records_the_stake_lost_and_the_winnings(self):
        with mock.patch("managers.user_manager.random.random", return_value=0.99):      # always loses
            self.users.gamble("alice", 100)
        with mock.patch("managers.user_manager.random.random", return_value=0.0):       # always wins
            self.users.gamble("alice", 100)
        rows = self._rows("alice", "gamble")
        self.assertEqual([r["pnl_minor_units"] for r in rows], [-100, 100])

    def test_recording_is_idempotent_and_append_only(self):
        with self.db.atomic() as conn:
            self.assertTrue(PnlLedger.record(conn, "alice", "stock_sale", "r1", "NWX", 0, 50, 70, now=1))
            self.assertFalse(PnlLedger.record(conn, "alice", "stock_sale", "r1", "NWX", 0, 50, 70, now=2))
            with self.assertRaisesRegex(Exception, "append-only"):
                conn.execute("UPDATE user_realized_pnl SET pnl_minor_units=0")


if __name__ == "__main__":
    unittest.main()
