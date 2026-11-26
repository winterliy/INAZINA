import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "../../../shared/api";
import { won, type ServiceProps } from "./ServiceKit";
import { decisionSummary, type CaseFiling } from "./courtCases";
import "./GovernancePages.css";

type Kind = "rehabilitation" | "bankruptcy";
type Filing = CaseFiling;

const KIND: Record<Kind, { title: string; case: string; summary: string; points: string[] }> = {
  rehabilitation: { title: "개인회생 신청", case: "개회", summary: "채무를 조정해 변제계획에 따라 일부를 갚고, 나머지는 면책받는 절차입니다.",
    points: ["꾸준한 수입으로 일정 기간 갚을 수 있을 때", "자산을 유지하면서 채무를 정리", "변제계획서 작성 필요"] },
  bankruptcy: { title: "파산 신청", case: "하단", summary: "갚을 수 없는 채무를 정리하기 위해 자산을 처분하고 남은 채무의 면책을 구하는 절차입니다.",
    points: ["채무를 갚을 능력이 없을 때", "보유 자산은 처분되어 채권자에게 배당", "면책 결정 전까지 신용 제한"] },
};
const STATUS: Record<string, string> = { submitted: "접수", reviewing: "심리 중", approved: "인가", rejected: "기각" };
const ERRORS: Record<string, string> = { reason_required: "신청 이유를 입력해 주세요.", plan_required: "변제계획을 입력해 주세요.", consent_required: "확인 사항에 동의해 주세요.", filing_pending: "24시간 안에 접수한 신청이 있습니다. 처리 결과를 기다려 주세요." };
const caseNo = (filing: Filing) => `${new Date(filing.createdEpoch * 1000).getFullYear()}${KIND[filing.kind].case}${filing.filingId}`;
const STEPS = ["신청 유형 선택", "신청서 작성", "확인 및 제출"];

