"""Game policy rates and nationality-specific legal interest-rate caps."""

import math
import time
from contextlib import nullcontext

from functions.interest_calc import YEAR_SECONDS
from functions.admin_access import resolve_user_role


RATE_CHART_PREFIX = "__rate__:"
RATE_NAMES = {"base_rate": "기준금리", "legal_max_rate": "법정최고이자"}


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

    @staticmethod
    def from_connection(conn, key, nationality=None, *, require_country=False):
        if key not in RATE_NAMES:
            raise ValueError("지원하지 않는 금리입니다.")
        if nationality is not None:
            row = conn.execute(
                f"SELECT {key} rate FROM country_currencies WHERE nationality=?",
                (str(nationality),),
            ).fetchone()
            if not row and require_country:
                raise ValueError("국가 ID를 확인해주세요.")
            if row:
                rate = float(row["rate"])
            else:
                fallback = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                rate = float(fallback["value"] if fallback else 0)
        else:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            rate = float(row["value"] if row else 0)
        if not math.isfinite(rate) or not 0 <= rate <= 100:
            raise ValueError("금리는 0~100%의 유한한 숫자여야 합니다.")
        return rate

    def get_rate(self, key, nationality=None):
        if key not in self.KEYS:
            raise ValueError("지원하지 않는 금리입니다.")
        with self.db.readonly() as conn:
            return self.from_connection(conn, key, nationality, require_country=nationality is not None)

    @staticmethod
    def for_exchange(conn, key, exchange_id):
        row = conn.execute(
            "SELECT nationality FROM market_exchanges WHERE exchange_id=?",
            (str(exchange_id or ''),),
        ).fetchone()
        return RateManager.from_connection(
            conn, key, row['nationality'] if row and row['nationality'] else None
        )

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

    def set_country_rate(self, key, nationality, percent, actor_id, now=None, conn=None):
        """Set one country's rate without changing any other nationality."""
        if resolve_user_role(actor_id, self.db.users.get_user(str(actor_id))) != "admin":
            return False, "❌ 금리 설정은 관리자 전용입니다."
        if key not in self.KEYS:
            return False, "❌ 지원하지 않는 금리입니다."
        try:
            rate = float(percent)
            if isinstance(percent, bool) or not math.isfinite(rate) or not 0 <= rate <= 100:
                raise ValueError("연이율은 0~100%여야 합니다.")
            wall_now = time.time()
            now = float(wall_now if now is None else now)
            if not math.isfinite(now) or now < 0:
                raise ValueError("적용 시각은 유효한 UNIX 시각이어야 합니다.")
            country = str(nationality or "").strip()
            if not country:
                raise ValueError("국적을 입력해주세요.")
            with (self.db.atomic(("system",)) if conn is None else nullcontext(conn)) as conn:
                updated = conn.execute(
                    f"UPDATE country_currencies SET {key}=? WHERE nationality=?",
                    (rate, country),
                )
                if updated.rowcount != 1:
                    raise ValueError("통화와 연결된 국적을 확인해주세요.")
                # The SQL-browser trigger records ordinary direct edits.  This
                # managed path replaces that wall-clock row with the caller's
                # precise effective timestamp (important for deterministic jobs/tests).
                conn.execute(
                    "DELETE FROM country_rate_history WHERE nationality=? AND rate_key=? "
                    "AND annual_percent=? AND changed_by='sql' AND effective_epoch BETWEEN ? AND ?",
                    (country, key, rate, wall_now-2, wall_now+2),
                )
                conn.execute(
                    "INSERT INTO country_rate_history"
                    "(nationality,rate_key,annual_percent,effective_epoch,changed_by) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(nationality,rate_key,effective_epoch) "
                    "DO UPDATE SET annual_percent=excluded.annual_percent,changed_by=excluded.changed_by",
                    (country, key, rate, now, str(actor_id)),
                )
                if key in {"base_rate", "legal_max_rate"}:
                    self.reprice_floating_products(conn, country)
            suffix = (" 기존 초과 상품 이율은 새 상한으로 조정했습니다."
                      if key == "legal_max_rate" else "")
            return True, (
                f"✅ {country} {self.KEYS[key]}를 연 {rate:g}%로 설정했습니다. "
                f"연동된 변동금리 상품도 재산정했습니다.{suffix}"
            )
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    @staticmethod
    def legal_max_for_currency(conn, currency_code):
        """Return the strictest country cap for a currency shared by countries."""
        rows = conn.execute(
            "SELECT nationality, legal_max_rate FROM country_currencies "
            "WHERE currency_code=? ORDER BY nationality",
            (str(currency_code or "NWX").upper(),),
        ).fetchall()
        if rows:
            row = min(rows, key=lambda item: float(item["legal_max_rate"]))
            return float(row["legal_max_rate"]), row["nationality"]
        fallback = RateManager.from_connection(conn, "legal_max_rate")
        return fallback, "기본"

    @staticmethod
    def legal_max_for_exchange(conn, exchange_id, quote_currency="NWX"):
        row = conn.execute(
            "SELECT c.nationality, c.legal_max_rate FROM market_exchanges e "
            "JOIN country_currencies c ON c.nationality=e.nationality "
            "WHERE e.exchange_id=?",
            (str(exchange_id or ""),),
        ).fetchone()
        if row:
            return float(row["legal_max_rate"]), row["nationality"]
        return RateManager.legal_max_for_currency(conn, quote_currency)

    @staticmethod
    def require_legal_rate(conn, annual_rate, *, nationality=None,
                           currency_code=None, exchange_id=None):
        """Validate an interest-bearing product rate against its country cap."""
        rate = float(annual_rate)
        if not math.isfinite(rate) or rate < 0:
            raise ValueError("상품 연이율은 0 이상의 유한한 숫자여야 합니다.")
        if nationality is not None:
            cap = RateManager.from_connection(
                conn, "legal_max_rate", str(nationality), require_country=True
            )
            country = str(nationality)
        elif exchange_id is not None:
            cap, country = RateManager.legal_max_for_exchange(
                conn, exchange_id, currency_code or "NWX"
            )
        else:
            cap, country = RateManager.legal_max_for_currency(
                conn, currency_code or "NWX"
            )
        if rate > cap:
            raise ValueError(
                f"상품 연이율 {rate:g}%는 {country} 법정최고이자 {cap:g}%를 넘을 수 없습니다."
            )
        return rate

    @staticmethod
    def rate_at(conn, key, nationality, epoch):
        """Return the policy rate that was effective at a historical instant."""
        row = conn.execute(
            "SELECT annual_percent FROM country_rate_history "
            "WHERE nationality=? AND rate_key=? AND effective_epoch<=? "
            "ORDER BY effective_epoch DESC LIMIT 1",
            (str(nationality), key, float(epoch)),
        ).fetchone()
        if row:
            return float(row["annual_percent"])
        return RateManager.from_connection(
            conn, key, str(nationality), require_country=True
        )

    @staticmethod
    def floating_rate(conn, nationality, spread=0, floor=0, cap=None, *, epoch=None):
        """Price a floating contract as base + spread within all applicable bounds."""
        country = str(nationality or "").strip()
        if not country:
            raise ValueError("변동금리 상품에는 기준금리 적용 국적이 필요합니다.")
        base = (RateManager.rate_at(conn, "base_rate", country, epoch)
                if epoch is not None else
                RateManager.from_connection(conn, "base_rate", country, require_country=True))
        legal = (RateManager.rate_at(conn, "legal_max_rate", country, epoch)
                 if epoch is not None else
                 RateManager.from_connection(conn, "legal_max_rate", country, require_country=True))
        spread, floor = float(spread or 0), float(floor or 0)
        ceiling = legal if cap is None else min(legal, float(cap))
        values = (base, legal, spread, floor, ceiling)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("금리 산정값은 유한한 숫자여야 합니다.")
        if not -100 <= spread <= 100 or not 0 <= floor <= 100 or not 0 <= ceiling <= 100:
            raise ValueError("금리 스프레드·하한·상한 설정을 확인해주세요.")
        if floor > ceiling:
            raise ValueError("상품 금리 하한은 적용 상한보다 클 수 없습니다.")
        return min(ceiling, max(floor, base + spread))

    @staticmethod
    def reprice_floating_products(conn, nationality):
        """Refresh advertised rates; signed contracts reset on their own schedule."""
        country = str(nationality)
        for table in ("saving", "liability"):
            rows = conn.execute(
                f"SELECT product_code,base_rate_spread,rate_floor,rate_cap "
                f"FROM {table} WHERE rate_type='floating' AND rate_nationality=?",
                (country,),
            ).fetchall()
            for row in rows:
                rate = RateManager.floating_rate(
                    conn, country, row["base_rate_spread"], row["rate_floor"], row["rate_cap"]
                )
                conn.execute(
                    f"UPDATE {table} SET interest_rate=? WHERE product_code=?",
                    (rate, row["product_code"]),
                )

    @staticmethod
    def interest_between(conn, key, principal, start, end, spread=0, compound=False,
                         nationality=None):
        """Integrate dated annual rates; never apply a new rate to old elapsed time."""
        if end <= start or principal <= 0:
            return 0.0
        rate = RateManager.from_connection(conn, key, nationality)
        return float(principal) * max(0, rate + spread) / 100 * (end - start) / YEAR_SECONDS
