import { exactMoney, validMoney, moneyCompare, minorMoney, formatMoney } from "../../shared/money";
import type { Money } from "../../shared/money";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Card, Field, date, plain, type Ctx } from "./common";

type Kyc = { user_id: string; status: string; risk_tier: string; reviewed_by: string | null; reviewed_epoch: number | null; expires_epoch: number | null; updated_epoch: number };
type Alert = { alert_id: number; user_id: string; nationality: string; rule_code: string; reference_key: string; currency_code: string; amount: Money; amount_text: string; status: string; outcome_code: string | null; opened_epoch: number; reviewed_by: string | null; reviewed_epoch: number | null };
type Rule = { nationality: string; currency_code: string; large_transaction_limit: number; rolling_24h_amount_limit: number; rolling_24h_count_limit: number; enabled: number; updated_epoch: number };
type KycEvent = { event_id: number; user_id: string; actor_id: string; event_type: string; prior_status: string; new_status: string; prior_risk_tier: string; new_risk_tier: string; reason_code: string; recorded_epoch: number };
type AmlEvent = { event_id: number; alert_id: number; actor_id: string; event_type: string; prior_status: string | null; new_status: string; outcome_code: string | null; recorded_epoch: number; nationality: string; user_id: string };
type EclScenario = { nationality: string; scenario_key: "optimistic" | "base" | "downside"; probability: number; pd_multiplier: number; lgd_multiplier: number; updated_epoch: number };
type EclAssessment = { assessment_id: number; loan_id: number; as_of_day: string; stage: number; risk_grade: string | null; nonaccrual_since_epoch: number | null; pd_12m: number; lifetime_pd: number; lgd: number; ead: number; expected_loss: number; discount_factor: number; effective_interest_rate: number; expected_loss_time_years: number; macro_pd_multiplier: number; ead_text: string; expected_loss_text: string; decimal_places: number; quote_currency: string; scenario: string; calculated_epoch: number; nationality: string };
type EclBacktest = { issuer: string; nationality: string; pdBand: string; sampleCount: number; borrowerCount: number; effectiveSampleCount: number; observedDefaults: number; meanPredictedPd: number; observedDefaultRate: number; calibrationGap: number; observedToExpected: number | null; observedRateLower95: number; observedRateUpper95: number; predictedPdWithinObserved95: boolean; brierScore: number };
type EclDiscrimination = { issuer: string; nationality: string; sampleCount: number; defaults: number; auroc: number | null; method: string };
type EclStability = { issuer: string; nationality: string; olderCount: number; recentCount: number; populationStabilityIndex: number | null; cohortSplitEpoch: number; method: string };
type EclBorrowerHoldout = { issuer: string; nationality: string; borrowerCount: number; defaults: number; meanPredictedPd: number; observedDefaultRate: number; observedRateLower95: number; observedRateUpper95: number; predictedPdWithinObserved95: boolean; brierScore: number; auroc: number | null; method: string };
type EclOutOfTimeHoldout = EclBorrowerHoldout & { cohortStartEpoch: number; cohortEndEpoch: number };
type GradeTransition = { issuer: string; nationality: string; from_grade: string; to_grade: string; loan_count: number };
type CreditSupport = { support_id: number; loan_id: number; party_user_id: string; support_type: string; exposure_share_bps: number; underwriting_dti: number | null; underwriting_dsr: number | null; guarantee_limit_minor_units: number; quote_currency: string; guarantee_recovery_bps: number; recovery_days: number; recovery_cost_bps: number; seniority_rank: number; active: number; updated_epoch: number };
type CreditPolicy = { nationality: string; product_code: string; pd_multiplier: number; grade_a_max: number; grade_b_max: number; grade_c_max: number; grade_d_max: number; grade_e_max: number; grade_f_max: number; updated_epoch: number; updated_by: string };
type FutureCommitment = { loan_id: number; committed_limit_minor_units: number; committed_limit_text: string; quote_currency: string; credit_conversion_factor_bps: number; expires_epoch: number | null; active: number; updated_epoch: number; updated_by: string };
type CollateralRecovery = { collateral_id: number; loan_id: number; asset_code: string; seniority_rank: number; recovery_days: number; recovery_cost_bps: number; last_value: number; quote_currency: string; status: string; nationality: string };
type ChargeoffEvent = { event_id: number; loan_id: number; event_key: string; event_type: string; amount_minor_units: number; decimal_places: number; currency_code: string; actor_id: string; created_epoch: number; memo: string; nationality: string };
type Overview = { kycProfiles: Kyc[]; amlAlerts: Alert[]; amlRules: Rule[]; kycEvents: KycEvent[]; amlEvents: AmlEvent[]; creditRiskScenarios: EclScenario[]; creditSupport: CreditSupport[]; creditPolicyOverrides: CreditPolicy[]; futureCommitments: FutureCommitment[]; collateralRecovery: CollateralRecovery[]; chargeoffEvents: ChargeoffEvent[]; eclAssessments: EclAssessment[]; eclBacktest: { maturedThroughEpoch: number; groups: EclBacktest[]; discrimination: EclDiscrimination[]; stability: EclStability[]; borrowerHoldout: EclBorrowerHoldout[]; outOfTimeBorrowerHoldout: EclOutOfTimeHoldout[]; gradeTransitions: GradeTransition[]; gradeTransitionMethod: string; method: string } };
type Result = { success: boolean; result: Record<string, unknown> };

const KYC_STATUSES = [["pending", "검토 대기"], ["verified", "확인 완료"], ["rejected", "확인 거절"], ["expired", "만료"]];
const REASONS = [["review_started", "검토 시작"], ["requirements_met", "요건 충족"], ["requirements_not_met", "요건 미충족"], ["periodic_review", "정기 검토"], ["expired", "만료"], ["risk_escalation", "위험 상향"], ["customer_request", "사용자 요청"]];
const OUTCOMES = [["no_action", "추가 조치 없음"], ["false_positive", "오탐"], ["further_review", "추가 검토"], ["escalated", "상급 검토"], ["account_restriction_considered", "제한 검토"]];
const KYC_EVENT_LABELS: Record<string, string> = { review: "심사", expiry: "만료", transfer_denied: "송금 차단", deposit_open_denied: "예금 계좌 개설 차단", loan_apply_denied: "대출 신청 차단" };
const KYC_EVENT_REASON_LABELS: Record<string, string> = { risk_restricted: "제한 위험등급", kyc_incomplete_or_expired: "확인 미완료 또는 만료", eligibility_changed_during_check: "확인 중 자격 상태 변경" };
const AML_EVENT_LABELS: Record<string, string> = { alert_opened: "경보 생성", analyst_review: "분석 검토" };
const AML_STATUS_LABELS: Record<string, string> = { open: "미검토", reviewing: "검토 중", closed: "종결" };
const AML_OUTCOME_LABELS: Record<string, string> = { no_action: "추가 조치 없음", false_positive: "오탐", further_review: "추가 검토", escalated: "상급 검토", account_restriction_considered: "제한 검토" };

