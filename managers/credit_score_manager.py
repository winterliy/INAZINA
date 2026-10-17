"""Explainable, event-sourced personal credit score (0–1000).

The stored users.credit_score is a cache of compute(): an anchor plus
- payment history: on-time payments and paid-off loans add points, missed
  payments, accelerations and defaults subtract them; every event decays
  with its own half-life and stops counting after a retention period,
- current delinquency: unpaid installments and overdue/defaulted balances,
  removed as soon as the borrower cures them,
- debt burden: outstanding loans against stability-weighted income,
- credit history length and recently opened loans,
- adjustments: permanent administrator corrections and a decaying baseline
  that preserves each user's pre-model score at enrollment.

Weights follow the shape of bureau scorecards (payment history dominates)
but are simulator assumptions kept in credit_score_policy, not a calibrated
model of any real bureau.
"""
import json
import math
import time

from database.credit_db import CREDIT_POLICY_DEFAULTS
from managers.currency_manager import CurrencyManager, SCALE

DAY = 86400.0
MIN_SCORE, MAX_SCORE = 0, 1000

# event_type -> (points policy key, half-life key, retention key, sign)
EVENT_RULES = {
    "payment_on_time": ("on_time_points", "on_time_half_life_days", None, 1),
    "loan_closed": ("closed_points", "closed_half_life_days", None, 1),
    "payment_missed": ("missed_points", "missed_half_life_days", "missed_retention_days", -1),
    "payment_cured": ("cured_points", "missed_half_life_days", "missed_retention_days", 1),
    "acceleration": ("acceleration_points", "acceleration_half_life_days",
                     "derogatory_retention_days", -1),
    "default": ("default_points", "default_half_life_days", "derogatory_retention_days", -1),
}
FACTOR_LABELS = {
    "on_time": "정상 납부 실적",
    "closed": "대출 완납 실적",
    "missed": "납부 연체 기록",
    "cured": "연체 회차 사후 납부",
    "acceleration": "기한이익상실 기록",
    "default": "부도 기록",
    "unpaid_installments": "현재 미납 회차",
    "delinquent_loans": "현재 연체 중 대출",
    "debt_burden": "소득 대비 부채수준",
    "history_length": "신용거래기간",
    "new_credit": "최근 신규 대출",
    "manual": "관리자 정정",
    "legacy": "도입 전 점수 보정(감쇠 중)",
}


