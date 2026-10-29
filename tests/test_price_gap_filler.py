import random
import sqlite3
import unittest

from managers.price_gap_filler import MAX_POINTS_PER_GAP, VIEWABLE_SECONDS, fill_price_gaps


class PriceGapFillerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE market_price_history (asset_code TEXT, price REAL, recorded_epoch INTEGER, "
                          "UNIQUE(asset_code, recorded_epoch))")
        rng = random.Random(1)
        price = 1000.0
        for epoch in range(10_000, 10_600):
            price *= 1 + rng.gauss(0, 0.001)
            self.conn.execute("INSERT INTO market_price_history VALUES ('CO', ?, ?)", (price, epoch))
        self.last = price

    def rows(self, start):
        return self.conn.execute("SELECT recorded_epoch, price FROM market_price_history WHERE asset_code='CO' "
                                 "AND recorded_epoch > ? ORDER BY recorded_epoch", (start,)).fetchall()

    def test_short_gap_gets_one_point_per_second_that_wanders_and_returns(self):
        added = fill_price_gaps(self.conn, now=10_599 + 300, rng=random.Random(7))
        filled = self.rows(10_599)
        self.assertEqual(added, {"CO": len(filled)})
        self.assertEqual([epoch for epoch, _ in filled], list(range(10_600, 10_899)))
        prices = [price for _, price in filled]
        self.assertTrue(all(price > 0 for price in prices))
        self.assertGreater(max(prices) - min(prices), 0)
        self.assertLess(abs(prices[-1] / self.last - 1), 0.05)

    def test_earlier_outage_inside_the_window_is_bridged_between_real_prices(self):
        self.conn.execute("DELETE FROM market_price_history WHERE recorded_epoch BETWEEN 10100 AND 10399")
        before, after = (self.conn.execute("SELECT price FROM market_price_history WHERE recorded_epoch=?", (epoch,)).fetchone()[0]
                         for epoch in (10_099, 10_400))
        fill_price_gaps(self.conn, now=10_640, rng=random.Random(5))
        filled = self.conn.execute("SELECT recorded_epoch, price FROM market_price_history WHERE recorded_epoch "
                                   "BETWEEN 10100 AND 10399 ORDER BY recorded_epoch").fetchall()
        self.assertEqual([epoch for epoch, _ in filled], list(range(10_100, 10_400)))
        self.assertLess(abs(filled[0][1] / before - 1), 0.05)
        self.assertLess(abs(filled[-1][1] / after - 1), 0.05)

    def test_long_gap_is_capped_and_never_reaches_outside_the_viewable_range(self):
        now = 10_599 + 40 * 86400
        self.conn.execute("INSERT INTO market_price_history VALUES ('CO', ?, ?)", (self.last, now - 20 * 86400))
        fill_price_gaps(self.conn, now=now, rng=random.Random(3))
        outside = self.conn.execute("SELECT COUNT(*) FROM market_price_history WHERE recorded_epoch > 10599 "
                                    "AND recorded_epoch < ?", (now - VIEWABLE_SECONDS,)).fetchone()[0]
        self.assertEqual(outside, 0)
        inside = self.rows(now - VIEWABLE_SECONDS)
        self.assertLessEqual(len(inside), 2 * MAX_POINTS_PER_GAP + 1)
        self.assertLess(inside[-1][0], now)

    def test_recent_history_and_stale_assets_are_left_alone(self):
        self.assertEqual(fill_price_gaps(self.conn, now=10_599 + 30), {})
        self.assertEqual(fill_price_gaps(self.conn, now=10_599 + VIEWABLE_SECONDS + 10), {})
        self.assertEqual(self.rows(10_599), [])

    def test_second_run_adds_nothing(self):
        fill_price_gaps(self.conn, now=20_000, rng=random.Random(2))
        self.assertEqual(fill_price_gaps(self.conn, now=20_000, rng=random.Random(2)), {})


if __name__ == "__main__":
    unittest.main()