/** Administrator-only simulator compliance controls; never collects identity documents. */
export function CompliancePage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<Overview>({ kycProfiles: [], amlAlerts: [], amlRules: [], kycEvents: [], amlEvents: [], creditRiskScenarios: [], creditSupport: [], creditPolicyOverrides: [], futureCommitments: [], collateralRecovery: [], chargeoffEvents: [], eclAssessments: [], eclBacktest: { maturedThroughEpoch: 0, groups: [], discrimination: [], stability: [], borrowerHoldout: [], outOfTimeBorrowerHoldout: [], gradeTransitions: [], gradeTransitionMethod: "", method: "" } });
  const [nationality, setNationality] = useState("");
  const [kyc, setKyc] = useState({ userId: "", status: "pending", riskTier: "standard", reasonCode: "review_started", expires: "" });
  const [rule, setRule] = useState({ large: "", rollingAmount: "", rollingCount: "", enabled: false });
  const [outcomes, setOutcomes] = useState<Record<number, string>>({});
  const [creditSupport, setCreditSupport] = useState({ loanId: "", partyUserId: "", supportType: "co_borrower", exposureShareBps: "10000", underwritingDti: "", underwritingDsr: "", guaranteeLimit: "", guaranteeRecoveryBps: "10000", recoveryDays: "180", recoveryCostBps: "0", seniorityRank: "1" });
  const [policyDraft, setPolicyDraft] = useState({ productCode: "*", pdMultiplier: "1", a: "0.005", b: "0.01", c: "0.025", d: "0.05", e: "0.1", f: "0.2" });
  const [commitmentDraft, setCommitmentDraft] = useState({ loanId: "", limit: "", conversionBps: "5000", expires: "" });
  const [collateralDraft, setCollateralDraft] = useState({ collateralId: "", seniorityRank: "1", recoveryDays: "180", recoveryCostBps: "1000" });
  const [chargeoffDraft, setChargeoffDraft] = useState({ loanId: "", action: "charge_off", amount: "", eventKey: "", memo: "" });
  const [scenarioDraft, setScenarioDraft] = useState<Record<string, { probability: string; pdMultiplier: string; lgdMultiplier: string }>>({});
  const [scenarioDraftCountry, setScenarioDraftCountry] = useState("");
  const scenarioLoadVersion = useRef(0);

  const load = useCallback(async () => {
    const version = ++scenarioLoadVersion.current;
    const suffix = nationality ? `?nationality=${encodeURIComponent(nationality)}` : "";
    const result = await api<Overview>(`/api/admin/compliance${suffix}`);
    if (version !== scenarioLoadVersion.current) return;
    setData(result);
    if (!nationality && result.amlRules.length) setNationality(result.amlRules[0].nationality);
    const selected = result.amlRules.find((item) => item.nationality === nationality) ?? result.amlRules[0];
    if (selected) setRule({ large: String(selected.large_transaction_limit || ""), rollingAmount: String(selected.rolling_24h_amount_limit || ""), rollingCount: String(selected.rolling_24h_count_limit || ""), enabled: Boolean(selected.enabled) });
    const selectedCountry = nationality || selected?.nationality || "";
    const scenarios = result.creditRiskScenarios.filter((item) => item.nationality === selectedCountry);
    if (scenarios.length) {
      setScenarioDraft(Object.fromEntries(scenarios.map((item) => [item.scenario_key, {
        probability: String(item.probability), pdMultiplier: String(item.pd_multiplier), lgdMultiplier: String(item.lgd_multiplier),
      }])));
      setScenarioDraftCountry(selectedCountry);
    } else {
      setScenarioDraft({});
      setScenarioDraftCountry("");
    }
  }, [nationality]);

  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "준법 정보를 불러오지 못했습니다.", true)); }, [load, say]);

  function saveKyc(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const expiresEpoch = kyc.expires ? Math.floor(new Date(kyc.expires).getTime() / 1000) : undefined;
    if (!window.confirm(`${kyc.userId} 사용자의 KYC 상태를 '${kyc.status}'로 기록할까요? 신원 문서는 저장하지 않습니다.`)) return;
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ action: "kyc_review", userId: kyc.userId.trim(), status: kyc.status, riskTier: kyc.riskTier, reasonCode: kyc.reasonCode, ...(expiresEpoch ? { expiresEpoch } : {}) }) }),
      "KYC 검토를 저장하지 못했습니다.", async (result) => { say(plain(JSON.stringify(result.result)), !result.success); setKyc({ ...kyc, userId: "", expires: "" }); await load(); });
  }

  function saveRule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!nationality) { say("AML 규칙을 설정할 국적을 선택해주세요.", true); return; }
    if (rule.enabled && !([rule.large, rule.rollingAmount].some((value) => validMoney(value, true)) || Number(rule.rollingCount) > 0)) { say("감시를 켜려면 하나 이상의 양수 임계치가 필요합니다.", true); return; }
    if (!window.confirm(`${nationality}의 AML 시뮬레이터 감시 규칙을 저장할까요? 법정 신고 기준이 아닙니다.`)) return;
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ action: "aml_rule", nationality, largeTransactionLimit: exactMoney(rule.large || 0), rolling24hAmountLimit: exactMoney(rule.rollingAmount || 0), rolling24hCountLimit: Number(rule.rollingCount || 0), enabled: rule.enabled }) }),
      "AML 규칙을 저장하지 못했습니다.", async (result) => { say(plain(JSON.stringify(result.result)), !result.success); await load(); });
  }

  function reviewAlert(item: Alert, close: boolean) {
    const status = close ? "closed" : "reviewing";
    const outcomeCode = close ? (outcomes[item.alert_id] || "no_action") : "further_review";
    const text = close ? "종결" : "검토 중";
    if (!window.confirm(`AML 경보 #${item.alert_id}을 '${text}' 상태로 바꿀까요?`)) return;
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ action: "aml_review", alertId: item.alert_id, status, outcomeCode }) }),
      "AML 사례를 갱신하지 못했습니다.", async (result) => { say(plain(JSON.stringify(result.result)), !result.success); await load(); });
  }

  function expireDueKyc() {
    if (!window.confirm("만료일이 지난 확인 완료 KYC를 만료 처리하고 감사 이력에 기록할까요? 최대 500건씩 처리합니다.")) return;
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ action: "kyc_expire_due" }) }),
      "만료된 KYC를 처리하지 못했습니다.", async (result) => {
        const count = Number(result.result.expired_count ?? 0);
        say(`KYC 만료 처리 ${count}건 · 감사 이력에 기록했습니다.`, !result.success);
        await load();
      });
  }

  function saveScenarios(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (scenarioDraftCountry !== nationality) { say("선택한 국가의 ECL 가정을 불러오는 중입니다.", true); return; }
    const scenarios = (["optimistic", "base", "downside"] as const).map((key) => ({ key,
      probability: Number(scenarioDraft[key]?.probability), pdMultiplier: Number(scenarioDraft[key]?.pdMultiplier), lgdMultiplier: Number(scenarioDraft[key]?.lgdMultiplier),
    }));
    if (scenarios.some((item) => !Number.isFinite(item.probability) || item.probability < 0 || item.probability > 1 || !Number.isFinite(item.pdMultiplier) || item.pdMultiplier < 0 || item.pdMultiplier > 20 || !Number.isFinite(item.lgdMultiplier) || item.lgdMultiplier < 0 || item.lgdMultiplier > 5) || Math.abs(scenarios.reduce((sum, item) => sum + item.probability, 0) - 1) > 1e-6) {
      say("확률은 각각 0~1 범위이고 세 시나리오 합은 1이어야 합니다. PD 배수 0~20, LGD 배수 0~5 범위도 확인해주세요.", true); return;
    }
    if (!window.confirm(`${nationality}의 ECL 낙관·기준·하방 가정을 저장할까요? 이후 평가에 적용됩니다.`)) return;
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ action: "credit_risk_scenarios", nationality,
      scenarios: scenarios.map(({ key, probability, pdMultiplier, lgdMultiplier }) => ({ key, probability, pdMultiplier, lgdMultiplier })) }) }),
      "ECL 시나리오를 저장하지 못했습니다.", async (result) => { say(plain(JSON.stringify(result.result)), !result.success); await load(); });
  }

  function saveCreditSupport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const isGuarantor = creditSupport.supportType === "guarantor";
    const payload = { action: "loan_credit_support", loanId: Number(creditSupport.loanId), partyUserId: creditSupport.partyUserId.trim(), supportType: creditSupport.supportType,
      exposureShareBps: Number(creditSupport.supportType === "co_borrower" ? creditSupport.exposureShareBps : 0), guaranteeLimit: isGuarantor ? creditSupport.guaranteeLimit : "0",
      guaranteeRecoveryBps: Number(creditSupport.guaranteeRecoveryBps), recoveryDays: Number(creditSupport.recoveryDays),
      recoveryCostBps: Number(creditSupport.recoveryCostBps), seniorityRank: Number(creditSupport.seniorityRank), active: true };
    if (!Number.isSafeInteger(payload.loanId) || payload.loanId <= 0 || !payload.partyUserId ||
      ![payload.exposureShareBps, payload.guaranteeRecoveryBps, payload.recoveryDays, payload.recoveryCostBps, payload.seniorityRank].every(Number.isSafeInteger) ||
      (isGuarantor && (!validMoney(payload.guaranteeLimit, true)))) {
      say("대출·당사자와 신용지원 조건을 확인해주세요.", true); return;
    }
    if (!window.confirm(`대출 #${payload.loanId}의 ${payload.supportType} 신용지원을 등록할까요? 평가에 반영되며 변경 이력을 남깁니다.`)) return;
    const dti = creditSupport.underwritingDti === "" ? undefined : Number(creditSupport.underwritingDti);
    const dsr = creditSupport.underwritingDsr === "" ? undefined : Number(creditSupport.underwritingDsr);
    if ([dti, dsr].some((value) => value !== undefined && (!Number.isFinite(value) || value < 0 || value > 200))) {
      say("공동차주 DTI·DSR은 0~200% 범위여야 합니다.", true); return;
    }
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ ...payload, ...(creditSupport.supportType === "co_borrower" ? { underwritingDti: dti, underwritingDsr: dsr } : {}) }) }),
      "신용지원을 저장하지 못했습니다.", async (result) => { say(plain(JSON.stringify(result.result)), !result.success); await load(); });
  }

  function saveCreditPolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const gradeKeys = ["a", "b", "c", "d", "e", "f"] as const;
    const gradeThresholds = Object.fromEntries(gradeKeys.map((key) => [key, Number(policyDraft[key])])) as Record<typeof gradeKeys[number], number>;
    const limits = Object.values(gradeThresholds);
    const multiplier = Number(policyDraft.pdMultiplier);
    if (!nationality || !Number.isFinite(multiplier) || multiplier < 0.1 || multiplier > 10 ||
      limits.some((value) => !Number.isFinite(value) || value <= 0 || value >= 1) ||
      limits.some((value, index) => index > 0 && value <= limits[index - 1])) {
      say("관할·PD 배수와 A~F 등급기준(0~1, 오름차순)을 확인해주세요.", true); return;
    }
    if (!window.confirm(`${nationality} · ${policyDraft.productCode === "*" ? "모든 상품" : policyDraft.productCode}의 PD·등급기준을 저장할까요? 이후 평가부터 적용됩니다.`)) return;
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ action: "credit_risk_policy", nationality, productCode: policyDraft.productCode,
      pdMultiplier: multiplier, gradeThresholds }) }), "신용위험 정책을 저장하지 못했습니다.", async (result) => { say(plain(JSON.stringify(result.result)), !result.success); await load(); });
  }

  function saveFutureCommitment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const expiresEpoch = commitmentDraft.expires ? Math.floor(new Date(commitmentDraft.expires).getTime()/1000) : undefined;
    const loanId = Number(commitmentDraft.loanId), limit = commitmentDraft.limit, ccf = Number(commitmentDraft.conversionBps);
    if (!Number.isSafeInteger(loanId) || loanId <= 0 || !validMoney(limit) || moneyCompare(limit, 0) < 0 ||
      !Number.isSafeInteger(ccf) || ccf < 0 || ccf > 10000 || (expiresEpoch !== undefined && !Number.isFinite(expiresEpoch))) {
      say("약정 대출 ID·한도·신용환산율을 확인해주세요.", true); return;
    }
    if (!window.confirm(`대출 #${loanId}의 미사용 약정한도 ${limit} 및 CCF ${ccf}bp를 EAD 가정으로 저장할까요?`)) return;
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ action: "loan_future_commitment", loanId, committedLimit: limit,
      creditConversionFactorBps: ccf, active: true, ...(expiresEpoch ? { expiresEpoch } : {}) }) }), "미사용 약정을 저장하지 못했습니다.",
    async (result) => { say(plain(JSON.stringify(result.result)), !result.success); await load(); });
  }

  function saveCollateralRecovery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = [Number(collateralDraft.collateralId), Number(collateralDraft.seniorityRank), Number(collateralDraft.recoveryDays), Number(collateralDraft.recoveryCostBps)];
    if (!values.every(Number.isSafeInteger) || values[0] <= 0 || values[1] < 1 || values[1] > 100 || values[2] < 0 || values[2] > 3650 || values[3] < 0 || values[3] > 10000) {
      say("담보 식별자와 회수 순위·기간·비용을 확인해주세요.", true); return;
    }
    if (!window.confirm(`담보 #${values[0]}의 회수순위·기간·비용을 갱신할까요? 이후 LGD 평가부터 적용되며 변경 이력을 남깁니다.`)) return;
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ action: "loan_collateral_recovery", collateralId: values[0], seniorityRank: values[1], recoveryDays: values[2], recoveryCostBps: values[3] }) }),
      "담보 회수 가정을 저장하지 못했습니다.", async (result) => { say(plain(JSON.stringify(result.result)), !result.success); await load(); });
  }

  function recordChargeoff(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const loanId = Number(chargeoffDraft.loanId), amount = chargeoffDraft.amount;
    if (!Number.isSafeInteger(loanId) || loanId <= 0 || !chargeoffDraft.eventKey.trim() ||
      (chargeoffDraft.action === "recovery" && (!validMoney(amount, true)))) {
      say("대출 ID, 고유 참조 키와 회수액을 확인해주세요.", true); return;
    }
    const chargeoff = chargeoffDraft.action === "charge_off";
    const label = chargeoff ? "부도채권을 상각" : `회수액 ${amount}을 기록`;
    if (!window.confirm(`대출 #${loanId} ${label}할까요? 불변 원장 이벤트로 기록됩니다.`)) return;
    const body = chargeoff
      ? { action: "loan_charge_off", loanId, eventKey: chargeoffDraft.eventKey.trim(), memo: chargeoffDraft.memo }
      : { action: "loan_chargeoff_recovery", loanId, amount, eventKey: chargeoffDraft.eventKey.trim(), memo: chargeoffDraft.memo };
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify(body) }),
      "대손상각·회수를 기록하지 못했습니다.", async (result) => { say(plain(JSON.stringify(result.result)), !result.success); await load(); });
  }

  const selectedRule = data.amlRules.find((item) => item.nationality === nationality);
  return <>
    <Card title="KYC 상태 검토" caption="상태·위험등급·고정 사유 코드와 검토 이력만 저장합니다. KYC 프로필이 있는 사용자는 확인 완료·유효 상태이며 제한 등급이 아닐 때 송금, 예금 계좌 개설, 대출 신청을 할 수 있고 수취는 계속 가능합니다. 프로필이 없는 기존 사용자는 기존 정책을 따릅니다. 실명, 신분증 번호·이미지, 자유기술형 증빙은 입력하지 마세요."
      actions={<button className="soft small" type="button" disabled={busy} onClick={expireDueKyc}>만료 처리 실행</button>}>
      <form className="form-grid" onSubmit={saveKyc}>
        <Field label="사용자 ID"><input className="mono" value={kyc.userId} required maxLength={80} onChange={(event) => setKyc({ ...kyc, userId: event.target.value })} /></Field>
        <Field label="상태"><select value={kyc.status} onChange={(event) => setKyc({ ...kyc, status: event.target.value })}>{KYC_STATUSES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
        <Field label="위험 등급"><select value={kyc.riskTier} onChange={(event) => setKyc({ ...kyc, riskTier: event.target.value })}><option value="standard">일반</option><option value="enhanced">강화</option><option value="restricted">제한</option></select></Field>
        <Field label="사유 코드"><select value={kyc.reasonCode} onChange={(event) => setKyc({ ...kyc, reasonCode: event.target.value })}>{REASONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
        <Field label="만료일" hint="선택"><input type="datetime-local" value={kyc.expires} onChange={(event) => setKyc({ ...kyc, expires: event.target.value })} /></Field>
        <button className="btn primary" disabled={busy || !kyc.userId.trim()}>검토 기록</button>
      </form>
      <div className="scroll-x"><table><thead><tr><th>사용자 ID</th><th>상태</th><th>위험</th><th>검토자</th><th>만료</th><th>갱신</th></tr></thead><tbody>
        {data.kycProfiles.map((item) => <tr key={item.user_id}><td className="mono">{item.user_id}</td><td>{item.status}</td><td>{item.risk_tier}</td><td className="mono">{item.reviewed_by || "—"}</td><td>{item.expires_epoch ? date(item.expires_epoch) : "—"}</td><td>{date(item.updated_epoch)}</td></tr>)}
      </tbody></table>{!data.kycProfiles.length && <p className="empty">KYC 검토 기록이 없습니다.</p>}</div>
    </Card>

    <Card title="국적별 AML 시뮬레이터 규칙" caption="고액·24시간 누적액·횟수 기준으로 경보만 생성합니다. 기본 비활성 상태이며 법정 기준·신고 판정이 아닙니다.">
      <form className="form-grid" onSubmit={saveRule}>
        <Field label="국적"><select value={nationality} onChange={(event) => setNationality(event.target.value)}>{data.amlRules.map((item) => <option key={item.nationality} value={item.nationality}>{item.nationality} · {item.currency_code}</option>)}</select></Field>
        <Field label={`단건 기준 (${selectedRule?.currency_code || "통화"})`}><input type="number" min="0" step="any" value={rule.large} onChange={(event) => setRule({ ...rule, large: event.target.value })} /></Field>
        <Field label={`24시간 누적액 (${selectedRule?.currency_code || "통화"})`}><input type="number" min="0" step="any" value={rule.rollingAmount} onChange={(event) => setRule({ ...rule, rollingAmount: event.target.value })} /></Field>
        <Field label="24시간 거래 횟수"><input type="number" min="0" step="1" value={rule.rollingCount} onChange={(event) => setRule({ ...rule, rollingCount: event.target.value })} /></Field>
        <Field label="감시"><select value={rule.enabled ? "on" : "off"} onChange={(event) => setRule({ ...rule, enabled: event.target.value === "on" })}><option value="off">비활성</option><option value="on">활성</option></select></Field>
        <button className="btn primary" disabled={busy || !nationality}>규칙 저장</button>
      </form>
      <p className="hint">현재 규칙 {selectedRule ? `${selectedRule.currency_code} · ${selectedRule.enabled ? "활성" : "비활성"} · 갱신 ${date(selectedRule.updated_epoch)}` : "없음"}</p>
    </Card>

    <Card title="대출별 공동차주·보증 지원" caption="내부 시뮬레이터용 익스포저 분담 또는 동일 통화 보증입니다. 변경은 불변 지원 이력과 관리자 감사 로그에 기록됩니다.">
      <form className="form-grid" onSubmit={saveCreditSupport}>
        <Field label="대출 ID"><input type="number" min="1" step="1" required value={creditSupport.loanId} onChange={(event) => setCreditSupport({ ...creditSupport, loanId: event.target.value })} /></Field>
        <Field label="지원 당사자 사용자 ID"><input className="mono" maxLength={80} required value={creditSupport.partyUserId} onChange={(event) => setCreditSupport({ ...creditSupport, partyUserId: event.target.value })} /></Field>
        <Field label="지원 유형"><select value={creditSupport.supportType} onChange={(event) => setCreditSupport({ ...creditSupport, supportType: event.target.value })}><option value="co_borrower">공동차주</option><option value="guarantor">보증인</option><option value="connected_party">연결차주(집중도 식별)</option></select></Field>
        {creditSupport.supportType === "co_borrower" && <Field label="익스포저 분담 (bp)"><input type="number" min="1" max="10000" step="1" required value={creditSupport.exposureShareBps} onChange={(event) => setCreditSupport({ ...creditSupport, exposureShareBps: event.target.value })} /></Field>}
        {creditSupport.supportType === "co_borrower" && <><Field label="공동차주 DTI (%)" hint="선택 · 비우면 주차주 지표 사용"><input type="number" min="0" max="200" step="0.01" value={creditSupport.underwritingDti} onChange={(event) => setCreditSupport({ ...creditSupport, underwritingDti: event.target.value })} /></Field><Field label="공동차주 DSR (%)" hint="선택 · 비우면 주차주 지표 사용"><input type="number" min="0" max="200" step="0.01" value={creditSupport.underwritingDsr} onChange={(event) => setCreditSupport({ ...creditSupport, underwritingDsr: event.target.value })} /></Field></>}
        {creditSupport.supportType === "guarantor" && <><Field label="보증한도 (계약통화)"><input type="number" min="0.01" step="any" required value={creditSupport.guaranteeLimit} onChange={(event) => setCreditSupport({ ...creditSupport, guaranteeLimit: event.target.value })} /></Field><Field label="보증 회수율 (bp)"><input type="number" min="0" max="10000" step="1" value={creditSupport.guaranteeRecoveryBps} onChange={(event) => setCreditSupport({ ...creditSupport, guaranteeRecoveryBps: event.target.value })} /></Field></>}
        <Field label="회수기간 (일)"><input type="number" min="0" max="3650" step="1" value={creditSupport.recoveryDays} onChange={(event) => setCreditSupport({ ...creditSupport, recoveryDays: event.target.value })} /></Field>
        <Field label="회수비용 (bp)"><input type="number" min="0" max="10000" step="1" value={creditSupport.recoveryCostBps} onChange={(event) => setCreditSupport({ ...creditSupport, recoveryCostBps: event.target.value })} /></Field>
        <Field label="회수 선순위"><input type="number" min="1" max="100" step="1" value={creditSupport.seniorityRank} onChange={(event) => setCreditSupport({ ...creditSupport, seniorityRank: event.target.value })} /></Field>
        <button className="btn primary" disabled={busy}>신용지원 기록</button>
      </form>
      <div className="scroll-x"><table><thead><tr><th>대출</th><th>당사자</th><th>유형</th><th>분담</th><th>보증한도</th><th>심사지표</th><th>회수조건</th><th>상태</th></tr></thead><tbody>
        {data.creditSupport.map((item) => <tr key={item.support_id}><td className="mono">#{item.loan_id}</td><td className="mono">{item.party_user_id}</td><td>{item.support_type}</td><td>{item.exposure_share_bps / 100}%</td><td>{item.guarantee_limit_minor_units} {item.quote_currency} 최소단위</td><td>{item.underwriting_dti == null ? "주차주 DTI 사용" : `DTI ${item.underwriting_dti}%`} · {item.underwriting_dsr == null ? "주차주 DSR 사용" : `DSR ${item.underwriting_dsr}%`}</td><td>{item.recovery_days}일 · {item.recovery_cost_bps}bp · 순위 {item.seniority_rank}</td><td>{item.active ? "활성" : "해제"}</td></tr>)}
      </tbody></table>{!data.creditSupport.length && <p className="empty">등록된 신용지원이 없습니다.</p>}</div>
    </Card>

    <Card title="관할·상품별 신용등급 기준" caption="국적 관할의 기본 정책(*) 또는 개별 대출상품 예외를 설정합니다. PD 배수와 A~F 구간은 이후 새 평가에 적용되며 과거 ECL 스냅샷은 변경하지 않습니다.">
      <form className="form-grid" onSubmit={saveCreditPolicy}>
        <Field label="관할 국적"><select value={nationality} onChange={(event) => setNationality(event.target.value)}>{data.creditRiskScenarios.filter((item, index, rows) => rows.findIndex((other) => other.nationality === item.nationality) === index).map((item) => <option key={item.nationality} value={item.nationality}>{item.nationality}</option>)}</select></Field>
        <Field label="상품 코드"><input className="mono" value={policyDraft.productCode} maxLength={80} onChange={(event) => setPolicyDraft({ ...policyDraft, productCode: event.target.value || "*" })} /></Field>
        <Field label="PD 배수 (0.1~10)"><input type="number" min="0.1" max="10" step="0.01" value={policyDraft.pdMultiplier} onChange={(event) => setPolicyDraft({ ...policyDraft, pdMultiplier: event.target.value })} /></Field>
        {(["a", "b", "c", "d", "e", "f"] as const).map((key) => <Field key={key} label={`등급 ${key.toUpperCase()} 상한 PD`}><input type="number" min="0.0001" max="0.9999" step="0.0001" value={policyDraft[key]} onChange={(event) => setPolicyDraft({ ...policyDraft, [key]: event.target.value })} /></Field>)}
        <button className="btn primary" disabled={busy || !nationality}>관할 정책 저장</button>
      </form>
      <div className="scroll-x"><table><thead><tr><th>관할</th><th>상품</th><th>PD 배수</th><th>A~F 상한 PD</th><th>행위자 / 갱신</th></tr></thead><tbody>
        {data.creditPolicyOverrides.map((item) => <tr key={`${item.nationality}-${item.product_code}`}><td>{item.nationality}</td><td>{item.product_code}</td><td>{Number(item.pd_multiplier).toFixed(2)}×</td><td>{[item.grade_a_max, item.grade_b_max, item.grade_c_max, item.grade_d_max, item.grade_e_max, item.grade_f_max].map((value) => `${(value * 100).toFixed(2)}%`).join(" / ")}</td><td>{item.updated_by} · {date(item.updated_epoch)}</td></tr>)}
      </tbody></table>{!data.creditPolicyOverrides.length && <p className="empty">관할·상품별 예외 정책이 없습니다. 기본 점수카드 설정을 사용합니다.</p>}</div>
    </Card>

    <Card title="미사용 대출약정 EAD 가정" caption="활성 약정의 사용한도와 CCF로 예상손실 시점의 추가인출을 근사합니다. 확정된 drawdown 현금흐름이 아닌 내부 시뮬레이터 가정입니다.">
      <form className="form-grid" onSubmit={saveFutureCommitment}>
        <Field label="대출 ID"><input type="number" min="1" step="1" required value={commitmentDraft.loanId} onChange={(event) => setCommitmentDraft({ ...commitmentDraft, loanId: event.target.value })} /></Field>
        <Field label="총 약정한도 (대출통화)"><input type="number" min="0" step="any" required value={commitmentDraft.limit} onChange={(event) => setCommitmentDraft({ ...commitmentDraft, limit: event.target.value })} /></Field>
        <Field label="CCF (bp)"><input type="number" min="0" max="10000" step="1" required value={commitmentDraft.conversionBps} onChange={(event) => setCommitmentDraft({ ...commitmentDraft, conversionBps: event.target.value })} /></Field>
        <Field label="만료 시각" hint="선택"><input type="datetime-local" value={commitmentDraft.expires} onChange={(event) => setCommitmentDraft({ ...commitmentDraft, expires: event.target.value })} /></Field>
        <button className="btn primary" disabled={busy}>약정 EAD 가정 저장</button>
      </form>
      <div className="scroll-x"><table><thead><tr><th>대출</th><th>한도</th><th>CCF</th><th>만료</th><th>상태</th><th>행위자 / 갱신</th></tr></thead><tbody>
        {data.futureCommitments.map((item) => <tr key={item.loan_id}><td className="mono">#{item.loan_id}</td><td>{groupAmount(item.committed_limit_text)} {item.quote_currency}</td><td>{item.credit_conversion_factor_bps}bp</td><td>{item.expires_epoch ? date(item.expires_epoch) : "없음"}</td><td>{item.active ? "활성" : "해제"}</td><td>{item.updated_by} · {date(item.updated_epoch)}</td></tr>)}
      </tbody></table>{!data.futureCommitments.length && <p className="empty">등록된 미사용 약정이 없습니다.</p>}</div>
    </Card>

    <Card title="부도채권 상각·회수" caption="부도 상태 대출만 상각됩니다. 상각은 법적 채무를 면제하지 않으며 미회수 잔액을 메모잔액으로 유지합니다. 회수는 메모잔액을 초과할 수 없습니다. 이벤트 키는 재시도 멱등성에 쓰이므로 작업별 고유값을 입력하세요.">
      <form className="form-grid" onSubmit={recordChargeoff}>
        <Field label="작업"><select value={chargeoffDraft.action} onChange={(event) => setChargeoffDraft({ ...chargeoffDraft, action: event.target.value })}><option value="charge_off">상각</option><option value="recovery">회수</option></select></Field>
        <Field label="대출 ID"><input type="number" min="1" step="1" required value={chargeoffDraft.loanId} onChange={(event) => setChargeoffDraft({ ...chargeoffDraft, loanId: event.target.value })} /></Field>
        {chargeoffDraft.action === "recovery" && <Field label="회수액 (대출 통화)"><input type="number" min="0.01" step="any" required value={chargeoffDraft.amount} onChange={(event) => setChargeoffDraft({ ...chargeoffDraft, amount: event.target.value })} /></Field>}
        <Field label="고유 이벤트 키"><input className="mono" maxLength={160} required value={chargeoffDraft.eventKey} onChange={(event) => setChargeoffDraft({ ...chargeoffDraft, eventKey: event.target.value })} /></Field>
        <Field label="메모"><input maxLength={500} value={chargeoffDraft.memo} onChange={(event) => setChargeoffDraft({ ...chargeoffDraft, memo: event.target.value })} /></Field>
        <button className="btn primary" disabled={busy}>상각·회수 기록</button>
      </form>
      <div className="scroll-x"><table><thead><tr><th>대출</th><th>이벤트</th><th>키</th><th>금액</th><th>통화</th><th>처리자</th><th>시각</th></tr></thead><tbody>
        {data.chargeoffEvents.map((item) => <tr key={item.event_id}><td>#{item.loan_id}</td><td>{item.event_type === "charge_off" ? "상각" : "회수"}</td><td className="mono">{item.event_key}</td><td>{formatMoney(minorMoney(item.amount_minor_units, item.decimal_places))}</td><td>{item.currency_code}</td><td>{item.actor_id}</td><td>{date(item.created_epoch)}</td></tr>)}
      </tbody></table>{!data.chargeoffEvents.length && <p className="empty">상각·회수 기록이 없습니다.</p>}</div>
    </Card>

    <Card title="담보 회수순위·기간·비용" caption="질권 담보별 예상 회수금을 순위별로 배분하고 회수비용 및 계약 EIR로 LGD를 할인합니다. 변경은 관리자 감사와 불변 이전/이후 스냅샷에 남습니다.">
      <form className="form-grid" onSubmit={saveCollateralRecovery}>
        <Field label="담보 ID"><input type="number" min="1" step="1" required value={collateralDraft.collateralId} onChange={(event) => setCollateralDraft({ ...collateralDraft, collateralId: event.target.value })} /></Field>
        <Field label="회수 선순위"><input type="number" min="1" max="100" step="1" value={collateralDraft.seniorityRank} onChange={(event) => setCollateralDraft({ ...collateralDraft, seniorityRank: event.target.value })} /></Field>
        <Field label="회수기간 (일)"><input type="number" min="0" max="3650" step="1" value={collateralDraft.recoveryDays} onChange={(event) => setCollateralDraft({ ...collateralDraft, recoveryDays: event.target.value })} /></Field>
        <Field label="회수비용 (bp)"><input type="number" min="0" max="10000" step="1" value={collateralDraft.recoveryCostBps} onChange={(event) => setCollateralDraft({ ...collateralDraft, recoveryCostBps: event.target.value })} /></Field>
        <button className="btn primary" disabled={busy}>담보 회수조건 저장</button>
      </form>
      <div className="scroll-x"><table><thead><tr><th>담보</th><th>대출</th><th>자산</th><th>관할</th><th>상태</th><th>회수순위</th><th>회수기간</th><th>회수비용</th></tr></thead><tbody>
        {data.collateralRecovery.map((item) => <tr key={item.collateral_id}><td>#{item.collateral_id}</td><td>#{item.loan_id}</td><td>{item.asset_code}</td><td>{item.nationality}</td><td>{item.status}</td><td>{item.seniority_rank}</td><td>{item.recovery_days}일</td><td>{item.recovery_cost_bps}bp</td></tr>)}
      </tbody></table>{!data.collateralRecovery.length && <p className="empty">등록된 담보가 없습니다.</p>}</div>
    </Card>

    <Card title="국적별 기대신용손실(ECL) 시나리오" caption="낙관·기준·하방 확률가중 점수카드 가정입니다. 감독기관 승인 모델이 아니며 변경은 관리자 감사 기록에 남습니다.">
      <form className="form-grid" onSubmit={saveScenarios}>
        <Field label="국적"><select value={nationality} onChange={(event) => { setScenarioDraft({}); setScenarioDraftCountry(""); setNationality(event.target.value); }}>{data.creditRiskScenarios.filter((item, index, rows) => rows.findIndex((other) => other.nationality === item.nationality) === index).map((item) => <option key={item.nationality} value={item.nationality}>{item.nationality}</option>)}</select></Field>
        {(["optimistic", "base", "downside"] as const).map((key) => <div className="span-wide" key={key}><h3 className="sub-head">{({ optimistic: "낙관", base: "기준", downside: "하방" })[key]} 시나리오</h3><div className="form-grid">
          <Field label="확률 (0~1, 총합 1)"><input type="number" min="0" max="1" step="0.01" required value={scenarioDraft[key]?.probability ?? ""} onChange={(event) => setScenarioDraft({ ...scenarioDraft, [key]: { ...scenarioDraft[key], probability: event.target.value } })} /></Field>
          <Field label="PD 배수 (0~20)"><input type="number" min="0" max="20" step="0.01" required value={scenarioDraft[key]?.pdMultiplier ?? ""} onChange={(event) => setScenarioDraft({ ...scenarioDraft, [key]: { ...scenarioDraft[key], pdMultiplier: event.target.value } })} /></Field>
          <Field label="LGD 배수 (0~5)"><input type="number" min="0" max="5" step="0.01" required value={scenarioDraft[key]?.lgdMultiplier ?? ""} onChange={(event) => setScenarioDraft({ ...scenarioDraft, [key]: { ...scenarioDraft[key], lgdMultiplier: event.target.value } })} /></Field>
        </div></div>)}
        <button className="btn primary" disabled={busy || !nationality || scenarioDraftCountry !== nationality}>ECL 가정 저장</button>
      </form>
    </Card>

    <Card title="12개월 PD 백테스트" caption="국적 필터 적용 · 대출별 가장 최근 Stage 1/2 성숙 평가 하나만 사용합니다. 예측 PD 구간별 실제 부도율·차주 군집을 할인한 Kish 유효표본 기준 Wilson 95% 구간·Brier 점수·보정 차이를 비교합니다. 군집 보정은 근사 진단이며 독립 검증·규제 승인 모형이 아닙니다.">
      <p className="hint">관측 기준일: {date(data.eclBacktest.maturedThroughEpoch)} · 표본 칸은 대출 수, 괄호 안은 차주 수와 유효표본 수입니다.</p>
      <div className="scroll-x"><table><thead><tr><th>기관</th><th>국적</th><th>성숙 표본</th><th>부도</th><th className="r">변별력 AUROC</th><th className="r">PD 안정성 PSI</th><th>최근/과거 코호트</th></tr></thead><tbody>
        {data.eclBacktest.discrimination.map((row) => { const stability = data.eclBacktest.stability.find((item) => item.issuer === row.issuer && item.nationality === row.nationality); return <tr key={`${row.issuer}-${row.nationality}`}><td>{row.issuer}</td><td>{row.nationality}</td><td className="r">{row.sampleCount}</td><td className="r">{row.defaults}</td><td className="r mono">{row.auroc == null ? "표본 부족" : Number(row.auroc).toFixed(3)}</td><td className="r mono">{stability?.populationStabilityIndex == null ? "표본 부족" : Number(stability.populationStabilityIndex).toFixed(3)}</td><td className="r">{stability ? `${stability.recentCount} / ${stability.olderCount}` : "—"}</td></tr>; })}
      </tbody></table>{!data.eclBacktest.discrimination.length && <p className="empty">진단할 성숙 표본이 없습니다.</p>}</div>
      <p className="hint">등급 전이(첫 평가→최근 평가): {data.eclBacktest.gradeTransitionMethod}</p>
      <div className="scroll-x"><table><thead><tr><th>기관</th><th>국적</th><th>최초 등급</th><th>최근 등급</th><th className="r">대출 수</th></tr></thead><tbody>
        {data.eclBacktest.gradeTransitions.map((row) => <tr key={`${row.issuer}-${row.nationality}-${row.from_grade}-${row.to_grade}`}><td>{row.issuer}</td><td>{row.nationality}</td><td>{row.from_grade}</td><td>{row.to_grade}</td><td className="r">{row.loan_count}</td></tr>)}
      </tbody></table>{!data.eclBacktest.gradeTransitions.length && <p className="empty">등급 이력이 충분한 대출이 없습니다.</p>}</div>
      <div className="scroll-x"><table><thead><tr><th>기관</th><th>국적</th><th>예측 PD 구간</th><th className="r">표본 (대출/차주/유효)</th><th className="r">부도</th><th className="r">평균 예측 PD</th><th className="r">관측 부도율 (95% Wilson)</th><th>평균 PD 구간 판정</th><th className="r">보정 차이</th><th className="r">관측/기대</th><th className="r">Brier</th></tr></thead><tbody>
        {data.eclBacktest.groups.map((row) => <tr key={`${row.issuer}-${row.nationality}-${row.pdBand}`}><td>{row.issuer}</td><td>{row.nationality}</td><td>{row.pdBand}</td><td className="r">{row.sampleCount} / {row.borrowerCount} / {Number(row.effectiveSampleCount).toFixed(1)}</td><td className="r">{row.observedDefaults}</td><td className="r">{(row.meanPredictedPd * 100).toFixed(2)}%</td><td className="r">{(row.observedDefaultRate * 100).toFixed(2)}% ({(row.observedRateLower95 * 100).toFixed(1)}–{(row.observedRateUpper95 * 100).toFixed(1)}%)</td><td>{row.predictedPdWithinObserved95 ? "구간 내" : "구간 밖"}</td><td className="r">{(row.calibrationGap * 100).toFixed(2)}%p</td><td className="r">{row.observedToExpected == null ? "—" : `${Number(row.observedToExpected).toFixed(2)}×`}</td><td className="r mono">{Number(row.brierScore).toFixed(4)}</td></tr>)}
      </tbody></table>{!data.eclBacktest.groups.length && <p className="empty">12개월 관측기간을 마친 평가 표본이 아직 없습니다.</p>}</div>
      <h3 className="sub-head">차주 단위 홀드아웃 진단</h3>
      <p className="hint">차주 ID 해시로 분리한 20% 고정 홀드아웃입니다. 대출이 여러 개인 차주는 기관·국적별 최근 성숙 대출 한 건만 포함하며, 관측 부도율에는 차주 단위 95% Wilson 구간을 표시합니다. 시뮬레이터 내부 표본이므로 외부 독립 검증은 아닙니다.</p>
      <div className="scroll-x"><table><thead><tr><th>기관</th><th>국적</th><th className="r">차주 수</th><th className="r">부도</th><th className="r">평균 예측 PD</th><th className="r">관측 부도율 (95% Wilson)</th><th>평균 PD 구간 판정</th><th className="r">Brier</th><th className="r">AUROC</th></tr></thead><tbody>
        {data.eclBacktest.borrowerHoldout.map((row) => <tr key={`${row.issuer}-${row.nationality}`}><td>{row.issuer}</td><td>{row.nationality}</td><td className="r">{row.borrowerCount}</td><td className="r">{row.defaults}</td><td className="r">{(row.meanPredictedPd * 100).toFixed(2)}%</td><td className="r">{(row.observedDefaultRate * 100).toFixed(2)}% ({(row.observedRateLower95 * 100).toFixed(1)}–{(row.observedRateUpper95 * 100).toFixed(1)}%)</td><td>{row.predictedPdWithinObserved95 ? "구간 내" : "구간 밖"}</td><td className="r mono">{Number(row.brierScore).toFixed(4)}</td><td className="r mono">{row.auroc == null ? "표본 부족" : Number(row.auroc).toFixed(3)}</td></tr>)}
      </tbody></table>{!data.eclBacktest.borrowerHoldout.length && <p className="empty">홀드아웃에 포함된 성숙 차주가 없습니다.</p>}</div>
      <h3 className="sub-head">최근 기간 홀드아웃 (3~1년 전 성숙 표본)</h3>
      <p className="hint">시간순으로 분리한 최근 코호트이며 차주별 최근 관측 한 건을 사용합니다. 과거 6~3년 코호트와 겹치지 않는 시점 진단입니다. 프로젝트 내부 시뮬레이터 데이터만 사용하므로 외부 독립 검증으로 해석하면 안 됩니다.</p>
      <div className="scroll-x"><table><thead><tr><th>기관</th><th>국적</th><th className="r">차주 수</th><th className="r">부도</th><th className="r">평균 예측 PD</th><th className="r">관측 부도율 (95% Wilson)</th><th>평균 PD 구간 판정</th><th className="r">Brier</th><th className="r">AUROC</th></tr></thead><tbody>
        {data.eclBacktest.outOfTimeBorrowerHoldout.map((row) => <tr key={`${row.issuer}-${row.nationality}`}><td>{row.issuer}</td><td>{row.nationality}</td><td className="r">{row.borrowerCount}</td><td className="r">{row.defaults}</td><td className="r">{(row.meanPredictedPd * 100).toFixed(2)}%</td><td className="r">{(row.observedDefaultRate * 100).toFixed(2)}% ({(row.observedRateLower95 * 100).toFixed(1)}–{(row.observedRateUpper95 * 100).toFixed(1)}%)</td><td>{row.predictedPdWithinObserved95 ? "구간 내" : "구간 밖"}</td><td className="r mono">{Number(row.brierScore).toFixed(4)}</td><td className="r mono">{row.auroc == null ? "표본 부족" : Number(row.auroc).toFixed(3)}</td></tr>)}
      </tbody></table>{!data.eclBacktest.outOfTimeBorrowerHoldout.length && <p className="empty">최근 기간의 성숙 차주 표본이 없습니다.</p>}</div>
    </Card>

    <Card title="최근 대출 ECL 평가" caption="최근 100건 · 사용 EIR·예상손실 시점·할인계수를 평가 시점 그대로 보존합니다. Stage 1·2 EAD는 midpoint까지 납부 예정인 미도래 원금을 차감한 proxy이며, 상환일정이 없거나 Stage 3이면 현재 잔액입니다. 시점·EAD 모두 현금흐름 기반 정밀모형이 아닌 근사입니다.">
      <div className="scroll-x"><table><thead><tr><th>일자</th><th>대출</th><th>국적</th><th>단계</th><th>PD 등급</th><th>비이자수익 개시</th><th className="r">12개월 PD</th><th className="r">기간 PD</th><th className="r">거시 PD 배수</th><th className="r">LGD</th><th className="r">적용 EIR</th><th className="r">손실시점(년)</th><th className="r">할인계수</th><th className="r">EAD</th><th className="r">기대손실</th><th>통화</th><th>시나리오</th></tr></thead><tbody>
        {data.eclAssessments.map((item) => <tr key={item.assessment_id}><td>{item.as_of_day}</td><td className="mono">#{item.loan_id}</td><td>{item.nationality}</td><td>{item.stage}</td><td>{item.risk_grade ?? "과거평가"}</td><td>{item.nonaccrual_since_epoch ? date(item.nonaccrual_since_epoch) : "—"}</td><td className="r">{(item.pd_12m * 100).toFixed(2)}%</td><td className="r">{(item.lifetime_pd * 100).toFixed(2)}%</td><td className="r mono">{Number(item.macro_pd_multiplier).toFixed(3)}×</td><td className="r">{(item.lgd * 100).toFixed(2)}%</td><td className="r mono">{Number(item.effective_interest_rate).toFixed(4)}%</td><td className="r mono">{Number(item.expected_loss_time_years).toFixed(3)}</td><td className="r mono">{Number(item.discount_factor).toFixed(6)}</td><td className="r mono">{groupAmount(item.ead_text)}</td><td className="r mono">{groupAmount(item.expected_loss_text)}</td><td>{item.quote_currency}</td><td>{plain(item.scenario)}</td></tr>)}
      </tbody></table>{!data.eclAssessments.length && <p className="empty">아직 ECL 평가가 없습니다.</p>}</div>
    </Card>

    <Card title="미종결 AML 경보" caption="송금인·수취인 양쪽을 감시합니다. 24시간 누적액 규칙의 금액은 경보 시점 누적합이며, 그 밖의 규칙은 해당 거래액입니다. 거래가 자동 차단되는 것은 아니며, 추가 조치는 별도 절차로 진행하세요.">
      <div className="scroll-x"><table><thead><tr><th>ID</th><th>사용자</th><th>국적</th><th>규칙</th><th>거래 참조</th><th className="r">금액</th><th>상태</th><th>종결 결과</th><th>접수</th><th /></tr></thead><tbody>
        {data.amlAlerts.map((item) => <tr key={item.alert_id}><td className="mono">#{item.alert_id}</td><td className="mono">{item.user_id}</td><td>{item.nationality}</td><td>{item.rule_code}</td><td className="mono">{item.reference_key}</td><td className="r mono">{groupAmount(item.amount_text)} {item.currency_code}</td><td>{item.status}</td><td><select aria-label={`AML 경보 #${item.alert_id} 종결 결과`} value={outcomes[item.alert_id] || "no_action"} onChange={(event) => setOutcomes({ ...outcomes, [item.alert_id]: event.target.value })}>{OUTCOMES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></td><td>{date(item.opened_epoch)}</td><td className="row-actions"><button className="soft small" disabled={busy || item.status === "reviewing"} onClick={() => reviewAlert(item, false)}>검토 시작</button><button className="soft small" disabled={busy} onClick={() => reviewAlert(item, true)}>종결</button></td></tr>)}
      </tbody></table>{!data.amlAlerts.length && <p className="empty">미종결 경보가 없습니다.</p>}</div>
    </Card>

    <Card title="준법 검토 이력" caption="KYC·AML 변경 이벤트의 최근 기록입니다. 저장된 기록은 수정·삭제할 수 없습니다.">
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>종류</th><th>사용자 / 사례</th><th>행위자</th><th>상태 / 당시 등급</th><th>사유 / 결과</th></tr></thead><tbody>
        {[...data.kycEvents.map((item) => ({ id: `kyc-${item.event_id}`, time: item.recorded_epoch, kind: `KYC · ${KYC_EVENT_LABELS[item.event_type] || item.event_type}`, subject: item.user_id, actor: item.actor_id, change: formatKycEventChange(item), reason: KYC_EVENT_REASON_LABELS[item.reason_code] || item.reason_code })),
          ...data.amlEvents.map((item) => ({ id: `aml-${item.event_id}`, time: item.recorded_epoch, kind: `AML · ${item.nationality} · ${AML_EVENT_LABELS[item.event_type] || item.event_type}`, subject: `${item.user_id} / #${item.alert_id}`, actor: item.actor_id, change: `${item.prior_status ? AML_STATUS_LABELS[item.prior_status] || item.prior_status : "—"} → ${AML_STATUS_LABELS[item.new_status] || item.new_status}`, reason: item.outcome_code ? AML_OUTCOME_LABELS[item.outcome_code] || item.outcome_code : "—" }))]
          .sort((left, right) => right.time - left.time).slice(0, 100).map((item) => <tr key={item.id}><td>{date(item.time)}</td><td>{item.kind}</td><td className="mono">{item.subject}</td><td className="mono">{item.actor}</td><td>{item.change}</td><td>{item.reason}</td></tr>)}
      </tbody></table>{!data.kycEvents.length && !data.amlEvents.length && <p className="empty">준법 검토 이력이 없습니다.</p>}</div>
    </Card>
  </>;
}

function groupAmount(value: string) {
  const [whole, fraction] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}

function formatKycEventChange(item: KycEvent) {
  const statuses: Record<string, string> = { pending: "검토 대기", verified: "확인 완료", rejected: "확인 거절", expired: "만료" };
  const tiers: Record<string, string> = { standard: "일반", enhanced: "강화", restricted: "제한" };
  const status = statuses[item.new_status] || item.new_status;
  const tier = tiers[item.new_risk_tier] || item.new_risk_tier;
  if (item.event_type.endsWith("_denied")) return `차단 당시: ${status} · ${tier} 위험등급`;
  return `${statuses[item.prior_status] || item.prior_status} → ${status} · ${tiers[item.prior_risk_tier] || item.prior_risk_tier} → ${tier}`;
}
