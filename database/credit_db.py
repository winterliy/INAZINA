"""Append-only credit bureau records: events, score history and model policy."""
from .db_manager import DBManager


CREDIT_EVENT_TYPES = (
    "payment_on_time", "payment_missed", "payment_cured", "loan_closed",
    "acceleration", "default", "manual_adjustment", "external_adjustment",
    "legacy_baseline",
)

# (key, default, description). Durations are real days, compressed from
# bureau-like years to weeks/months for the simulator's pace; administrators
# can tune them further by editing credit_score_policy.
# Earlier year-scale defaults. Rows still holding one are moved to the current
# default; values an administrator changed are left alone.
LEGACY_POLICY_DEFAULTS = {
    "on_time_half_life_days": 730, "closed_half_life_days": 1095,
    "missed_half_life_days": 180, "missed_retention_days": 1095,
    "acceleration_half_life_days": 365, "default_half_life_days": 730,
    "derogatory_retention_days": 1825, "history_full_days": 1825,
    "new_credit_window_days": 90, "legacy_half_life_days": 365,
    "refresh_interval_seconds": 86400,
}

CREDIT_POLICY_DEFAULTS = (
    ("anchor", 700, "이력이 없는 차주의 기준점수"),
    ("on_time_points", 4, "정상 납부 1회 가점"),
    ("on_time_half_life_days", 60, "정상 납부 가점 반감기"),
    ("on_time_cap", 120, "정상 납부 가점 상한"),
    ("closed_points", 20, "대출 완납 1건 가점"),
    ("closed_half_life_days", 90, "완납 가점 반감기"),
    ("closed_cap", 60, "완납 가점 상한"),
    ("missed_points", 35, "납부 연체 1회 감점"),
    ("cured_points", 15, "연체 회차 사후 납부 시 회복 가점"),
    ("missed_half_life_days", 14, "연체·회복 영향 반감기"),
    ("missed_retention_days", 90, "연체·회복 기록 반영 기간"),
    ("acceleration_points", 120, "기한이익상실 감점"),
    ("acceleration_half_life_days", 30, "기한이익상실 반감기"),
    ("default_points", 250, "부도 감점"),
    ("default_half_life_days", 60, "부도 반감기"),
    ("derogatory_retention_days", 180, "기한이익상실·부도 기록 반영 기간"),
    ("unpaid_installment_points", 20, "미납 상태 회차 1건 감점(해소 시 즉시 소멸)"),
    ("unpaid_installment_cap", 100, "미납 회차 감점 상한"),
    ("overdue_loan_points", 60, "만기 경과 연체 중 대출 1건 감점"),
    ("accelerated_loan_points", 150, "기한이익상실 상태 대출 1건 감점"),
    ("defaulted_loan_points", 250, "부도 상태 미상환 대출 1건 감점"),
    ("delinquency_cap", 400, "현재 연체 상태 감점 상한"),
    ("debt_low_ratio", 0.2, "가점 받는 부채/유효소득 비율 상한"),
    ("debt_low_bonus", 15, "저부채 가점"),
    ("debt_high_ratio", 1.0, "부채/유효소득 고위험 기준"),
    ("debt_high_penalty", 60, "고위험 기준에서의 감점"),
    ("debt_excess_slope", 40, "고위험 기준 초과 비율 1.0당 추가 감점"),
    ("debt_floor", 150, "부채수준 감점 상한"),
    ("debt_no_income_penalty", 0, "유효소득 미등록 상태에서 부채가 있을 때 감점(기본 0: 판단 보류)"),
    ("history_max_points", 40, "신용거래기간 최대 가점"),
    ("history_full_days", 180, "최대 가점에 도달하는 거래기간"),
    ("new_credit_window_days", 14, "신규 대출 집계 기간"),
    ("new_credit_points", 10, "기간 내 두 번째 이후 신규 대출 1건 감점"),
    ("new_credit_cap", 30, "신규 대출 감점 상한"),
    ("legacy_half_life_days", 30, "도입 전 점수가 모델보다 낮은 차이의 반감기(높은 차이는 유지)"),
    ("refresh_interval_seconds", 3600, "감쇠 반영을 위한 정기 재계산 주기"),
)


class CreditDB(DBManager):
    def __init__(self):
        super().__init__("users")
        self._init_db()

    def _init_db(self):
        types = ",".join(f"'{name}'" for name in CREDIT_EVENT_TYPES)
        self.execute(f"""CREATE TABLE IF NOT EXISTS credit_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ({types})),
            reference TEXT NOT NULL,
            loan_id INTEGER,
            occurred_epoch REAL NOT NULL,
            recorded_epoch REAL NOT NULL,
            points REAL NOT NULL DEFAULT 0,
            actor_id TEXT,
            reason TEXT,
            UNIQUE(user_id, event_type, reference)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_credit_events_user "
                     "ON credit_events(user_id, occurred_epoch)")
        self.execute("""CREATE TABLE IF NOT EXISTS credit_score_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            previous_score INTEGER NOT NULL,
            new_score INTEGER NOT NULL CHECK(new_score BETWEEN 0 AND 1000),
            trigger TEXT NOT NULL,
            components TEXT NOT NULL,
            computed_epoch REAL NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_credit_score_history_user "
                     "ON credit_score_history(user_id, computed_epoch)")
        for table, label in (("credit_events", "credit events"),
                             ("credit_score_history", "credit score history")):
            for action in ("update", "delete"):
                self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action} "
                    f"BEFORE {action.upper()} ON {table} BEGIN "
                    f"SELECT RAISE(ABORT,'{label} are append-only'); END"
                )
        self.execute("""CREATE TABLE IF NOT EXISTS credit_score_policy (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL CHECK(value >= 0),
            description TEXT NOT NULL DEFAULT ''
        )""")
        self.conn.executemany(
            "INSERT OR IGNORE INTO credit_score_policy(key,value,description) VALUES (?,?,?)",
            CREDIT_POLICY_DEFAULTS,
        )
        defaults = {key: (value, description) for key, value, description in CREDIT_POLICY_DEFAULTS}
        self.conn.executemany(
            "UPDATE credit_score_policy SET value=?,description=? WHERE key=? AND value=?",
            [(*defaults[key], key, old) for key, old in LEGACY_POLICY_DEFAULTS.items()],
        )
        self.conn.commit()