/** 회생 · 파산: an e-court style application — choose the case type, fill the form, review and file. */
export function CourtPage({ cash, homeCurrency, homeDigits, creditScore, accounts }: ServiceProps) {
  const [step, setStep] = useState(0);
  const [kind, setKind] = useState<Kind | null>(null);
  const [reason, setReason] = useState("");
  const [plan, setPlan] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const [filings, setFilings] = useState<Filing[] | null>(null);
  const [openCase, setOpenCase] = useState<number | null>(null);
  const load = useCallback(() => { api<{ filings: Filing[] }>("/api/court/filings").then((data) => setFilings(data.filings)).catch(() => setFilings([])); }, []);
  useEffect(load, [load]);
  const deposits = accounts.reduce((sum, account) => account.currency_code === homeCurrency ? sum + Number(account.balance) : sum, 0);
  const ready = kind && reason.trim() && (kind === "bankruptcy" || plan.trim());

  async function file() {
    if (!kind) return;
    setBusy(true); setMessage(null);
    try {
      const result = await api<{ message: string }>("/api/court/filings", { method: "POST", body: JSON.stringify({ kind, reason, plan, consent }) });
      setMessage({ text: result.message, ok: true });
      setStep(0); setKind(null); setReason(""); setPlan(""); setConsent(false); load();
    } catch (cause) {
      const text = cause instanceof Error ? cause.message : "";
      setMessage({ text: ERRORS[text] ?? (text || "신청서를 제출하지 못했습니다."), ok: false });
    } finally { setBusy(false); }
  }

  return <div className="court">
    <section className="court-frame">
      <header className="court-head"><p>나루 전자소송</p><h2>회생 · 파산 신청</h2><span>신청서는 관리자(법원)에게 전달되어 심리 후 처리됩니다.</span></header>
      <ol className="court-steps">{STEPS.map((label, index) => <li key={label} aria-current={step === index ? "step" : undefined} className={index < step ? "done" : ""}><b>{index + 1}</b>{label}</li>)}</ol>

      {step === 0 && <div className="court-body">
        <h3 className="court-title">신청할 사건의 종류를 선택하세요</h3>
        <div className="court-kinds" role="radiogroup" aria-label="사건 종류">{(Object.keys(KIND) as Kind[]).map((key) => <button key={key} type="button" role="radio" aria-checked={kind === key} onClick={() => setKind(key)}>
          <h4>{KIND[key].title}</h4><p>{KIND[key].summary}</p><ul>{KIND[key].points.map((point) => <li key={point}>{point}</li>)}</ul>
        </button>)}</div>
        <div className="court-nav"><span /><button type="button" className="court-primary" disabled={!kind} onClick={() => setStep(1)}>다음</button></div>
      </div>}

      {step === 1 && kind && <div className="court-body">
        <h3 className="court-title">{KIND[kind].title}서 작성</h3>
        <table className="court-form"><tbody>
          <tr><th>사건 종류</th><td>{KIND[kind].title.replace(" 신청", "")}</td></tr>
          <tr><th>신청인 재산 현황</th><td>지갑 {won(cash, homeDigits)} {homeCurrency} · 예금 {won(String(deposits), homeDigits)} {homeCurrency} · 신용점수 {creditScore}점<small>제출하면 전체 자산·부채 내역이 함께 첨부됩니다.</small></td></tr>
          <tr><th><label htmlFor="court-reason">신청 이유 <em>*</em></label></th><td><textarea id="court-reason" rows={5} maxLength={2000} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="채무가 생긴 경위와 현재 갚기 어려운 사정을 적어 주세요." /><small>{reason.length}/2000</small></td></tr>
          {kind === "rehabilitation" && <tr><th><label htmlFor="court-plan">변제계획 <em>*</em></label></th><td><textarea id="court-plan" rows={4} maxLength={2000} value={plan} onChange={(event) => setPlan(event.target.value)} placeholder="예: 매주 기본소득의 50%를 12주 동안 변제" /><small>{plan.length}/2000</small></td></tr>}
        </tbody></table>
        <div className="court-nav"><button type="button" className="court-secondary" onClick={() => setStep(0)}>이전</button><button type="button" className="court-primary" disabled={!ready} onClick={() => setStep(2)}>다음</button></div>
      </div>}

      {step === 2 && kind && <div className="court-body">
        <h3 className="court-title">신청 내용을 확인하세요</h3>
        <table className="court-form"><tbody>
          <tr><th>사건 종류</th><td>{KIND[kind].title.replace(" 신청", "")}</td></tr>
          <tr><th>신청 이유</th><td className="court-pre">{reason}</td></tr>
          {kind === "rehabilitation" && <tr><th>변제계획</th><td className="court-pre">{plan}</td></tr>}
        </tbody></table>
        <label className="court-consent"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /> 기재한 내용이 사실이며, 신청 후에는 취소할 수 없고 자산·부채 내역이 관리자에게 제출됨을 확인합니다.</label>
        <div className="court-nav"><button type="button" className="court-secondary" onClick={() => setStep(1)}>이전</button><button type="button" className="court-primary" disabled={!consent || busy} onClick={() => void file()}>{busy ? "제출 중…" : "신청서 제출"}</button></div>
      </div>}
      {message && <p className={message.ok ? "account-success court-message" : "account-error court-message"} role={message.ok ? "status" : "alert"}>{message.text}</p>}
    </section>

    <section className="court-frame">
      <header className="court-subhead"><h3>나의 사건 목록</h3></header>
      {filings === null ? <p className="svc-empty">불러오는 중…</p> : filings.length === 0 ? <p className="svc-empty">신청한 사건이 없습니다.</p> :
        <table className="court-cases"><thead><tr><th>사건번호</th><th>사건명</th><th>접수일</th><th>진행 상태</th></tr></thead>
          <tbody>{filings.map((filing) => {
            const opened = openCase === filing.filingId;
            const outcome = decisionSummary(filing);
            const toggle = () => setOpenCase(opened ? null : filing.filingId);
            return <Fragment key={filing.filingId}>
              <tr className={opened ? "court-case-row open" : "court-case-row"} tabIndex={0} aria-expanded={opened} onClick={toggle} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } }}>
                <td>{caseNo(filing)}</td><td>{filing.label}</td><td>{new Date(filing.createdEpoch * 1000).toLocaleDateString("ko-KR")}</td><td><span className={`court-status ${filing.status}`}>{STATUS[filing.status] ?? filing.status}</span></td>
              </tr>
              {opened && <tr className="court-case-detail"><td colSpan={4}>
                <dl>
                  <dt>접수</dt><dd>{new Date(filing.createdEpoch * 1000).toLocaleString("ko-KR")}</dd>
                  {filing.handledEpoch && <><dt>결정</dt><dd>{new Date(filing.handledEpoch * 1000).toLocaleString("ko-KR")}</dd></>}
                  <dt>신청 이유</dt><dd className="court-pre">{filing.reason}</dd>
                  {filing.plan && <><dt>변제계획</dt><dd className="court-pre">{filing.plan}</dd></>}
                </dl>
                <div className={`court-outcome${outcome.decided ? ` ${filing.status}` : ""}`}><h4>{outcome.heading}</h4><p className="court-pre">{outcome.text}</p></div>
              </td></tr>}
            </Fragment>;
          })}</tbody></table>}
    </section>
  </div>;
}
