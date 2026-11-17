"""회생·파산 인가의 실제 처리.

파산 인가: 미체결 주문을 취소하고 주식·채권·예금을 처분해 현금으로 모은 뒤, 국가별
면제재산을 남기고 나머지를 본국 통화 대출에 잔액 비율대로 배당합니다. 남은 대출은
면책(상각 후 법적 채무 메모까지 소멸)하고, 신용점수를 내리고, 일정 기간 대출을 막습니다.
계정은 막지 않습니다.

회생 인가: 자산은 그대로 둡니다. 변제 총액(대출의 일정 비율, 단 청산가치 이상)을
N주로 나눠 매주 자동 변제하고, 그동안 이자·연체·정기상환은 멈춥니다. 모두 갚으면
남은 대출을 면책하고, 허용 횟수를 넘겨 못 갚으면 회생이 폐지되어 원래 빚이 되살아납니다.

수치는 국가(nationality)별 insolvency_policies 행으로 관리하고, 행이 없으면 DEFAULTS를 씁니다.
외화 대출은 배당 대상이 아니며 면책만 됩니다.
"""
import json
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from functions.money import base_currency_code
from managers.currency_manager import CurrencyManager, SCALE

WEEK = 604800
DAY = 86400

# 2026-11-14 확정 기본값. 변경 방법은 docs/insolvency.md
DEFAULTS = {
    "exempt_amount": 1_000_000,       # 파산 시 남겨 두는 현금(본국 통화)
    "bankruptcy_score": 300,          # 파산 면책 후 신용점수 상한(0~1000)
    "bankruptcy_ban_days": 30,        # 파산 면책 후 대출 금지 기간(일)
    "rehab_score": 450,               # 회생 인가 시 신용점수 상한
    "rehab_weeks": 12,                # 변제 기간 기본값(주)
    "rehab_min_weeks": 4,
    "rehab_max_weeks": 20,
    "rehab_repay_percent": 50,        # 최소 변제율(대출 잔액 대비 %), 청산가치가 더 크면 그 금액
    "rehab_max_missed": 1,            # 허용 미납 횟수, 초과하면 회생 폐지
    "rehab_ban_days_after": 14,       # 회생 완료 후 추가 대출 금지(변제 중에는 항상 금지)
    "rediscard_days": 90,             # 면책 후 다시 인가받을 수 없는 기간(일)
}
FIELDS = tuple(DEFAULTS)


