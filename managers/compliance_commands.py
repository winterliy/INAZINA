"""Admin commands for KYC review and AML alerts.

The dashboard no longer has a KYC/AML tab: nobody is reviewed or alerted in day-to-day play, so the
controls live here and are used only when a specific account needs to be restricted or released.
The checks that block or flag transactions stay in ComplianceManager, untouched.
"""
import time

from managers.compliance_manager import ComplianceManager

KYC_USAGE = ('[목록] · [설정 사용자 상태 위험등급 [사유]] · [제한 사용자] · [해제 사용자] · [만료처리]'
             ' · 상태: 대기/확인/거절/만료 · 위험등급: 일반/강화/제한')
AML_USAGE = ('[목록] · [검토 번호] · [종결 번호 [결과]] · [규칙 국적] · [규칙 국적 끄기] · [규칙 국적 설정 단건 24시간누적 24시간횟수]'
             ' · 결과: 추가조치없음/오탐/추가검토/상급검토/제한검토')

STATUS = {"대기": "pending", "확인": "verified", "확인완료": "verified", "거절": "rejected", "만료": "expired"}
TIER = {"일반": "standard", "강화": "enhanced", "제한": "restricted"}
REASON = {"검토시작": "review_started", "요건충족": "requirements_met", "요건미충족": "requirements_not_met",
          "정기검토": "periodic_review", "만료": "expired", "위험상향": "risk_escalation", "사용자요청": "customer_request"}
OUTCOME = {"추가조치없음": "no_action", "오탐": "false_positive", "추가검토": "further_review",
           "상급검토": "escalated", "제한검토": "account_restriction_considered"}
LIST_LIMIT = 20


def _pick(table, value, label):
    code = table.get(value, value)
    if code not in table.values():
        raise ValueError(f"{label}을(를) 확인해주세요: {', '.join(table)}")
    return code


def _day(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(epoch))) if epoch else "—"


def run_compliance_command(db, actor, command, args):
    if command == "KYC심사":
        return _kyc(db, actor, args)
    return _aml(db, actor, args)


def _kyc(db, actor, args):
    action = args[0] if args else ""
    if action == "목록" and len(args) == 1:
        with db.readonly() as conn:
            rows = conn.execute(
                "SELECT user_id,status,risk_tier,expires_epoch FROM kyc_profiles ORDER BY updated_epoch DESC LIMIT ?",
                (LIST_LIMIT,)).fetchall()
        return "\n".join(f"{r['user_id']} · {r['status']} · {r['risk_tier']} · 만료 {_day(r['expires_epoch'])}"
                         for r in rows) or "KYC 프로필이 없습니다. 프로필이 없는 사용자는 제한 없이 거래합니다."
    if action == "설정" and len(args) in {4, 5}:
        user, status, tier = args[1], _pick(STATUS, args[2], "상태"), _pick(TIER, args[3], "위험등급")
        reason = _pick(REASON, args[4], "사유") if len(args) == 5 else "review_started"
    elif action == "제한" and len(args) == 2:
        user, status, tier, reason = args[1], "verified", "restricted", "risk_escalation"
    elif action == "해제" and len(args) == 2:
        user, status, tier, reason = args[1], "verified", "standard", "requirements_met"
    elif action == "만료처리" and len(args) == 1:
        with db.atomic() as conn:
            result = ComplianceManager.expire_due_kyc_profiles(conn, actor)
        return f"✅ 만료된 KYC {result['expired_count']}건을 만료 처리했습니다."
    else:
        raise ValueError(f"사용법: $KYC심사 {KYC_USAGE}")
    with db.atomic() as conn:
        result = ComplianceManager.set_kyc_status(conn, user, actor, status, risk_tier=tier, reason_code=reason)
    note = " · 송금·예금 개설·대출 신청이 막힙니다." if result["risk_tier"] == "restricted" else ""
    return f"✅ {user} KYC {result['status']} · 위험등급 {result['risk_tier']}{note}"


def _aml(db, actor, args):
    action = args[0] if args else ""
    if action == "목록" and len(args) == 1:
        with db.readonly() as conn:
            rows = conn.execute(
                "SELECT alert_id,user_id,nationality,rule_code,amount,currency_code,status,opened_epoch FROM aml_alerts "
                "WHERE status<>'closed' ORDER BY alert_id DESC LIMIT ?", (LIST_LIMIT,)).fetchall()
        return "\n".join(f"#{r['alert_id']} {r['user_id']} · {r['nationality']} · {r['rule_code']} · "
                         f"{r['amount']} {r['currency_code']} · {r['status']} · {_day(r['opened_epoch'])}"
                         for r in rows) or "미종결 AML 경보가 없습니다."
    if action in {"검토", "종결"} and len(args) in ({2} if action == "검토" else {2, 3}):
        status = "reviewing" if action == "검토" else "closed"
        outcome = (_pick(OUTCOME, args[2], "결과") if len(args) == 3 else "no_action") if action == "종결" else "further_review"
        try:
            alert_id = int(args[1].lstrip("#"))
        except ValueError:
            raise ValueError("경보 번호를 확인해주세요.") from None
        with db.atomic() as conn:
            ComplianceManager.review_aml_alert(conn, alert_id, actor, status, outcome)
        return f"✅ AML 경보 #{alert_id} · {'검토 중' if status == 'reviewing' else '종결'} ({outcome})"
    if action == "규칙" and 2 <= len(args) <= 6:
        return _aml_rule(db, actor, args[1:])
    raise ValueError(f"사용법: $AML경보 {AML_USAGE}")


def _aml_rule(db, actor, args):
    nationality, rest = args[0], args[1:]
    with db.readonly() as conn:
        rule = conn.execute(
            "SELECT currency_code,large_transaction_limit,rolling_24h_amount_limit,rolling_24h_count_limit,enabled "
            "FROM aml_monitoring_rules WHERE nationality=?", (nationality,)).fetchone()
    if not rest and not rule:
        return f"{nationality} · 규칙 없음 (비활성)"
    if not rest:
        return (f"{nationality} · {rule['currency_code']} · {'활성' if rule['enabled'] else '비활성'} · 단건 {rule['large_transaction_limit']}"
                f" · 24시간 누적 {rule['rolling_24h_amount_limit']} · 24시간 {rule['rolling_24h_count_limit']}회")
    if rest == ["끄기"]:
        if not rule:
            return f"{nationality}에는 켜진 AML 규칙이 없습니다."
        large, rolling, count, enabled = (rule["large_transaction_limit"], rule["rolling_24h_amount_limit"],
                                          rule["rolling_24h_count_limit"], False)
    elif rest[0] == "설정" and len(rest) == 4:
        large, rolling, count, enabled = rest[1], rest[2], rest[3], True
    else:
        raise ValueError(f"사용법: $AML경보 {AML_USAGE}")
    with db.atomic() as conn:
        ComplianceManager.configure_aml_rule(
            conn, nationality, actor, large_transaction_limit=large, rolling_24h_amount_limit=rolling,
            rolling_24h_count_limit=count, enabled=enabled)
    return f"✅ {nationality} AML 감시를 {'켰습니다' if enabled else '껐습니다'}. 거래는 막지 않고 경보만 만듭니다."
