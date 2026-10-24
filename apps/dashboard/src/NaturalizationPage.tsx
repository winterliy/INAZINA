import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { date, number, type Ctx } from "./common";

type Preview = { holdings: number; bonds: number; accounts: number; loans: number; netWorth: number; currency: string };
type Request = {
  request_id: number; user_id: string; nickname: string | null; from_nationality: string; to_nationality: string; reason: string;
  status: "pending" | "approved" | "rejected"; requested_epoch: number; decided_by: string | null; decided_epoch: number | null; note: string | null; preview?: Preview;
};

const STATUS = { pending: "심사 대기", approved: "승인", rejected: "거절" };

/** 귀화심사: pending requests first, each showing what approval would liquidate. */
export function NaturalizationPage({ ctx, onChange }: { ctx: Ctx; onChange: () => void }) {
  const { busy, run, say } = ctx;
  const [requests, setRequests] = useState<Request[]>([]);
  const [notes, setNotes] = useState<Record<number, string>>({});

  const load = useCallback(async () => setRequests((await api<{ requests: Request[] }>("/api/admin/naturalization")).requests), []);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "귀화 신청을 불러오지 못했습니다.", true)); }, [load]);

  function decide(item: Request, approve: boolean) {
    const who = item.nickname ?? item.user_id;
    if (!window.confirm(approve
      ? `${who}의 ${item.to_nationality} 귀화를 승인할까요?\n\n보유 자산을 현재가로 정리하고 국적을 옮깁니다. 되돌릴 수 없습니다.`
      : `${who}의 귀화 신청을 거절할까요?`)) return;
    void run(() => api<{ message: string }>(`/api/admin/naturalization/${item.request_id}`, { method: "POST", body: JSON.stringify({ action: approve ? "approve" : "reject", note: notes[item.request_id] ?? "" }) }),
      "귀화 신청을 처리하지 못했습니다.", (result) => { say(result.message); void load(); onChange(); });
  }

  const pending = requests.filter((item) => item.status === "pending");
  const decided = requests.filter((item) => item.status !== "pending");
  return <div className="nat">
    <section className="nat-queue" aria-label="심사 대기">
      {pending.map((item) => <article key={item.request_id} className="nat-card">
        <header className="nat-card-head">
          <div><p className="eyebrow">신청 #{item.request_id} · {date(item.requested_epoch)}</p><h2>{item.nickname ?? item.user_id}</h2><p className="mono nat-id">{item.user_id}</p></div>
          <div className="nat-route"><span>{item.from_nationality}</span><svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M13 6l6 6-6 6" /></svg><strong>{item.to_nationality}</strong></div>
        </header>
        {item.reason && <p className="nat-reason">“{item.reason}”</p>}
        {item.preview && <dl className="nat-preview">
          <div><dt>매각할 종목</dt><dd>{number(item.preview.holdings)}개</dd></div>
          <div><dt>국채</dt><dd>{number(item.preview.bonds)}건</dd></div>
          <div><dt>해지할 통장</dt><dd>{number(item.preview.accounts)}개</dd></div>
          <div><dt>상환할 대출</dt><dd>{number(item.preview.loans)} {item.preview.currency}</dd></div>
          <div><dt>순자산</dt><dd>{number(item.preview.netWorth)}</dd></div>
        </dl>}
        <p className="nat-warning">승인하면 위 자산을 현재가로 정리하고 새 나라 통화로 환전한 뒤 국적을 옮깁니다. 되돌릴 수 없습니다.</p>
        <div className="nat-actions">
          <input aria-label={`신청 #${item.request_id} 메모`} placeholder="메모 · 거절 사유 (선택)" value={notes[item.request_id] ?? ""} onChange={(event) => setNotes({ ...notes, [item.request_id]: event.target.value })} />
          <button type="button" className="outline" disabled={busy} onClick={() => decide(item, false)}>거절</button>
          <button type="button" className="btn primary" disabled={busy} onClick={() => decide(item, true)}>승인</button>
        </div>
      </article>)}
      {!pending.length && <div className="nat-empty"><h2>심사할 신청이 없습니다</h2><p>시민이 Discord에서 /귀화신청을 하면 여기에 나타나고, 관리자에게 DM이 갑니다.</p></div>}
    </section>

    {decided.length > 0 && <section className="nat-history" aria-label="처리한 신청">
      <h2>처리한 신청</h2>
      <ul>{decided.map((item) => <li key={item.request_id}>
        <span className={item.status === "approved" ? "tag ok" : "tag warn"}>{STATUS[item.status]}</span>
        <span className="nat-history-who">#{item.request_id} {item.nickname ?? item.user_id}</span>
        <span className="muted">{item.from_nationality} → {item.to_nationality}</span>
        <span className="muted">{item.decided_epoch ? date(item.decided_epoch) : "—"}{item.note ? ` · ${item.note}` : ""}</span>
      </li>)}</ul>
    </section>}
  </div>;
}