def ensure_tables(conn):
    cols = ",".join(f"{key} REAL" for key in FIELDS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS insolvency_policies (nationality TEXT PRIMARY KEY, {cols}, "
                 "updated_by TEXT, updated_epoch INTEGER)")
    conn.execute("""CREATE TABLE IF NOT EXISTS insolvency_cases (
        case_id INTEGER PRIMARY KEY AUTOINCREMENT, filing_id INTEGER NOT NULL UNIQUE,
        user_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('rehabilitation','bankruptcy')),
        status TEXT NOT NULL CHECK(status IN ('active','completed','revoked','discharged')),
        currency TEXT NOT NULL, actor_id TEXT NOT NULL,
        debt_units INTEGER NOT NULL DEFAULT 0, plan_units INTEGER NOT NULL DEFAULT 0,
        weekly_units INTEGER NOT NULL DEFAULT 0, paid_units INTEGER NOT NULL DEFAULT 0,
        weeks_total INTEGER NOT NULL DEFAULT 0, weeks_paid INTEGER NOT NULL DEFAULT 0,
        missed INTEGER NOT NULL DEFAULT 0, next_due_epoch INTEGER,
        detail TEXT NOT NULL DEFAULT '{}', created_epoch INTEGER NOT NULL, closed_epoch INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_insolvency_cases_user ON insolvency_cases(user_id, status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS insolvency_restrictions (
        user_id TEXT PRIMARY KEY, loan_ban_until INTEGER NOT NULL DEFAULT 0, last_discharge_epoch INTEGER)""")


def policy(conn, nationality):
    ensure_tables(conn)
    row = conn.execute("SELECT * FROM insolvency_policies WHERE nationality=?", (str(nationality),)).fetchone()
    result = dict(DEFAULTS)
    if row:
        for key in FIELDS:
            if row[key] is not None:
                result[key] = row[key]
    return result


def policies(conn):
    ensure_tables(conn)
    rows = conn.execute("SELECT c.nationality,c.currency_code,p.nationality FROM country_currencies c "
                        "LEFT JOIN insolvency_policies p ON p.nationality=c.nationality ORDER BY 1").fetchall()
    return [{"nationality": r[0], "currency": r[1], "custom": r[2] is not None, **policy(conn, r[0])} for r in rows]


def set_policy(conn, actor, nationality, values):
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM country_currencies WHERE nationality=?", (str(nationality),)).fetchone():
        raise ValueError("국가를 찾을 수 없습니다.")
    current = policy(conn, nationality)
    for key, value in (values or {}).items():
        if key not in DEFAULTS:
            continue
        number = float(value)
        if number < 0 or (key.endswith("_score") and number > 1000) or (key == "rehab_repay_percent" and number > 100):
            raise ValueError(f"{key} 값이 범위를 벗어났습니다.")
        current[key] = number
    if not 1 <= current["rehab_min_weeks"] <= current["rehab_weeks"] <= current["rehab_max_weeks"]:
        raise ValueError("회생 기간은 최소~최대 주 사이여야 합니다.")
    updates = ','.join(f"{field}=excluded.{field}" for field in (*FIELDS, 'updated_by', 'updated_epoch'))
    conn.execute(f"INSERT INTO insolvency_policies(nationality,{','.join(FIELDS)},updated_by,updated_epoch) "
                 f"VALUES (?,{','.join('?' * len(FIELDS))},?,?) "
                 f"ON CONFLICT(nationality) DO UPDATE SET {updates}",
                 (str(nationality), *[current[k] for k in FIELDS], str(actor), int(time.time())))
    return current


def loan_ban_until(conn, user_id, now=None):
    """대출 금지 종료 시각(없으면 0). 회생 변제 중이면 무기한."""
    now = time.time() if now is None else now
    try:
        if conn.execute("SELECT 1 FROM insolvency_cases WHERE user_id=? AND status='active'", (str(user_id),)).fetchone():
            return float("inf")
        row = conn.execute("SELECT loan_ban_until FROM insolvency_restrictions WHERE user_id=?", (str(user_id),)).fetchone()
    except Exception:  # 테이블이 아직 없으면 제한도 없음
        return 0
    return row[0] if row and row[0] > now else 0


def loan_ban_message(conn, user_id):
    until = loan_ban_until(conn, user_id)
    if not until:
        return None
    if until == float("inf"):
        return "❌ 회생 변제 중에는 새 대출·연장을 할 수 없습니다."
    return f"❌ 회생·파산 면책에 따라 <t:{int(until)}:D>까지 대출이 제한됩니다."


def frozen_users(conn):
    """회생 변제 중이라 이자·연체·정기상환을 멈추는 사용자."""
    try:
        return {str(r[0]) for r in conn.execute("SELECT user_id FROM insolvency_cases WHERE status='active'")}
    except Exception:
        return set()


def _units(value):
    return int((Decimal(str(value or 0)) * SCALE).to_integral_value(rounding=ROUND_CEILING))


class InsolvencyManager:
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    def _home_debt(self, conn, user_id, home):
        """본국 통화 대출 목록과 합계(SCALE 단위)."""
        loans = [(row, _units(row["outstanding_balance"])) for row in conn.execute(
            "SELECT loan_id,product_code,outstanding_balance,quote_currency FROM loans "
            "WHERE user_id=? AND status IN ('active','defaulted') AND outstanding_balance>0", (str(user_id),)).fetchall()
            if (row["quote_currency"] or home) == home]
        return loans, sum(units for _, units in loans)

    def _pay_pro_rata(self, user_id, budget):
        """budget(SCALE 단위, 본국 통화)을 본국 통화 대출에 잔액 비율로 상환. 실제 상환액 반환."""
        with self.db.readonly() as conn:
            home = CurrencyManager.home(conn, user_id)
            precision = CurrencyManager.precision(conn, home)
            loans, total = self._home_debt(conn, user_id, home)
        if budget <= 0 or total <= 0:
            return 0
        budget = min(budget, total)
        quantum = Decimal(10) ** -precision
        paid = 0
        for row, units in loans:
            share = Decimal(budget) * units / total / SCALE
            amount = min(share.quantize(quantum, rounding=ROUND_FLOOR), Decimal(units) / SCALE)
            if amount <= 0:
                continue
            ok, message = self.bot.bank_mgr.repay_loan(user_id, row["product_code"], str(amount), row["loan_id"])
            if not ok:
                raise ValueError(f"대출 #{row['loan_id']} 배당 상환 실패: {message}")
            paid += int(amount * SCALE)
        return paid

    def _discharge_all(self, conn, actor, user_id, reference):
        """남은 대출 전부 면책: 상각 분개 후 상각 채무 메모까지 소멸."""
        from managers.credit_risk_manager import CreditRiskManager
        now = time.time()
        discharged = []
        for row in conn.execute("SELECT loan_id FROM loans WHERE user_id=? AND status IN ('active','defaulted') "
                                "AND outstanding_balance>0", (str(user_id),)).fetchall():
            conn.execute("UPDATE loans SET status='defaulted',collection_status='defaulted',"
                         "defaulted_epoch=COALESCE(defaulted_epoch,?) WHERE loan_id=?", (now, row["loan_id"]))
            result = CreditRiskManager.charge_off_loan(conn, loan_id=row["loan_id"], event_key=f"{reference}:{row['loan_id']}",
                                                       actor_id=actor, now=now, memo="회생·파산 면책")
            gross = int(result.get("grossMinorUnits") or 0)
            conn.execute("UPDATE loans SET outstanding_balance=0,charged_off_balance_minor_units="
                         "MAX(0,charged_off_balance_minor_units-?) WHERE loan_id=?", (gross, row["loan_id"]))
            discharged.append(row["loan_id"])
        return discharged

    def _restrict(self, conn, user_id, ban_days):
        now = int(time.time())
        until = now + int(ban_days * DAY)
        conn.execute("INSERT INTO insolvency_restrictions(user_id,loan_ban_until,last_discharge_epoch) VALUES (?,?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET loan_ban_until=MAX(loan_ban_until,excluded.loan_ban_until),"
                     "last_discharge_epoch=excluded.last_discharge_epoch", (str(user_id), until, now))
        return until

    def _score(self, conn, user_id, actor, target, reason):
        from managers.credit_score_manager import CreditScoreManager
        current = conn.execute("SELECT credit_score FROM users WHERE user_id=?", (str(user_id),)).fetchone()
        if current and (current[0] or 0) > target:
            CreditScoreManager.set_score(conn, user_id, int(target), actor, reason)

    # ------------------------------------------------------------ approve
    def approve(self, actor, filing):
        """filing: court_filings 행(dict, 선택적으로 weeks). 인가 처리 결과 요약 문자열."""
        user_id = str(filing["user_id"])
        with self.db.atomic() as conn:
            ensure_tables(conn)
            if conn.execute("SELECT 1 FROM insolvency_cases WHERE filing_id=?", (filing["filing_id"],)).fetchone():
                raise ValueError("이미 처리된 신청입니다.")
            if conn.execute("SELECT 1 FROM insolvency_cases WHERE user_id=? AND status='active'", (user_id,)).fetchone():
                raise ValueError("이미 진행 중인 회생 사건이 있습니다.")
            nationality = conn.execute("SELECT nationality,role FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not nationality:
                raise ValueError("신청자를 찾을 수 없습니다.")
            if nationality["role"] == "nation" or user_id == str(CurrencyManager.user_treasury_id(conn, user_id)):
                raise ValueError("국가·국고 계정은 회생·파산을 인가할 수 없습니다.")
            pol = policy(conn, nationality[0])
            last = conn.execute("SELECT last_discharge_epoch FROM insolvency_restrictions WHERE user_id=?", (user_id,)).fetchone()
            if last and last[0] and last[0] > time.time() - pol["rediscard_days"] * DAY:
                raise ValueError(f"최근 {int(pol['rediscard_days'])}일 안에 면책받은 기록이 있어 인가할 수 없습니다.")
        if filing["kind"] == "bankruptcy":
            return self._approve_bankruptcy(actor, filing, user_id, pol)
        return self._approve_rehabilitation(actor, filing, user_id, pol)

    def _approve_bankruptcy(self, actor, filing, user_id, pol):
        from managers.nationality_manager import NationalityManager
        from managers.interest_manager import InterestManager
        nat = NationalityManager(self.bot)
        nat.cancel_open_orders(user_id)
        InterestManager(self.db).process_user_interests(user_id)
        portfolio = self.bot.user_mgr.get_total_assets(user_id, settle=False)
        unit = {i["code"]: Decimal(str(i["value"])) / Decimal(str(i["qty"]))
                for i in portfolio["stocks"] if i.get("qty") and not i.get("is_collateral")}
        bonds = {i["holding_id"]: Decimal(str(i["market_value"])) for i in portfolio["bonds"]}
        summary = nat._liquidate(actor, user_id, unit, bonds, require_cover=False)
        with self.db.readonly() as conn:
            home = CurrencyManager.home(conn, user_id)
            cash = CurrencyManager.balance(conn, user_id, home)
            _, debt = self._home_debt(conn, user_id, home)
        exempt = int(Decimal(str(pol["exempt_amount"])) * SCALE)
        paid = self._pay_pro_rata(user_id, max(0, cash - exempt))
        now = int(time.time())
        with self.db.atomic() as conn:
            discharged = self._discharge_all(conn, actor, user_id, f"court:{filing['filing_id']}")
            until = self._restrict(conn, user_id, pol["bankruptcy_ban_days"])
            self._score(conn, user_id, actor, pol["bankruptcy_score"], f"파산 면책 제{filing['filing_id']}호")
            conn.execute("INSERT INTO insolvency_cases(filing_id,user_id,kind,status,currency,actor_id,debt_units,paid_units,"
                         "detail,created_epoch,closed_epoch) VALUES (?,?,'bankruptcy','discharged',?,?,?,?,?,?,?)",
                         (filing["filing_id"], user_id, home, str(actor), debt, paid,
                          json.dumps({"liquidated": summary, "discharged": discharged, "exempt": exempt, "banUntil": until}),
                          now, now))
        return (f"자산 {(summary['sold'] + summary['accounts']) / SCALE:,.0f} {home} 처분, 채권자 배당 {paid / SCALE:,.0f} {home}, "
                f"대출 {len(discharged)}건 면책, 신용점수 {int(pol['bankruptcy_score'])}점 이하, "
                f"{int(pol['bankruptcy_ban_days'])}일간 대출 금지")

    def _approve_rehabilitation(self, actor, filing, user_id, pol):
        from managers.interest_manager import InterestManager
        InterestManager(self.db).process_user_interests(user_id)
        portfolio = self.bot.user_mgr.get_total_assets(user_id, settle=False)
        weeks = int(filing.get("weeks") or pol["rehab_weeks"])
        weeks = max(int(pol["rehab_min_weeks"]), min(int(pol["rehab_max_weeks"]), weeks))
        with self.db.atomic() as conn:
            home = CurrencyManager.home(conn, user_id)
            _, debt = self._home_debt(conn, user_id, home)
            if debt <= 0:
                raise ValueError("갚을 본국 통화 대출이 없어 회생을 인가할 수 없습니다.")
            # 청산가치 보장: 파산했다면 채권자가 받았을 금액(총자산 - 면제재산, 대출 한도) 이상 갚게 합니다.
            gross = CurrencyManager.convert(conn, _units(portfolio.get("grand_total")), base_currency_code(conn), home)
            liquidation = max(0, min(debt, gross - int(Decimal(str(pol["exempt_amount"])) * SCALE)))
            plan = min(debt, max(liquidation, int(Decimal(debt) * Decimal(str(pol["rehab_repay_percent"])) / 100)))
            weekly = -(-plan // weeks)
            now = int(time.time())
            conn.execute("INSERT INTO insolvency_cases(filing_id,user_id,kind,status,currency,actor_id,debt_units,plan_units,"
                         "weekly_units,weeks_total,next_due_epoch,detail,created_epoch) "
                         "VALUES (?,?,'rehabilitation','active',?,?,?,?,?,?,?,?,?)",
                         (filing["filing_id"], user_id, home, str(actor), debt, plan, weekly, weeks, now + WEEK,
                          json.dumps({"liquidation": liquidation}), now))
            # 동결 시작: 이 시점 이후 이자를 붙이지 않도록 커서를 맞춥니다.
            conn.execute("UPDATE loans SET last_payment_epoch=?,interest_remainder=0 WHERE user_id=? "
                         "AND status IN ('active','defaulted')", (now, user_id))
            self._score(conn, user_id, actor, pol["rehab_score"], f"회생 인가 제{filing['filing_id']}호")
        return (f"변제 총액 {plan / SCALE:,.0f} {home}(대출 {debt / SCALE:,.0f}의 {plan * 100 // debt}%), "
                f"{weeks}주간 매주 {weekly / SCALE:,.0f} {home} 자동 변제, 변제 중 이자 동결·대출 금지")

    # ------------------------------------------------------------ weekly
    def process_due(self, now=None):
        """봇 대출 워커가 호출: 기한이 된 회생 변제를 처리하고 알림 목록을 돌려줍니다."""
        now = int(now or time.time())
        with self.db.readonly() as conn:
            try:
                due = [dict(r) for r in conn.execute(
                    "SELECT * FROM insolvency_cases WHERE status='active' AND next_due_epoch<=?", (now,)).fetchall()]
            except Exception:
                return []
        notices = []
        for case in due:
            try:
                notices.append(self._installment(case, now))
            except Exception as exc:
                print(f"[Insolvency] case {case['case_id']}: {exc}")
        return notices

    def _installment(self, case, now):
        user_id = case["user_id"]
        with self.db.readonly() as conn:
            row = conn.execute("SELECT nationality FROM users WHERE user_id=?", (user_id,)).fetchone()
            pol = policy(conn, row[0] if row else "")
            cash = CurrencyManager.balance(conn, user_id, case["currency"])
        amount = min(case["weekly_units"], case["plan_units"] - case["paid_units"])
        if cash < amount:
            missed = case["missed"] + 1
            with self.db.atomic() as conn:
                if missed > pol["rehab_max_missed"]:
                    conn.execute("UPDATE insolvency_cases SET status='revoked',missed=?,closed_epoch=? WHERE case_id=?",
                                 (missed, now, case["case_id"]))
                    # 폐지: 원래 빚이 되살아나고 이 시점부터 이자가 다시 붙습니다.
                    conn.execute("UPDATE loans SET last_payment_epoch=? WHERE user_id=? AND status IN ('active','defaulted')",
                                 (now, user_id))
                    return {"user_id": user_id, "event": "revoked", "case": case["case_id"]}
                conn.execute("UPDATE insolvency_cases SET missed=?,next_due_epoch=? WHERE case_id=?",
                             (missed, case["next_due_epoch"] + WEEK, case["case_id"]))
            return {"user_id": user_id, "event": "missed", "case": case["case_id"], "missed": missed,
                    "left": int(pol["rehab_max_missed"]) - missed + 1}
        paid = self._pay_pro_rata(user_id, amount)
        total = case["paid_units"] + paid
        weeks = case["weeks_paid"] + 1
        with self.db.atomic() as conn:
            if weeks >= case["weeks_total"] or total >= case["plan_units"]:
                discharged = self._discharge_all(conn, case["actor_id"], user_id, f"rehab:{case['case_id']}")
                self._restrict(conn, user_id, pol["rehab_ban_days_after"])
                conn.execute("UPDATE insolvency_cases SET status='completed',paid_units=?,weeks_paid=?,closed_epoch=? "
                             "WHERE case_id=?", (total, weeks, now, case["case_id"]))
                return {"user_id": user_id, "event": "completed", "case": case["case_id"], "discharged": len(discharged)}
            conn.execute("UPDATE insolvency_cases SET paid_units=?,weeks_paid=?,next_due_epoch=? WHERE case_id=?",
                         (total, weeks, case["next_due_epoch"] + WEEK, case["case_id"]))
        return {"user_id": user_id, "event": "paid", "case": case["case_id"], "week": weeks}
