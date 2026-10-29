import random
import sqlite3
import unittest
from datetime import datetime

from functions import trading_hours
from managers.price_gap_filler import MAX_POINTS_PER_GAP, VIEWABLE_SECONDS, fill_price_gaps


class PriceGapFillerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE market_price_history (asset_code TEXT, price REAL, recorded_epoch INTEGER,
                UNIQUE(asset_code, recorded_epoch));
            CREATE TABLE market_price_gap_fills (asset_code TEXT, start_epoch INTEGER, end_epoch INTEGER,
                points INTEGER, filled_epoch INTEGER);
            CREATE TABLE market_exchanges (exchange_id TEXT PRIMARY KEY, status TEXT, reason TEXT DEFAULT '',
                open_time TEXT DEFAULT '', close_time TEXT DEFAULT '', trading_days TEXT DEFAULT '1,2,3,4,5',
                holidays TEXT DEFAULT '[]');
            CREATE TABLE company (company_code TEXT, exchange_id TEXT, status TEXT);
            CREATE TABLE market_assets (code TEXT, exchange_id TEXT, status TEXT);
            CREATE TABLE investment (product_code TEXT, exchange_id TEXT, status TEXT);
            CREATE TABLE treasury_bond_products (product_code TEXT, exchange_id TEXT, status TEXT);
            INSERT INTO market_exchanges(exchange_id, status) VALUES ('EX', 'open');
            INSERT INTO company VALUES ('CO', 'EX', 'active');
        """)
        rng = random.Random(1)
        price = 1000.0
        for epoch in range(10_000, 10_600):
            price *= 1 + rng.gauss(0, 0.001)
            self.conn.execute("INSERT INTO market_price_history VALUES ('CO', ?, ?)", (price, epoch))
        self.last = price

    def tearDown(self):
        self.conn.close()

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

    def test_long_gap_coarse_steps_are_not_refilled_on_restart(self):
        now = 10_599 + 10 * 86400
        first = fill_price_gaps(self.conn, now=now, rng=random.Random(4))
        self.assertGreater(first["CO"], 0)
        self.assertEqual(fill_price_gaps(self.conn, now=now + 5, rng=random.Random(4)), {})

    def test_prices_are_whole_minor_units(self):
        fill_price_gaps(self.conn, now=10_599 + 300, rng=random.Random(7))
        self.assertTrue(all(float(price).is_integer() for _, price in self.rows(10_599)))

    def test_only_trading_hours_are_filled(self):
        def kst(text):
            return int(datetime.fromisoformat(text).replace(tzinfo=trading_hours.KST).timestamp())
        self.conn.execute("DELETE FROM market_price_history")
        self.conn.execute("UPDATE market_exchanges SET open_time='09:00', close_time='15:30', status='closed', reason='장 마감'")
        last = kst("2026-11-02 15:00")   # Monday afternoon; bot back on Wednesday 10:00
        self.conn.execute("INSERT INTO market_price_history VALUES ('CO', 1000, ?)", (last,))
        fill_price_gaps(self.conn, now=kst("2026-11-04 10:00"), rng=random.Random(9))
        for epoch, _ in self.rows(last):
            self.assertTrue(trading_hours.is_open(trading_hours.from_row({"open_time": "09:00", "close_time": "15:30",
                                                                          "trading_days": "1,2,3,4,5"}), epoch))
        self.assertTrue(self.rows(last))

    def test_closed_paused_unlisted_and_inactive_assets_are_skipped(self):
        now = 10_599 + 300
        for setup in ("UPDATE market_exchanges SET status='closed', reason='휴장'",
                      "UPDATE market_exchanges SET status='paused', reason='사이드카'",
                      "UPDATE company SET exchange_id='UNLISTED'",
                      "UPDATE company SET status='delisted'"):
            with self.subTest(setup=setup):
                self.conn.execute("SAVEPOINT s")
                self.conn.execute(setup)
                self.assertEqual(fill_price_gaps(self.conn, now=now, rng=random.Random(1)), {})
                self.conn.execute("ROLLBACK TO s")
                self.conn.execute("RELEASE s")


if __name__ == "__main__":
    unittest.main()
