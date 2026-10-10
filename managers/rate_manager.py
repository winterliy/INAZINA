"""Game economic rates and dated, non-retroactive interest calculations."""

import math
import time
from contextlib import nullcontext

from functions.interest_calc import YEAR_SECONDS
from functions.money import round_money
from functions.admin_access import resolve_user_role


RATE_CHART_PREFIX = "__rate__:"
RATE_NAMES = {"base_rate": "기준금리", "statutory_rate": "법정이자"}


def rate_key_from_code(code):
    """Resolve reserved chart codes and the two Korean rate names."""
    if not isinstance(code, str):
        return None
    value = code.strip()
    if value.startswith(RATE_CHART_PREFIX):
        key = value[len(RATE_CHART_PREFIX):]
        return key if key in RATE_NAMES else None
    return next((key for key, name in RATE_NAMES.items() if value == name), None)


class RateManager:
    KEYS = RATE_NAMES
    RATE_CHART_PREFIX = RATE_CHART_PREFIX
    rate_key_from_code = staticmethod(rate_key_from_code)

    def __init__(self, db):
        self.db = db

    @staticmethod
    def chart_code(key):
        key = key if key in RATE_NAMES else rate_key_from_code(key)
        if key is None:
            raise ValueError("지원하지 않는 금리입니다.")
        return RATE_CHART_PREFIX + key

    def get_rate_history(self, key, since, now=None):
        """Return a right-continuous annual-rate series, with both plot boundaries.

        Unlike disposable price movement rows, dated rates are also needed for
        interest accrual. This is a read-only view of their original history.
        Fractional change timestamps and percentages are deliberately preserved.
        """
        key = key if key in self.KEYS else rate_key_from_code(key)
        if key is None:
            raise ValueError("지원하지 않는 금리입니다.")
        start = float(since)
        end = float(time.time() if now is None else now)
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError("조회 기간은 시작 시각 이후의 유효한 UNIX 시각이어야 합니다.")
        rate = self.get_rate(key)
        return [(start, rate)] if start == end else [(start, rate), (end, rate)]

    def get_rate(self, key):
        if key not in self.KEYS:
            raise ValueError("지원하지 않는 금리입니다.")
        rate = float(self.db.system.get_setting(key, "0"))
        if not math.isfinite(rate) or not 0 <= rate <= 100:
            raise ValueError("금리는 0~100%의 유한한 숫자여야 합니다.")
        return rate

    def set_rate(self, key, percent, actor_id, now=None, conn=None):
        if resolve_user_role(actor_id, self.db.users.get_user(str(actor_id))) != "admin":
            return False, "❌ 금리 설정은 관리자 전용입니다."
        if key not in self.KEYS:
            return False, "❌ 지원하지 않는 금리입니다."
        try:
            rate = float(percent)
            if isinstance(percent, bool) or not math.isfinite(rate) or not 0 <= rate <= 100:
                raise ValueError("연이율은 0~100%여야 합니다.")
            now = float(time.time() if now is None else now)
            if not math.isfinite(now) or now < 0:
                raise ValueError("적용 시각은 유효한 UNIX 시각이어야 합니다.")
            with (self.db.atomic(("system",)) if conn is None else nullcontext(conn)) as conn:
                conn.execute("INSERT INTO system.settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(rate)))
            return True, f"✅ {self.KEYS[key]}를 연 {rate:g}%로 설정했습니다. 변경 시점 이후부터 적용됩니다."
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    @staticmethod
    def interest_between(conn, key, principal, start, end, spread=0, compound=False):
        """Integrate dated annual rates; never apply a new rate to old elapsed time."""
        if end <= start or principal <= 0:
            return 0.0
        row = conn.execute("SELECT value FROM system.settings WHERE key = ?", (key,)).fetchone()
        rate = float(row["value"] if row else 0)
        return float(principal) * max(0, rate + spread) / 100 * (end - start) / YEAR_SECONDS

    def statutory_interest(self, principal, start, end):
        with self.db.atomic(("system",)) as conn:
            return round_money(self.interest_between(conn, "statutory_rate", principal, start, end))
