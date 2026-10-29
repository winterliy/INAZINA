"""Per-exchange regular trading hours (KST): validation and open/close arithmetic.

An exchange whose ``open_time`` and ``close_time`` are both empty trades around the clock.
With both set it trades on ``trading_days`` (ISO weekdays, 1=월 … 7=일) between them, except on
``holidays`` (YYYY-MM-DD in KST).  Overnight sessions are not supported: the close is always
later on the same day than the open.
"""

import json
import re
from datetime import date, datetime, time as clock, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
WEEKDAY_LABELS = "월화수목금토일"
DEFAULT_HOURS = {"enabled": False, "open": "", "close": "", "days": [1, 2, 3, 4, 5], "holidays": []}
# Far enough to cover a long holiday run, short enough to keep a bad schedule cheap.
_SEARCH_DAYS = 400


def _minutes(value, label):
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(value or "").strip())
    if not match:
        raise ValueError(f"{label}은 HH:MM(00:00~23:59) 형식으로 입력해주세요.")
    return int(match[1]) * 60 + int(match[2])


def normalize(raw):
    """Validate an admin-supplied schedule and return it in canonical form."""
    raw = raw or {}
    open_time = str(raw.get("open") or "").strip()
    close_time = str(raw.get("close") or "").strip()
    # Both empty: a 24-hour market.  Only one empty is a mistake, not a schedule.
    enabled = bool(open_time or close_time)
    if enabled and not (open_time and close_time):
        raise ValueError("개장·폐장 시각을 모두 입력하거나, 24시간 개장이면 둘 다 비워 주세요.")
    if enabled and _minutes(open_time, "개장 시각") >= _minutes(close_time, "폐장 시각"):
        raise ValueError("폐장 시각은 개장 시각보다 늦어야 합니다.")
    try:
        days = sorted({int(day) for day in raw.get("days", DEFAULT_HOURS["days"])})
    except (TypeError, ValueError) as exc:
        raise ValueError("거래 요일을 다시 선택해주세요.") from exc
    if any(day not in range(1, 8) for day in days):
        raise ValueError("거래 요일을 다시 선택해주세요.")
    if enabled and not days:
        raise ValueError("거래 요일을 하루 이상 선택해주세요.")
    holidays = set()
    for value in raw.get("holidays") or []:
        try:
            holidays.add(date.fromisoformat(str(value).strip()).isoformat())
        except ValueError as exc:
            raise ValueError(f"휴장일 {value}은(는) YYYY-MM-DD 형식이어야 합니다.") from exc
    return {"enabled": enabled, "open": open_time, "close": close_time, "days": days, "holidays": sorted(holidays)}


def from_row(row):
    """The schedule stored on a ``market_exchanges`` row (empty or missing times mean 24 hours)."""
    row = dict(row or {})
    if not (row.get("open_time") and row.get("close_time")):
        return dict(DEFAULT_HOURS)
    try:
        holidays = json.loads(row.get("holidays") or "[]")
    except ValueError:
        holidays = []
    return {"enabled": True, "open": row["open_time"], "close": row["close_time"],
            "days": [int(day) for day in str(row.get("trading_days") or "").split(",") if day.strip()],
            "holidays": holidays}


def to_columns(schedule):
    return {"open_time": schedule["open"], "close_time": schedule["close"],
            "trading_days": ",".join(str(day) for day in schedule["days"]),
            "holidays": json.dumps(schedule["holidays"])}


def _session(schedule, day):
    """(open, close) epochs of the session on a KST calendar day, or None if it does not trade."""
    if day.isoweekday() not in schedule["days"] or day.isoformat() in schedule["holidays"]:
        return None
    open_minutes, close_minutes = _minutes(schedule["open"], "개장 시각"), _minutes(schedule["close"], "폐장 시각")
    start = datetime.combine(day, clock(open_minutes // 60, open_minutes % 60), KST)
    end = datetime.combine(day, clock(close_minutes // 60, close_minutes % 60), KST)
    return int(start.timestamp()), int(end.timestamp())


def is_open(schedule, epoch):
    if not schedule.get("enabled"):
        return True
    session = _session(schedule, datetime.fromtimestamp(int(epoch), KST).date())
    return bool(session) and session[0] <= int(epoch) < session[1]


def next_open(schedule, epoch):
    """The first session open at or after ``epoch``; None for 24-hour or never-trading schedules."""
    if not schedule.get("enabled"):
        return None
    day = datetime.fromtimestamp(int(epoch), KST).date()
    for offset in range(_SEARCH_DAYS):
        session = _session(schedule, day + timedelta(days=offset))
        if session and session[1] > int(epoch):
            return max(session[0], int(epoch))
    return None


def last_close(schedule, epoch):
    """The most recent session close at or before ``epoch`` (when the market last shut)."""
    if not schedule.get("enabled"):
        return None
    day = datetime.fromtimestamp(int(epoch), KST).date()
    for offset in range(_SEARCH_DAYS):
        session = _session(schedule, day - timedelta(days=offset))
        if session and session[1] <= int(epoch):
            return session[1]
    return None


def open_intervals(schedule, start, end):
    """[(a, b), …] sub-ranges of [start, end) during which the exchange traded."""
    start, end = int(start), int(end)
    if end <= start:
        return []
    if not schedule.get("enabled"):
        return [(start, end)]
    intervals = []
    day = datetime.fromtimestamp(start, KST).date()
    last = datetime.fromtimestamp(end, KST).date()
    while day <= last:
        session = _session(schedule, day)
        if session:
            a, b = max(session[0], start), min(session[1], end)
            if a < b:
                intervals.append((a, b))
        day += timedelta(days=1)
    return intervals


def describe(schedule):
    if not schedule.get("enabled"):
        return "24시간"
    days = "".join(WEEKDAY_LABELS[day - 1] for day in schedule["days"])
    return f"{days} {schedule['open']}~{schedule['close']}"
