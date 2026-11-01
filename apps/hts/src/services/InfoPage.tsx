import { useState } from "react";
import type { FormEvent } from "react";
import { ActionCard, type ServiceProps } from "./ServiceKit";

const FORMS = { contact: { title: "📞 Contact · 문의", who: "문의" }, report: { title: "🚨 Report · 신고", who: "신고" } } as const;
const ERRORS: Record<string, string> = {
  feedback_rate_limited: "짧은 시간에 너무 많이 보냈습니다. 10분 뒤 다시 시도해 주세요.", consent_required: "개인정보 수집·이용에 동의해 주세요.",
  invalid_email: "이메일 주소를 확인해 주세요.", invalid_title: "제목을 확인해 주세요.", invalid_body: "내용을 확인해 주세요.", invalid_discordId: "디스코드 아이디를 확인해 주세요.",
};

/** Contact/Report from the 나루: the same form as the official site, delivered to the dashboard's 문의·신고. */
function FeedbackCard() {
  const [kind, setKind] = useState<keyof typeof FORMS>("contact");
  const [status, setStatus] = useState<{ text: string; ok: boolean } | null>(null);
  const [sending, setSending] = useState(false);

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setSending(true);
    setStatus(null);
    try {
      const response = await fetch("/api/site/feedback", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...Object.fromEntries(new FormData(form)), kind }) });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error ?? "send_failed");
      setStatus({ text: `${FORMS[kind].who}이 운영진에게 전달되었습니다. 접수 번호 #${result.feedback.id}`, ok: true });
      form.reset();
    } catch (error) {
      setStatus({ text: ERRORS[error instanceof Error ? error.message : ""] ?? "보내지 못했습니다. 잠시 후 다시 시도해 주세요.", ok: false });
    } finally {
      setSending(false);
    }
  }

  return <form className="svc-card svc-feedback" onSubmit={send}>
    <header><h3>{FORMS[kind].title}</h3><p>운영진에게 바로 전달됩니다. 답변은 이메일이나 Discord로 드립니다.</p></header>
    <div className="svc-feedback-kind" role="radiogroup" aria-label="종류">{(Object.keys(FORMS) as (keyof typeof FORMS)[]).map((key) =>
      <button key={key} type="button" role="radio" aria-checked={kind === key} onClick={() => { setKind(key); setStatus(null); }}>{FORMS[key].who}</button>)}</div>
    <label>제목<input name="title" required maxLength={120} autoComplete="off" /></label>
    <label>내용<textarea name="body" required maxLength={4000} rows={5} /></label>
    <div className="svc-two">
      <label>디스코드 아이디<input name="discordId" required maxLength={64} autoComplete="off" /></label>
      <label>이메일<input name="email" type="email" required maxLength={254} autoComplete="email" /></label>
    </div>
    <p className="svc-feedback-consent">수집 항목: 디스코드 아이디, 이메일, 작성 내용 · 목적: 문의·신고 확인과 답변, 부정 이용 조사 · 보유 기간: 접수 후 1년</p>
    <label className="svc-check"><input type="checkbox" name="consent" required /> 개인정보 수집·이용에 동의합니다.</label>
    <button className="svc-submit" disabled={sending}>{sending ? "보내는 중…" : `${FORMS[kind].who} 보내기`}</button>
    {status && <div className={status.ok ? "svc-outcome ok" : "svc-outcome failed"} role={status.ok ? "status" : "alert"}><p>{status.text}</p></div>}
  </form>;
}

/** 정보 · 고객센터: lookups and requests to the administrators. */
export function InfoPage({ kit }: ServiceProps) {
  return <div className="svc-info">
    <h3 className="svc-section-title">금융 정보</h3>
    <div className="svc-two">
      <a className="svc-card svc-link-card" href="/financials" target="_blank" rel="noopener"><header><h3>기업 재무제표 ↗</h3><p>회사의 자산·부채·자본, 손익과 주요 비율을 새 탭의 재무제표 화면에서 봅니다.</p></header></a>
    </div>
    <h3 className="svc-section-title">고객센터</h3>
    <a className="svc-help-link" href="https://dramatic-bread-b4a.notion.site/NAKWON-BOTS-3ea27b50c7bf80e0904ef783e0e5dfc4" target="_blank" rel="noreferrer"><b>📚 도움말 (/도움말)</b><small>봇·나루 이용 안내와 정책 문서</small></a>
    <FeedbackCard />
    <div className="svc-two">
      <ActionCard kit={kit} name="주총소집" title="주주총회 소집" description="최대주주만 소집할 수 있습니다. 주주들에게 Discord로 참석 여부를 묻습니다." submit="소집 요청" />
      <ActionCard kit={kit} name="파산" title="파산 신청" description="현재 자산 현황이 관리자에게 전달됩니다. 관리자 검토 후 처리됩니다." submit="파산 신청하기" />
    </div>
  </div>;
}
