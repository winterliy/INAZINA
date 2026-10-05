"""Persisted global trading/price halts, with restart-safe automatic reopening."""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class MarketSessionManager:
    SIDE_CAR_SECONDS = 300
    CIRCUIT_SECONDS = {1: 1200, 2: 1200}
    KST = ZoneInfo("Asia/Seoul")
    LABELS = {"close": "휴장", "open": "개장", "pause": "장 일시정지",
              "resume": "장 재개", "circuit": "서킷브레이커", "sidecar": "사이드카"}

    def __init__(self, db):
        self.db = db

    @classmethod
    def next_day_epoch(cls, now):
        current = datetime.fromtimestamp(now, cls.KST)
        return int((current + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())

    @staticmethod
    def _row(conn):
        return dict(conn.execute("SELECT * FROM market_session WHERE session_id=1").fetchone())

    @staticmethod
    def _resume(conn, state, resume_at):
        """Shift suspended model clocks while preserving contract maturities."""
        start = int(state["halt_started_epoch"] if state["halt_started_epoch"] is not None else resume_at)
        if resume_at > start:
            # An event created during a halt only freezes its own elapsed time.
            # Future scheduled events retain their relative waiting time too.
            conn.execute("""UPDATE market_events SET
                starts_epoch=starts_epoch + ? - MAX(?, MIN(starts_epoch, ?)),
                ends_epoch=ends_epoch + ? - MAX(?, MIN(starts_epoch, ?))
                WHERE event_id IN (SELECT event_id FROM market_event_targets WHERE state='active')
                   OR ends_epoch > ?""",
                (resume_at, start, resume_at, resume_at, start, resume_at, start))
            conn.execute("""UPDATE market_event_targets SET
                start_epoch=start_epoch + ? - MAX(?, MIN(start_epoch, ?)),
                end_epoch=end_epoch + ? - MAX(?, MIN(start_epoch, ?)),
                last_tick_epoch=CASE WHEN last_tick_epoch IS NULL THEN NULL
                    ELSE last_tick_epoch + ? - MAX(?, MIN(start_epoch, ?)) END
                WHERE state='active'""",
                (resume_at, start, resume_at, resume_at, start, resume_at,
                 resume_at, start, resume_at))
            conn.execute("""UPDATE market_auto_targets SET
                start_epoch=start_epoch + ?, end_epoch=end_epoch + ?""",
                (resume_at-start, resume_at-start))
            conn.execute("UPDATE market_simulation_state SET last_epoch=last_epoch+?",
                         (resume_at-start,))
            conn.execute("UPDATE market_factor_state SET last_epoch=last_epoch+?",
                         (resume_at-start,))
        conn.execute("""UPDATE market_session SET status='open', reason='',
            halt_started_epoch=NULL, resume_epoch=NULL, circuit_stage=NULL,
            updated_epoch=? WHERE session_id=1""", (resume_at,))

    @classmethod
    def process_conn(cls, conn, now=None):
        """Called inside the same transaction as an order or a price tick."""
        now = int(time.time() if now is None else now)
        state = cls._row(conn)
        if (state["status"] != "open" and state["resume_epoch"] is not None
                and int(state["resume_epoch"]) <= now):
            cls._resume(conn, state, int(state["resume_epoch"]))
            state = cls._row(conn)
        return state

    def process_due(self, now=None):
        with self.db.atomic(("system",)) as conn:
            return self.process_conn(conn, now)

    def get_state(self, now=None):
        return self.process_due(now)

    @classmethod
    def error_for_state(cls, state):
        if state["status"] == "open":
            return None
        message = f"❌ 시장이 {state['reason'] or '정지'} 상태입니다. 개장·재개 후 거래할 수 있습니다."
        if state["resume_epoch"] is not None:
            message += f" 자동 재개: <t:{int(state['resume_epoch'])}:F>"
        return message

    def trading_error(self, conn=None, now=None):
        state = self.process_conn(conn, now) if conn is not None else self.process_due(now)
        return self.error_for_state(state)

    def control(self, actor_id, action, stage=None, now=None):
        action = str(action).strip().casefold()
        if action not in self.LABELS:
            return False, "❌ 지원하지 않는 시장 상태 명령어입니다."
        if action == "circuit" and (isinstance(stage, bool) or stage not in (1, 2, 3)):
            return False, "❌ 서킷브레이커는 1·2·3단계 중 선택해주세요."
        now = int(time.time() if now is None else now)
        with self.db.atomic(("system", "users")) as conn:
            role = conn.execute("SELECT role FROM users WHERE user_id=?", (str(actor_id),)).fetchone()
            if not role or str(role["role"]).strip().casefold() != "admin":
                return False, "❌ 관리자 전용 기능입니다."
            state = self.process_conn(conn, now)
            if action in {"open", "resume"}:
                if state["status"] != "open":
                    self._resume(conn, state, now)
                else:
                    return True, "ℹ️ 시장은 이미 개장 상태입니다."
            else:
                if action in {"circuit", "sidecar"} and state["status"] == "closed":
                    return False, "❌ 휴장 중입니다. 먼저 $개장으로 거래를 재개해주세요."
                if action == "circuit":
                    resume = now + self.CIRCUIT_SECONDS[stage] if stage in (1, 2) else self.next_day_epoch(now)
                    reason = f"서킷브레이커 {stage}단계"
                elif action == "sidecar":
                    resume, reason = now + self.SIDE_CAR_SECONDS, "사이드카"
                else:
                    resume, reason = None, self.LABELS[action]
                # A shorter stop cannot shorten an already scheduled halt.
                if resume is not None and state["status"] != "open":
                    if state["resume_epoch"] is None:
                        return False, "❌ 수동 정지 상태입니다. $장재개로 먼저 재개해주세요."
                    if int(state["resume_epoch"]) > resume:
                        return False, "❌ 현재 정지 기간보다 짧은 정지를 적용할 수 없습니다."
                halt_started = now if state["status"] == "open" else state["halt_started_epoch"]
                conn.execute("""UPDATE market_session SET status=?, reason=?,
                    halt_started_epoch=?, resume_epoch=?, circuit_stage=?, updated_epoch=?
                    WHERE session_id=1""",
                    ("closed" if action == "close" else "paused", reason,
                     halt_started, resume, stage if action == "circuit" else None, now))
            current = self._row(conn)
        label = self.LABELS[action]
        if action in {"open", "resume"}:
            return True, f"✅ {label}: 거래와 가격 변동을 재개했습니다."
        deadline = f"\n자동 재개: <t:{int(current['resume_epoch'])}:F>" if current["resume_epoch"] is not None else ""
        return True, f"✅ {current['reason']}: 신규 거래와 가격 변동을 정지했습니다.{deadline}"
