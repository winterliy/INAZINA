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
        records = self.db.system.execute(
            "SELECT annual_percent, effective_epoch FROM rate_history "
            "WHERE rate_key = ? AND effective_epoch <= ? "
            "ORDER BY effective_epoch, history_id", (key, end),
        )
        if not records:
            # A future-only history has no effective rate yet. The current
            # setting may already contain that future value, so use it only
            # when this key has no dated history at all.
            has_history = self.db.system.execute_one(
                "SELECT 1 FROM rate_history WHERE rate_key = ? LIMIT 1", (key,),
            )
            rate = 0.0 if has_history else self.get_rate(key)
            return [(start, rate)] if start == end else [(start, rate), (end, rate)]

        # Initial rates are normally seeded at epoch zero. A manually shortened
        # history still must not move its first recorded change into the past.
        rate = 0.0
        for record in records:
            if float(record["effective_epoch"]) <= start:
                rate = float(record["annual_percent"])
            else:
                break
        points = [(start, rate)]
        for record in records:
            epoch = float(record["effective_epoch"])
            if epoch <= start:
                continue
            rate = float(record["annual_percent"])
            if points[-1][0] == epoch:
                points[-1] = (epoch, rate)
            else:
                points.append((epoch, rate))
        if points[-1][0] != end:
            points.append((end, rate))
        return points

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
                last_id = conn.execute("SELECT COALESCE(MAX(history_id), 0) FROM system.rate_history").fetchone()[0]
                conn.execute("INSERT INTO system.settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(rate)))
                recorded = conn.execute("SELECT history_id FROM system.rate_history "
                                        "WHERE history_id > ? AND rate_key = ?", (last_id, key)).fetchone()
                if recorded:
                    conn.execute("UPDATE system.rate_history SET effective_epoch = ?, changed_by = ? "
                                 "WHERE history_id = ?", (now, str(actor_id), recorded["history_id"]))
                else:
                    conn.execute("INSERT INTO system.rate_history "
                                 "(rate_key, annual_percent, effective_epoch, changed_by) VALUES (?, ?, ?, ?)",
                                 (key, rate, now, str(actor_id)))
            return True, f"✅ {self.KEYS[key]}를 연 {rate:g}%로 설정했습니다. 변경 시점 이후부터 적용됩니다."
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    @staticmethod
    def interest_between(conn, key, principal, start, end, spread=0, compound=False):
        """Integrate dated annual rates; never apply a new rate to old elapsed time."""
        if end <= start or principal <= 0:
            return 0.0
        records = conn.execute("SELECT annual_percent, effective_epoch FROM system.rate_history "
                               "WHERE rate_key = ? AND effective_epoch < ? "
                               "ORDER BY effective_epoch, history_id", (key, end)).fetchall()
        rate = 0.0
        cursor = float(start)
        total, balance = 0.0, float(principal)
        for record in records:
            epoch = float(record["effective_epoch"])
            if epoch <= start:
                rate = float(record["annual_percent"])
                continue
            duration = epoch - cursor
            interest = balance * max(0, rate + spread) / 100 * duration / YEAR_SECONDS
            total += interest
            if compound:
                balance += interest
            cursor, rate = epoch, float(record["annual_percent"])
        total += balance * max(0, rate + spread) / 100 * (end - cursor) / YEAR_SECONDS
        return total

    def statutory_interest(self, principal, start, end):
        with self.db.atomic(("system",)) as conn:
            return round_money(self.interest_between(conn, "statutory_rate", principal, start, end))
