import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Card, Field, date, plain, type Ctx } from "./common";

type Kyc = { user_id: string; status: string; risk_tier: string; reviewed_by: string | null; reviewed_epoch: number | null; expires_epoch: number | null; updated_epoch: number };
type Alert = { alert_id: number; user_id: string; nationality: string; rule_code: string; reference_key: string; currency_code: string; amount: number; status: string; outcome_code: string | null; opened_epoch: number; reviewed_by: string | null; reviewed_epoch: number | null };
type Rule = { nationality: string; currency_code: string; large_transaction_limit: number; rolling_24h_amount_limit: number; rolling_24h_count_limit: number; enabled: number; updated_epoch: number };
type KycEvent = { event_id: number; user_id: string; actor_id: string; event_type: string; prior_status: string; new_status: string; prior_risk_tier: string; new_risk_tier: string; reason_code: string; recorded_epoch: number };
type AmlEvent = { event_id: number; alert_id: number; actor_id: string; event_type: string; prior_status: string | null; new_status: string; outcome_code: string | null; recorded_epoch: number; nationality: string; user_id: string };
type Overview = { kycProfiles: Kyc[]; amlAlerts: Alert[]; amlRules: Rule[]; kycEvents: KycEvent[]; amlEvents: AmlEvent[] };
type Result = { success: boolean; result: Record<string, unknown> };

const KYC_STATUSES = [["pending", "검토 대기"], ["verified", "확인 완료"], ["rejected", "확인 거절"], ["expired", "만료"]];
const REASONS = [["review_started", "검토 시작"], ["requirements_met", "요건 충족"], ["requirements_not_met", "요건 미충족"], ["periodic_review", "정기 검토"], ["expired", "만료"], ["risk_escalation", "위험 상향"], ["customer_request", "사용자 요청"]];
const OUTCOMES = [["no_action", "추가 조치 없음"], ["false_positive", "오탐"], ["further_review", "추가 검토"], ["escalated", "상급 검토"], ["account_restriction_considered", "제한 검토"]];

/** Administrator-only simulator compliance controls; never collects identity documents. */
export function CompliancePage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<Overview>({ kycProfiles: [], amlAlerts: [], amlRules: [], kycEvents: [], amlEvents: [] });
  const [nationality, setNationality] = useState("");
  const [kyc, setKyc] = useState({ userId: "", status: "pending", riskTier: "standard", reasonCode: "review_started", expires: "" });
  const [rule, setRule] = useState({ large: "", rollingAmount: "", rollingCount: "", enabled: false });
  const [outcomes, setOutcomes] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    const suffix = nationality ? `?nationality=${encodeURIComponent(nationality)}` : "";
    const result = await api<Overview>(`/api/admin/compliance${suffix}`);
    setData(result);
    if (!nationality && result.amlRules.length) setNationality(result.amlRules[0].nationality);
    const selected = result.amlRules.find((item) => item.nationality === nationality) ?? result.amlRules[0];
    if (selected) setRule({ large: String(selected.large_transaction_limit || ""), rollingAmount: String(selected.rolling_24h_amount_limit || ""), rollingCount: String(selected.rolling_24h_count_limit || ""), enabled: Boolean(selected.enabled) });
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
    if (rule.enabled && ![rule.large, rule.rollingAmount, rule.rollingCount].some((value) => Number(value) > 0)) { say("감시를 켜려면 하나 이상의 양수 임계치가 필요합니다.", true); return; }
    if (!window.confirm(`${nationality}의 AML 시뮬레이터 감시 규칙을 저장할까요? 법정 신고 기준이 아닙니다.`)) return;
    void run(() => api<Result>("/api/admin/compliance", { method: "POST", body: JSON.stringify({ action: "aml_rule", nationality, largeTransactionLimit: Number(rule.large || 0), rolling24hAmountLimit: Number(rule.rollingAmount || 0), rolling24hCountLimit: Number(rule.rollingCount || 0), enabled: rule.enabled }) }),
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

  const selectedRule = data.amlRules.find((item) => item.nationality === nationality);
  return <>
    <Card title="KYC 상태 검토" caption="상태·위험등급·고정 사유 코드와 검토 이력만 저장합니다. 실명, 신분증 번호·이미지, 자유기술형 증빙은 입력하지 마세요."
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

    <Card title="미종결 AML 경보" caption="거래가 자동 차단되는 것은 아닙니다. 각 경보 검토 후 추가 조치가 필요하면 별도 절차를 적용하세요.">
      <div className="scroll-x"><table><thead><tr><th>ID</th><th>사용자</th><th>국적</th><th>규칙</th><th>거래 참조</th><th className="r">금액</th><th>상태</th><th>종결 결과</th><th>접수</th><th /></tr></thead><tbody>
        {data.amlAlerts.map((item) => <tr key={item.alert_id}><td className="mono">#{item.alert_id}</td><td className="mono">{item.user_id}</td><td>{item.nationality}</td><td>{item.rule_code}</td><td className="mono">{item.reference_key}</td><td className="r mono">{numberAmount(item.amount)} {item.currency_code}</td><td>{item.status}</td><td><select aria-label={`AML 경보 #${item.alert_id} 종결 결과`} value={outcomes[item.alert_id] || "no_action"} onChange={(event) => setOutcomes({ ...outcomes, [item.alert_id]: event.target.value })}>{OUTCOMES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></td><td>{date(item.opened_epoch)}</td><td className="row-actions"><button className="soft small" disabled={busy || item.status === "reviewing"} onClick={() => reviewAlert(item, false)}>검토 시작</button><button className="soft small" disabled={busy} onClick={() => reviewAlert(item, true)}>종결</button></td></tr>)}
      </tbody></table>{!data.amlAlerts.length && <p className="empty">미종결 경보가 없습니다.</p>}</div>
    </Card>

    <Card title="준법 검토 이력" caption="KYC·AML 변경 이벤트의 최근 기록입니다. 저장된 기록은 수정·삭제할 수 없습니다.">
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>종류</th><th>사용자 / 사례</th><th>관리자</th><th>변경</th><th>사유 / 결과</th></tr></thead><tbody>
        {[...data.kycEvents.map((item) => ({ id: `kyc-${item.event_id}`, time: item.recorded_epoch, kind: "KYC", subject: item.user_id, actor: item.actor_id, change: `${item.prior_status} → ${item.new_status} · ${item.prior_risk_tier} → ${item.new_risk_tier}`, reason: item.reason_code })),
          ...data.amlEvents.map((item) => ({ id: `aml-${item.event_id}`, time: item.recorded_epoch, kind: `AML · ${item.nationality}`, subject: `${item.user_id} / #${item.alert_id}`, actor: item.actor_id, change: `${item.prior_status || "—"} → ${item.new_status}`, reason: item.outcome_code || item.event_type }))]
          .sort((left, right) => right.time - left.time).slice(0, 100).map((item) => <tr key={item.id}><td>{date(item.time)}</td><td>{item.kind}</td><td className="mono">{item.subject}</td><td className="mono">{item.actor}</td><td>{item.change}</td><td>{item.reason}</td></tr>)}
      </tbody></table>{!data.kycEvents.length && !data.amlEvents.length && <p className="empty">준법 검토 이력이 없습니다.</p>}</div>
    </Card>
  </>;
}

function numberAmount(value: number) { return new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 6 }).format(value); }
