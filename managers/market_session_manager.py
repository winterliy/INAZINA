"""Per-exchange trading halts with restart-safe automatic reopening."""

import time  # Kept as a patch point for legacy worker-clock tests.
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functions import trading_hours
from functions.time_utils import validate_epoch


class MarketSessionManager:
    # 3DSG is the over-the-counter market: unlisted companies sit there and still trade.
    UNLISTED = "3DSG"
    # CURRENCY is the always-on system exchange used by FX conversion and currency pairs.
    SYSTEM_EXCHANGE = "CURRENCY"

    # Reason shown when regular trading hours end. Such a close is flagged scheduled_close=1 and
    # reopens itself at the next session; the reason is display text only.
    HOURS_REASON = "장 마감"
    SIDE_CAR_SECONDS = 300
    CIRCUIT_SECONDS = {1: 1200, 2: 1200}
    KST = ZoneInfo("Asia/Seoul")
    LABELS = {"close": "휴장", "open": "개장", "pause": "장 일시정지",
              "resume": "장 재개", "circuit": "서킷브레이커", "sidecar": "사이드카"}

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _epoch(now, error_code="invalid_market_session_epoch"):
        try:
            return int(validate_epoch(now))
        except ValueError as exc:
            raise ValueError(error_code) from exc

    @classmethod
    def next_day_epoch(cls, now):
        now = cls._epoch(now, "invalid_market_session_day_boundary_epoch")
        current = datetime.fromtimestamp(now, cls.KST)
        return int((current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    @classmethod
    def normalize_exchange(cls, exchange_id):
        value = str(exchange_id or "").strip()
        if not value or len(value) > 100:
            raise ValueError("거래소 ID를 입력해주세요.")
        if value.casefold() == cls.SYSTEM_EXCHANGE.casefold():
            return cls.SYSTEM_EXCHANGE
        return value

    @classmethod
    def exchange_ids(cls, conn):
        return [str(row["exchange_id"]) for row in conn.execute(
            "SELECT exchange_id FROM market_exchanges UNION SELECT exchange_id FROM company "
            "UNION SELECT exchange_id FROM market_assets UNION SELECT exchange_id FROM investment "
            "UNION SELECT exchange_id FROM treasury_bond_products ORDER BY exchange_id"
        ) if row['exchange_id']]

    @classmethod
    def exchange_for_asset(cls, conn, asset_code):
        code = str(asset_code or "").strip()
        if not code:
            return None
        row = conn.execute("""SELECT exchange_id FROM market_assets WHERE code=?
            UNION ALL SELECT exchange_id FROM company WHERE company_code=?
            UNION ALL SELECT exchange_id FROM investment WHERE product_code=?
            UNION ALL SELECT exchange_id FROM treasury_bond_products WHERE product_code=?
            UNION ALL SELECT COALESCE(a.exchange_id,c.exchange_id,i.exchange_id,b.exchange_id)
              FROM option_contracts o
              LEFT JOIN market_assets a ON a.code=o.underlying_code
              LEFT JOIN company c ON c.company_code=o.underlying_code
              LEFT JOIN investment i ON i.product_code=o.underlying_code
              LEFT JOIN treasury_bond_products b ON b.product_code=o.underlying_code
              WHERE o.contract_code=?
            LIMIT 1""", (code, code, code, code, code)).fetchone()
        return cls.normalize_exchange(row["exchange_id"]) if row and row["exchange_id"] else None

    @classmethod
    def _row(cls, conn, exchange_id):
        exchange_id = cls.normalize_exchange(exchange_id)
        row = conn.execute("SELECT * FROM market_exchanges WHERE exchange_id=?", (exchange_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO market_exchanges(exchange_id,status,updated_epoch,trading_halted) VALUES (?, 'open', 0, 0)", (exchange_id,))
            row = conn.execute("SELECT * FROM market_exchanges WHERE exchange_id=?", (exchange_id,)).fetchone()
        return dict(row)

    @classmethod
    def _asset_scope(cls, exchange_id):
        sql = """SELECT code FROM market_assets WHERE exchange_id=?
            UNION SELECT company_code FROM company WHERE exchange_id=?
            UNION SELECT product_code FROM investment WHERE exchange_id=?
            UNION SELECT product_code FROM treasury_bond_products WHERE exchange_id=?
            UNION SELECT o.contract_code FROM option_contracts o
              LEFT JOIN market_assets a ON a.code=o.underlying_code
              LEFT JOIN company c ON c.company_code=o.underlying_code
              LEFT JOIN investment i ON i.product_code=o.underlying_code
              LEFT JOIN treasury_bond_products b ON b.product_code=o.underlying_code
              WHERE COALESCE(a.exchange_id,c.exchange_id,i.exchange_id,b.exchange_id)=?"""
        return sql, (exchange_id,) * 5

    @classmethod
    def _resume(cls, conn, exchange_id, state, resume_at):
        start = int(state["halt_started_epoch"] if state["halt_started_epoch"] is not None else resume_at)
        if resume_at > start:
            if not conn.execute("SELECT 1 FROM market_exchanges WHERE status='open' LIMIT 1").fetchone():
                conn.execute('UPDATE market_factor_state SET last_epoch=last_epoch+?', (resume_at-start,))
            scope, params = cls._asset_scope(exchange_id)
            conn.execute(f"UPDATE market_auto_targets SET start_epoch=start_epoch+?,end_epoch=end_epoch+? WHERE asset_code IN ({scope})", (resume_at-start,resume_at-start,*params))
            conn.execute(f"UPDATE market_event_targets SET "
                "end_epoch=end_epoch+?-MAX(start_epoch,?),"
                "last_tick_epoch=COALESCE(last_tick_epoch,start_epoch)+?-MAX(start_epoch,?),"
                f"start_epoch=start_epoch+?-MAX(start_epoch,?) WHERE state='active' AND asset_code IN ({scope})",
                (resume_at,start,resume_at,start,resume_at,start,*params))
            conn.execute(f"UPDATE market_simulation_state SET last_epoch=last_epoch+? WHERE asset_code IN ({scope})", (resume_at - start, *params))
        conn.execute("""UPDATE market_exchanges SET status='open', reason='', halt_started_epoch=NULL,
            resume_epoch=NULL, circuit_stage=NULL, updated_epoch=?, trading_halted=0, scheduled_close=0
            WHERE exchange_id=?""", (resume_at, exchange_id))

    @classmethod
    def process_conn(cls, conn, now=None, exchange_id=None):
        now = cls._epoch(now, "invalid_market_session_processing_epoch")
        if exchange_id is None:
            return cls.process_all_conn(conn, now)
        exchange_id = cls.normalize_exchange(exchange_id)
        state = cls._row(conn, exchange_id)
        if exchange_id == cls.SYSTEM_EXCHANGE:
            if state["status"] != "open":
                cls._resume(conn, exchange_id, state, now)
            if (state.get("trading_halted") or state.get("open_time") or state.get("close_time") or state.get("trading_days") != "1,2,3,4,5,6,7"
                    or state.get("holidays") not in (None, "", "[]") or state.get("scheduled_close")):
                conn.execute(
                    "UPDATE market_exchanges SET open_time='',close_time='',trading_days='1,2,3,4,5,6,7',"
                    "holidays='[]',scheduled_close=0,status='open',reason='',halt_started_epoch=NULL,"
                    "resume_epoch=NULL,circuit_stage=NULL,trading_halted=0,updated_epoch=? WHERE exchange_id=?",
                    (now, exchange_id),
                )
            return cls._row(conn, exchange_id)
        if state["status"] != "open" and state["resume_epoch"] is not None and int(state["resume_epoch"]) <= now:
            cls._resume(conn, exchange_id, state, int(state["resume_epoch"]))
            conn.execute('INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)',
                ('system','market_auto_resume',json.dumps({'exchange_id':exchange_id}),state['resume_epoch']))
            state = cls._row(conn, exchange_id)
        schedule = cls._hours_close_due(state, now)
        if schedule:
            # Prices stop from the moment the session ended, even if nobody was polling then.
            closed_at = min(now, max(trading_hours.last_close(schedule, now) or now, int(state["updated_epoch"] or 0)))
            conn.execute("""UPDATE market_exchanges SET status='closed', reason=?, halt_started_epoch=?,
                resume_epoch=?, circuit_stage=NULL, updated_epoch=?, trading_halted=1, scheduled_close=1
                WHERE exchange_id=?""",
                (cls.HOURS_REASON, closed_at, trading_hours.next_open(schedule, now), now, exchange_id))
            state = cls._row(conn, exchange_id)
        return state

    @staticmethod
    def _hours_close_due(state, now):
        """The schedule of an open exchange whose regular session has ended, else None."""
        if state["status"] != "open" or not state.get("open_time"):
            return None
        schedule = trading_hours.from_row(state)
        return None if trading_hours.is_open(schedule, now) else schedule

    @classmethod
    def set_hours_conn(cls, conn, exchange_id, schedule, now=None):
        """Validate and store regular hours, then apply them right away (close now, or reopen early)."""
        now = cls._epoch(now, "invalid_market_session_processing_epoch")
        if cls.normalize_exchange(exchange_id) == cls.SYSTEM_EXCHANGE:
            raise ValueError("CURRENCY 시스템 거래소의 운영시간은 변경할 수 없습니다.")
        schedule = trading_hours.normalize(schedule)
        state = cls._row(conn, exchange_id)
        exchange_id = state["exchange_id"]
        columns = trading_hours.to_columns(schedule)
        conn.execute(f"UPDATE market_exchanges SET {', '.join(f'{key}=?' for key in columns)} WHERE exchange_id=?",
                     (*columns.values(), exchange_id))
        if state["scheduled_close"]:
            if trading_hours.is_open(schedule, now):
                cls._resume(conn, exchange_id, state, now)
            else:
                conn.execute("UPDATE market_exchanges SET resume_epoch=? WHERE exchange_id=?",
                             (trading_hours.next_open(schedule, now), exchange_id))
        return cls.process_conn(conn, now, exchange_id)

    @classmethod
    def process_all_conn(cls, conn, now=None):
        now = cls._epoch(now, "invalid_market_session_processing_epoch")
        for exchange_id in cls.exchange_ids(conn):
            cls.process_conn(conn, now, exchange_id)
        return {exchange_id: cls._row(conn, exchange_id) for exchange_id in cls.exchange_ids(conn)}

    def process_due(self, now=None):
        now = self._epoch(now, "invalid_market_session_processing_epoch")
        # Most polls have nothing to resume.  Check from a WAL read snapshot so
        # the five-second worker does not compete for SQLite's single writer.
        with self.db.readonly() as conn:
            rows = conn.execute(
                "SELECT * FROM market_exchanges ORDER BY exchange_id"
            ).fetchall()
            states = {str(row["exchange_id"]): dict(row) for row in rows}
            due = any(
                (row["status"] != "open"
                 and row["resume_epoch"] is not None
                 and int(row["resume_epoch"]) <= now)
                or self._hours_close_due(row, now)
                for row in states.values()
            )
        if not due:
            return states
        with self.db.atomic(("system",)) as conn:
            return self.process_all_conn(conn, now)

    def get_state(self, exchange_id=None, now=None):
        now = self._epoch(now, "invalid_market_session_processing_epoch")
        with self.db.atomic(("system",)) as conn:
            return self.process_all_conn(conn, now) if exchange_id is None else self.process_conn(conn, now, exchange_id)

    @classmethod
    def error_for_state(cls, state, exchange_id):
        if state["status"] == "open":
            return None
        message = f"❌ `{exchange_id}` 거래소가 {state['reason'] or '정지'} 상태입니다. 개장·재개 후 거래할 수 있습니다."
        if state["resume_epoch"] is not None:
            message += f" 자동 재개: <t:{int(state['resume_epoch'])}:F>"
        return message

    def trading_error(self, conn=None, now=None, *, asset_code=None, exchange_id=None):
        now = self._epoch(now, "invalid_market_session_check_epoch")
        if conn is None:
            with self.db.atomic(("system",)) as transaction:
                return self.trading_error(transaction, now, asset_code=asset_code, exchange_id=exchange_id)
        exchange_id = exchange_id or self.exchange_for_asset(conn, asset_code)
        if exchange_id is None:
            return None
        if conn is None:
            with self.db.atomic(("system",)) as transaction:
                state = self.process_conn(transaction, now, exchange_id)
        else:
            state = self.process_conn(conn, now, exchange_id)
        return self.error_for_state(state, exchange_id)

    def control(self, actor_id, action, stage=None, now=None, exchange_id="ALL"):
        action = str(action).strip().casefold()
        if action not in self.LABELS:
            return False, "❌ 지원하지 않는 시장 상태 명령어입니다."
        if action == "circuit" and (isinstance(stage, bool) or stage not in (1, 2, 3)):
            return False, "❌ 서킷브레이커는 1·2·3단계 중 선택해주세요."
        now = self._epoch(now, "invalid_market_session_control_epoch")
        requested = str(exchange_id or "").strip()
        with self.db.atomic(("system", "users")) as conn:
            role = conn.execute("SELECT role FROM users WHERE user_id=?", (str(actor_id),)).fetchone()
            if not role or str(role["role"]).strip().casefold() != "admin":
                return False, "❌ 관리자 전용 기능입니다."
            if requested.casefold() == "all":
                all_exchanges = self.exchange_ids(conn)
                exchanges = [item for item in all_exchanges if item.casefold() != self.SYSTEM_EXCHANGE.casefold()]
            else:
                normalized = self.normalize_exchange(requested)
                if normalized == self.SYSTEM_EXCHANGE:
                    return False, "❌ CURRENCY는 환전 시스템 전용 거래소라 상태를 변경할 수 없습니다."
                exchanges = [normalized]
            if not exchanges:
                return False, "❌ 관리할 일반 거래소가 없습니다."
            outside_hours = []
            for current_exchange in exchanges:
                state = self.process_conn(conn, now, current_exchange)
                if action in {"open", "resume"}:
                    schedule = trading_hours.from_row(state)
                    if not trading_hours.is_open(schedule, now):
                        # It would close again on the next poll; it opens itself at the next session.
                        outside_hours.append((current_exchange, trading_hours.next_open(schedule, now)))
                        continue
                    if state["status"] != "open":
                        self._resume(conn, current_exchange, state, now)
                    continue
                if action in {"circuit", "sidecar"} and state["status"] == "closed":
                    return False, f"❌ `{current_exchange}` 거래소는 휴장 중입니다. 먼저 개장해주세요."
                if action == "circuit":
                    resume = now + self.CIRCUIT_SECONDS[stage] if stage in (1, 2) else self.next_day_epoch(now)
                    reason = f"서킷브레이커 {stage}단계"
                elif action == "sidecar":
                    resume, reason = now + self.SIDE_CAR_SECONDS, "사이드카"
                else:
                    resume, reason = None, self.LABELS[action]
                if resume is not None and state["status"] != "open":
                    if state["resume_epoch"] is None:
                        return False, f"❌ `{current_exchange}` 거래소는 수동 정지 상태입니다. 먼저 재개해주세요."
                    if int(state["resume_epoch"]) > resume:
                        return False, "❌ 현재 정지 기간보다 짧은 정지를 적용할 수 없습니다."
                halt_started = now if state["status"] == "open" else state["halt_started_epoch"]
                conn.execute("""UPDATE market_exchanges SET status=?, reason=?, halt_started_epoch=?,
                    resume_epoch=?, circuit_stage=?, updated_epoch=?, trading_halted=1, scheduled_close=0
                    WHERE exchange_id=?""",
                    ("closed" if action == "close" else "paused", reason, halt_started, resume,
                     stage if action == "circuit" else None, now, current_exchange))
        target = "전체 일반 거래소 (CURRENCY 제외)" if requested.casefold() == "all" else f"`{requested}` 거래소"
        if action in {"open", "resume"}:
            skipped = "".join(
                f"\n`{exchange}`는 정규 거래 시간이 아니라 그대로 두었습니다."
                + (f" 다음 개장: <t:{int(reopen)}:F>" if reopen else "")
                for exchange, reopen in outside_hours)
            if len(outside_hours) == len(exchanges):
                return False, "❌ " + skipped.removeprefix("\n") + " 지금 열려면 거래 시간을 24시간으로 바꿔 주세요."
            return True, f"✅ {target} {self.LABELS[action]}: 거래와 가격 변동을 재개했습니다.{skipped}"
        deadline = f"\n자동 재개: <t:{int(resume)}:F>" if resume is not None else ""
        return True, f"✅ {target} {reason}: 신규 거래와 가격 변동을 정지했습니다.{deadline}"