class CreditScoreManager:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------ model
    @staticmethod
    def policy(conn):
        values = {key: float(default) for key, default, _ in CREDIT_POLICY_DEFAULTS}
        for row in conn.execute("SELECT key,value FROM credit_score_policy"):
            values[str(row[0])] = float(row[1])
        return values

    @staticmethod
    def _decay(age_seconds, half_life_days, retention_days=None):
        age_days = max(0.0, age_seconds/DAY)
        if retention_days and age_days > retention_days:
            return 0.0
        if half_life_days <= 0:
            return 1.0
        return 0.5 ** (age_days/half_life_days)

    @classmethod
    def compute(cls, conn, user_id, now=None):
        """Return the unclamped total, the clamped score and its factor breakdown."""
        now = time.time() if now is None else float(now)
        p = cls.policy(conn)
        user_id = str(user_id)
        factors = dict.fromkeys(FACTOR_LABELS, 0.0)

        for event in conn.execute(
            "SELECT event_type,occurred_epoch,points FROM credit_events WHERE user_id=?",
            (user_id,),
        ):
            kind, age = event["event_type"], now-float(event["occurred_epoch"])
            if kind == "manual_adjustment" or kind == "external_adjustment":
                factors["manual"] += float(event["points"])
            elif kind == "legacy_baseline":
                factors["legacy"] += float(event["points"])*cls._decay(
                    age, p["legacy_half_life_days"])
            elif kind in EVENT_RULES:
                points_key, half_life_key, retention_key, sign = EVENT_RULES[kind]
                factors[points_key.removesuffix("_points")] += sign*p[points_key]*cls._decay(
                    age, p[half_life_key], p[retention_key] if retention_key else None)
        factors["on_time"] = min(factors["on_time"], p["on_time_cap"])
        factors["closed"] = min(factors["closed"], p["closed_cap"])

        unpaid = conn.execute(
            "SELECT COUNT(*) FROM loan_payment_schedule s JOIN loans l ON l.loan_id=s.loan_id "
            "WHERE l.user_id=? AND l.status IN ('active','defaulted') AND s.status='missed'",
            (user_id,),
        ).fetchone()[0]
        factors["unpaid_installments"] = -min(
            p["unpaid_installment_cap"], p["unpaid_installment_points"]*int(unpaid))

        home = None
        debt = 0.0
        delinquency = 0.0
        loans = conn.execute(
            "SELECT status,collection_status,outstanding_balance,quote_currency,maturity_epoch,"
            "CAST(strftime('%s',created_at) AS REAL) AS opened FROM loans WHERE user_id=?",
            (user_id,),
        ).fetchall()
        for loan in loans:
            balance = max(0.0, float(loan["outstanding_balance"] or 0))
            if loan["status"] not in ("active", "defaulted") or balance <= 0:
                continue
            home = home or CurrencyManager.home(conn, user_id)
            currency = str(loan["quote_currency"] or home)
            debt += balance if currency == home else CurrencyManager.convert(
                conn, int(round(balance*SCALE)), currency, home)/SCALE
            state = str(loan["collection_status"] or "current")
            if loan["status"] == "defaulted" or state == "defaulted":
                delinquency += p["defaulted_loan_points"]
            elif state == "accelerated":
                delinquency += p["accelerated_loan_points"]
            elif (state == "overdue" or (loan["maturity_epoch"] is not None
                                         and now > float(loan["maturity_epoch"])
                                         and state != "grace")):
                delinquency += p["overdue_loan_points"]
        factors["delinquent_loans"] = -min(p["delinquency_cap"], delinquency)

        user = conn.execute(
            "SELECT annual_income,income_stability FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        income = (max(0.0, float(user["annual_income"] or 0))
                  * max(0.0, min(1.0, float(user["income_stability"] or 0)))) if user else 0.0
        if debt > 0:
            if income <= 0:
                factors["debt_burden"] = -p["debt_no_income_penalty"]
            else:
                ratio = debt/income
                low, high = p["debt_low_ratio"], max(p["debt_low_ratio"]+1e-9, p["debt_high_ratio"])
                if ratio <= low:
                    burden = p["debt_low_bonus"]
                elif ratio <= high:
                    burden = p["debt_low_bonus"]-(p["debt_low_bonus"]+p["debt_high_penalty"])*(
                        (ratio-low)/(high-low))
                else:
                    burden = -p["debt_high_penalty"]-p["debt_excess_slope"]*(ratio-high)
                factors["debt_burden"] = max(-p["debt_floor"], burden)

        opened = [float(loan["opened"]) for loan in loans if loan["opened"] is not None]
        if opened and p["history_full_days"] > 0:
            factors["history_length"] = p["history_max_points"]*min(
                1.0, max(0.0, now-min(opened))/DAY/p["history_full_days"])
        recent = sum(1 for epoch in opened if now-epoch <= p["new_credit_window_days"]*DAY)
        # One new loan is ordinary; several in a short window signal stacking.
        factors["new_credit"] = -min(p["new_credit_cap"], p["new_credit_points"]*max(0, recent-1))

        raw = p["anchor"]+sum(factors.values())
        score = int(max(MIN_SCORE, min(MAX_SCORE, math.floor(raw+0.5))))
        return {"raw": raw, "score": score, "anchor": p["anchor"],
                "factors": {key: round(value, 2) for key, value in factors.items()}}

    # ---------------------------------------------------------------- writes
    @classmethod
    def _insert_event(cls, conn, user_id, event_type, reference, occurred_epoch, now,
                      *, loan_id=None, points=0.0, actor_id=None, reason=None):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO credit_events(user_id,event_type,reference,loan_id,"
            "occurred_epoch,recorded_epoch,points,actor_id,reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(user_id), event_type, str(reference), loan_id, float(occurred_epoch),
             float(now), float(points), actor_id, reason),
        )
        return cursor.rowcount == 1

    @classmethod
    def sync(cls, conn, user_id, now=None):
        """Enroll a user or absorb an outside edit before new evidence is recorded.

        Enrollment stores (current score − model) as a decaying baseline so
        that introducing the model never jumps anyone's score. An outside
        edit (SQL Browser, legacy code) becomes a permanent external adjustment.
        """
        now = time.time() if now is None else float(now)
        row = conn.execute(
            "SELECT credit_score,credit_score_computed FROM users WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
        if not row:
            return False
        stored = max(MIN_SCORE, min(MAX_SCORE, int(row["credit_score"] or 0)))
        if row["credit_score_computed"] is not None and int(row["credit_score_computed"]) == stored:
            return True
        model = cls.compute(conn, user_id, now)
        if row["credit_score_computed"] is None:
            kind, reference, actor, reason = "legacy_baseline", "enrollment", None, "신용모델 도입 시점 점수 보존"
        else:
            kind, reference, actor, reason = (
                "external_adjustment", f"drift:{now:.6f}:{time.time_ns()}", "external",
                f"모델 밖에서 {int(row['credit_score_computed'])}→{stored}로 수정됨")
        cls._insert_event(conn, user_id, kind, reference, now, now,
                          points=stored-model["raw"], actor_id=actor, reason=reason)
        conn.execute(
            "UPDATE users SET credit_score=?,credit_score_computed=?,credit_score_epoch=? "
            "WHERE user_id=?", (stored, stored, now, str(user_id)),
        )
        return True

    @classmethod
    def refresh(cls, conn, user_id, now=None, trigger="recompute"):
        """Recompute and store a user's score; returns it (None for unknown users)."""
        now = time.time() if now is None else float(now)
        if not cls.sync(conn, user_id, now):
            return None
        previous = int(conn.execute(
            "SELECT credit_score FROM users WHERE user_id=?", (str(user_id),)
        ).fetchone()[0] or 0)
        result = cls.compute(conn, user_id, now)
        conn.execute(
            "UPDATE users SET credit_score=?,credit_score_computed=?,credit_score_epoch=? "
            "WHERE user_id=?", (result["score"], result["score"], now, str(user_id)),
        )
        if result["score"] != previous:
            conn.execute(
                "INSERT INTO credit_score_history(user_id,previous_score,new_score,trigger,"
                "components,computed_epoch) VALUES (?,?,?,?,?,?)",
                (str(user_id), previous, result["score"], str(trigger),
                 json.dumps(result["factors"], ensure_ascii=False, sort_keys=True), now),
            )
        return result["score"]

    @classmethod
    def record(cls, conn, user_id, event_type, reference, *, occurred_epoch=None, now=None,
               loan_id=None, refresh=True):
        """Record one credit event idempotently (by user, type and reference)."""
        if event_type not in EVENT_RULES:
            raise ValueError(f"unsupported credit event: {event_type}")
        now = time.time() if now is None else float(now)
        occurred = now if occurred_epoch is None else min(float(occurred_epoch), now)
        cls.sync(conn, user_id, now)
        inserted = cls._insert_event(conn, user_id, event_type, reference, occurred, now,
                                     loan_id=loan_id)
        if refresh:
            cls.refresh(conn, user_id, now, trigger=event_type)
        return inserted

    @classmethod
    def set_score(cls, conn, user_id, target, actor_id, reason, now=None):
        """Administrator correction: the score becomes target now and keeps evolving."""
        now = time.time() if now is None else float(now)
        target = int(target)
        if not MIN_SCORE <= target <= MAX_SCORE:
            raise ValueError("신용점수는 0~1000 사이여야 합니다.")
        reason = str(reason or "").strip()[:200] or "관리자 정정"
        if not cls.sync(conn, user_id, now):
            raise ValueError("사용자를 찾을 수 없습니다.")
        model = cls.compute(conn, user_id, now)
        if model["score"] != target:
            cls._insert_event(conn, user_id, "manual_adjustment", f"admin:{now:.6f}:{time.time_ns()}", now, now,
                              points=target-model["raw"], actor_id=str(actor_id), reason=reason)
        return cls.refresh(conn, user_id, now, trigger="manual_adjustment")

    def refresh_due(self, now=None):
        """Re-score users whose last computation is older than the refresh interval."""
        now = time.time() if now is None else float(now)
        with self.db.atomic(("users", "accounts")) as conn:
            interval = self.policy(conn)["refresh_interval_seconds"]
            users = [row[0] for row in conn.execute(
                "SELECT user_id FROM users WHERE credit_score_epoch IS NULL "
                "OR credit_score_epoch<=? OR credit_score_computed IS NULL "
                "OR credit_score_computed<>credit_score",
                (now-interval,),
            )]
            for user_id in users:
                self.refresh(conn, user_id, now, trigger="periodic")
        return len(users)

    # ----------------------------------------------------------------- reads
    @classmethod
    def report(cls, conn, user_id, now=None, *, events=20, history=20):
        """Score breakdown with plain-language factors, recent events and changes."""
        now = time.time() if now is None else float(now)
        result = cls.compute(conn, user_id, now)
        stored = conn.execute(
            "SELECT credit_score FROM users WHERE user_id=?", (str(user_id),)
        ).fetchone()
        factors = [
            {"key": key, "label": FACTOR_LABELS[key], "points": value}
            for key, value in result["factors"].items() if abs(value) >= 0.5
        ]
        factors.sort(key=lambda item: item["points"])
        return {
            "score": int(stored[0]) if stored else result["score"],
            "modelScore": result["score"],
            "anchor": result["anchor"],
            "factors": factors,
            "events": [dict(row) for row in conn.execute(
                "SELECT event_type AS type,reference,loan_id AS loanId,occurred_epoch AS occurredEpoch,"
                "points,actor_id AS actorId,reason FROM credit_events WHERE user_id=? "
                "ORDER BY occurred_epoch DESC,event_id DESC LIMIT ?",
                (str(user_id), int(events)),
            )],
            "history": [dict(row) for row in conn.execute(
                "SELECT previous_score AS previous,new_score AS score,trigger,"
                "computed_epoch AS epoch FROM credit_score_history WHERE user_id=? "
                "ORDER BY history_id DESC LIMIT ?",
                (str(user_id), int(history)),
            )],
        }
