"""Regular trading hours: schedule arithmetic and the automatic close/reopen it drives."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from functions import trading_hours
from managers.market_session_manager import MarketSessionManager


def kst(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=trading_hours.KST).timestamp())


WEEKDAYS = trading_hours.normalize({"open": "09:00", "close": "15:30", "days": [1, 2, 3, 4, 5],
                                     "holidays": ["2026-11-03"]})


class ScheduleTests(unittest.TestCase):
    def test_empty_times_mean_24_hours(self):
        schedule = trading_hours.normalize({"open": "", "close": ""})
        self.assertFalse(schedule["enabled"])
        self.assertTrue(trading_hours.is_open(schedule, kst("2026-11-01 03:00")))
        self.assertEqual(trading_hours.describe(schedule), "24시간")
        self.assertEqual(trading_hours.open_intervals(schedule, 10, 20), [(10, 20)])
        self.assertFalse(trading_hours.from_row({"open_time": "", "close_time": ""})["enabled"])

    def test_invalid_schedules_are_rejected(self):
        for raw in ({"open": "09:00", "close": ""}, {"open": "16:00", "close": "09:00"},
                    {"open": "9시", "close": "15:30"}, {"open": "09:00", "close": "15:30", "days": []},
                    {"open": "09:00", "close": "15:30", "holidays": ["11/03"]}):
            with self.assertRaises(ValueError):
                trading_hours.normalize(raw)

    def test_open_close_weekends_and_holidays(self):
        self.assertTrue(trading_hours.is_open(WEEKDAYS, kst("2026-11-02 09:00")))   # Monday open
        self.assertFalse(trading_hours.is_open(WEEKDAYS, kst("2026-11-02 15:30")))  # close is exclusive
        self.assertFalse(trading_hours.is_open(WEEKDAYS, kst("2026-11-03 10:00")))  # holiday
        self.assertFalse(trading_hours.is_open(WEEKDAYS, kst("2026-11-07 10:00")))  # Saturday
        self.assertEqual(trading_hours.next_open(WEEKDAYS, kst("2026-11-02 16:00")), kst("2026-11-04 09:00"))
        self.assertEqual(trading_hours.next_open(WEEKDAYS, kst("2026-11-06 16:00")), kst("2026-11-09 09:00"))
        self.assertEqual(trading_hours.last_close(WEEKDAYS, kst("2026-11-04 08:00")), kst("2026-11-02 15:30"))

    def test_open_intervals_cover_only_sessions(self):
        intervals = trading_hours.open_intervals(WEEKDAYS, kst("2026-11-02 12:00"), kst("2026-11-04 10:00"))
        self.assertEqual(intervals, [(kst("2026-11-02 12:00"), kst("2026-11-02 15:30")),
                                     (kst("2026-11-04 09:00"), kst("2026-11-04 10:00"))])

    def test_row_round_trip(self):
        self.assertEqual(trading_hours.from_row(trading_hours.to_columns(WEEKDAYS)), WEEKDAYS)


class SessionHoursTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "낙원", 1000000, role="admin")
        self.db.companies.create_company("CO", "Company", "Industry", 100000, 10000, 10000, "admin", "EX")
        self.session = MarketSessionManager(self.db)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def set_hours(self, now, **raw):
        with self.db.atomic(("system",)) as conn:
            return MarketSessionManager.set_hours_conn(conn, "EX", raw, now)

    def test_existing_exchanges_stay_24_hours(self):
        state = self.session.get_state("EX", now=kst("2026-11-07 03:00"))
        self.assertEqual(state["status"], "open")
        self.assertEqual(state["open_time"], "")

    def test_closes_after_hours_and_reopens_next_session(self):
        state = self.set_hours(kst("2026-11-02 10:00"), open="09:00", close="15:30", days=[1, 2, 3, 4, 5])
        self.assertEqual(state["status"], "open")
        state = self.session.process_due(now=kst("2026-11-02 15:31"))["EX"]
        self.assertEqual((state["status"], state["reason"], state["scheduled_close"]),
                         ("closed", MarketSessionManager.HOURS_REASON, 1))
        self.assertEqual(state["halt_started_epoch"], kst("2026-11-02 15:30"))
        self.assertEqual(state["resume_epoch"], kst("2026-11-03 09:00"))
        self.assertIn("장 마감", self.session.trading_error(now=kst("2026-11-02 18:00"), asset_code="CO"))
        state = self.session.process_due(now=kst("2026-11-03 09:00"))["EX"]
        self.assertEqual(state["status"], "open")
        self.assertIsNone(self.session.trading_error(now=kst("2026-11-03 09:01"), asset_code="CO"))

    def test_manual_open_outside_hours_is_refused_and_clearing_hours_reopens(self):
        self.set_hours(kst("2026-11-07 10:00"), open="09:00", close="15:30", days=[1, 2, 3, 4, 5])
        ok, message = self.session.control("admin", "open", now=kst("2026-11-07 10:01"), exchange_id="EX")
        self.assertFalse(ok)
        self.assertIn("정규 거래 시간", message)
        state = self.set_hours(kst("2026-11-07 10:02"), open="", close="")
        self.assertEqual(state["status"], "open")

    def test_manual_close_is_not_reopened_by_the_schedule(self):
        self.set_hours(kst("2026-11-02 10:00"), open="09:00", close="15:30", days=[1, 2, 3, 4, 5])
        self.assertTrue(self.session.control("admin", "close", now=kst("2026-11-02 11:00"), exchange_id="EX")[0])
        state = self.session.process_due(now=kst("2026-11-03 09:30"))["EX"]
        self.assertEqual((state["status"], state["reason"]), ("closed", "휴장"))


if __name__ == "__main__":
    unittest.main()
