import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../../shared/api";
import type { ServiceProps } from "./ServiceKit";
import "./GovernancePages.css";

type Choice = "for" | "against" | "abstain";
type Item = { itemId: number; seq: number; title: string; detail: string; tally: Record<Choice, number>; passed: boolean; myVote: Choice | null };
type Meeting = {
  meetingId: number; code: string; company: string; convener: string; place: string; meetingEpoch: number; closeEpoch: number;
  open: boolean; totalShares: number; canClose: boolean; attendees: { nickname: string; shares: number }[]; holderCount: number;
  me: { shares: number; attendance: "attend" | "absent" | null } | null; items: Item[];
};
type Data = { open: Meeting[]; minutes: Meeting[]; companies: { code: string; name: string }[] };

const CHOICE_LABEL: Record<Choice, string> = { for: "찬성", against: "반대", abstain: "기권" };
const ERRORS: Record<string, string> = {
  largest_holder_required: "해당 종목의 최대주주 또는 관리자만 소집할 수 있습니다.", preferred_no_vote: "우선주는 의결권이 없습니다. 보통주 코드로 소집해 주세요.",
  company_not_found: "회사를 찾을 수 없습니다.", no_holders: "해당 주식을 보유한 주주가 없습니다.", items_required: "의제를 1~10개 입력해 주세요.",
  item_title_required: "의제 제목을 모두 입력해 주세요.", place_required: "장소를 입력해 주세요.", invalid_schedule: "일시나 투표 기간을 확인해 주세요.",
  meeting_closed: "이미 종료된 주주총회입니다.", not_a_holder: "이 주주총회의 의결권이 없습니다.", convener_required: "소집한 주주만 종료할 수 있습니다.",
};
const reason = (cause: unknown) => { const text = cause instanceof Error ? cause.message : ""; return ERRORS[text] ?? (text || "처리하지 못했습니다."); };
const when = (epoch: number) => new Date(epoch * 1000).toLocaleString("ko-KR", { year: "numeric", month: "long", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit" });
const shares = (value: number) => value.toLocaleString("ko-KR", { maximumFractionDigits: 4 });
const pct = (value: number, total: number) => total > 0 ? `${(value / total * 100).toLocaleString("ko-KR", { maximumFractionDigits: 1 })}%` : "0%";
const localInput = (date: Date) => new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);

function Tally({ item, total }: { item: Item; total: number }) {
  return <div className="gov-tally">
    <div className="gov-bar" aria-hidden="true">{(["for", "against", "abstain"] as Choice[]).map((choice) => <span key={choice} className={choice} style={{ width: total ? `${item.tally[choice] / total * 100}%` : 0 }} />)}</div>
    <small>{(["for", "against", "abstain"] as Choice[]).map((choice) => `${CHOICE_LABEL[choice]} ${shares(item.tally[choice])}표 (${pct(item.tally[choice], total)})`).join(" · ")}</small>
  </div>;
}

function OpenMeeting({ meeting, reload }: { meeting: Meeting; reload: () => void }) {
  const [votes, setVotes] = useState<Record<number, Choice>>(() => Object.fromEntries(meeting.items.filter((item) => item.myVote).map((item) => [item.itemId, item.myVote!])));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  async function post(path: string, body: object, done: string) {
    setBusy(true); setMessage(null);
    try { await api(`/api/governance/meetings/${meeting.meetingId}/${path}`, { method: "POST", body: JSON.stringify(body) }); setMessage({ text: done, ok: true }); reload(); }
    catch (cause) { setMessage({ text: reason(cause), ok: false }); }
    finally { setBusy(false); }
  }
  const changed = meeting.items.some((item) => votes[item.itemId] && votes[item.itemId] !== item.myVote);
  return <article className="svc-card gov-meeting">
    <header className="gov-meeting-head">
      <div><span className="gov-badge live">투표 진행 중</span><h3>{meeting.company} <small>{meeting.code}</small></h3></div>
      <dl><div><dt>일시</dt><dd>{when(meeting.meetingEpoch)}</dd></div><div><dt>장소</dt><dd>{meeting.place}</dd></div><div><dt>투표 마감</dt><dd>{when(meeting.closeEpoch)}</dd></div><div><dt>소집</dt><dd>{meeting.convener}</dd></div></dl>
    </header>
    {meeting.me ? <div className="gov-me">
      <p>내 의결권 <b>{shares(meeting.me.shares)}표</b> <small>({pct(meeting.me.shares, meeting.totalShares)})</small></p>
      <div className="gov-seg" role="radiogroup" aria-label="참석 여부">{(["attend", "absent"] as const).map((value) => <button key={value} type="button" role="radio" aria-checked={meeting.me!.attendance === value} disabled={busy} onClick={() => void post("respond", { attendance: value }, value === "attend" ? "참석으로 응답했습니다." : "불참으로 응답했습니다.")}>{value === "attend" ? "참석" : "불참"}</button>)}</div>
    </div> : <p className="gov-muted">이 주주총회의 의결권이 없습니다. 결과만 볼 수 있습니다.</p>}
    <ol className="gov-items">{meeting.items.map((item) => <li key={item.itemId}>
      <div className="gov-item-head"><b>제{item.seq}호 의안</b><h4>{item.title}</h4></div>
      {item.detail && <p className="gov-detail">{item.detail}</p>}
      {meeting.me && <div className="gov-seg vote" role="radiogroup" aria-label={`제${item.seq}호 의안 투표`}>{(["for", "against", "abstain"] as Choice[]).map((choice) => <button key={choice} type="button" role="radio" className={choice} aria-checked={votes[item.itemId] === choice} onClick={() => setVotes({ ...votes, [item.itemId]: choice })}>{CHOICE_LABEL[choice]}</button>)}</div>}
      <Tally item={item} total={meeting.totalShares} />
    </li>)}</ol>
    {message && <p className={message.ok ? "account-success" : "account-error"} role={message.ok ? "status" : "alert"}>{message.text}</p>}
    <div className="gov-actions">
      {meeting.me && <button type="button" className="svc-submit" disabled={busy || !changed} onClick={() => void post("respond", { votes }, "투표를 제출했습니다. 마감 전까지 바꿀 수 있습니다.")}>{busy ? "제출 중…" : meeting.items.some((item) => item.myVote) ? "투표 수정" : "투표 제출"}</button>}
      {meeting.canClose && <button type="button" className="gov-ghost" disabled={busy} onClick={() => { if (confirm("투표를 마감하고 주주총회를 종료할까요? 되돌릴 수 없습니다.")) void post("close", {}, "주주총회를 종료했습니다."); }}>회의 종료</button>}
    </div>
  </article>;
}

function Minutes({ meeting }: { meeting: Meeting }) {
  return <details className="gov-minutes">
    <summary><span className="gov-date">{new Date(meeting.meetingEpoch * 1000).toLocaleDateString("ko-KR", { year: "numeric", month: "long", day: "numeric" })}</span><b>{meeting.company} 주주총회</b><small>{meeting.place} · 의안 {meeting.items.length}건 · 참석 {meeting.attendees.length}/{meeting.holderCount}명</small></summary>
    <div className="gov-minutes-body">
      <dl className="gov-facts">
        <div><dt>일시</dt><dd>{when(meeting.meetingEpoch)}</dd></div><div><dt>장소</dt><dd>{meeting.place}</dd></div>
        <div><dt>소집</dt><dd>{meeting.convener}</dd></div><div><dt>종료</dt><dd>{when(meeting.closeEpoch)}</dd></div>
        <div><dt>참석 의결권</dt><dd>{shares(meeting.attendees.reduce((sum, row) => sum + row.shares, 0))}표 / {shares(meeting.totalShares)}표</dd></div>
        {meeting.me && <div><dt>나</dt><dd>{meeting.me.attendance === "attend" ? "참석" : meeting.me.attendance === "absent" ? "불참" : "무응답"} · {shares(meeting.me.shares)}표</dd></div>}
      </dl>
      <h4>참석 주주</h4>
      <p className="gov-attendees">{meeting.attendees.length ? meeting.attendees.map((row) => <span key={row.nickname}>{row.nickname} <small>{shares(row.shares)}표</small></span>) : <span className="gov-muted">참석한 주주가 없습니다.</span>}</p>
      <h4>의결 결과</h4>
      <table className="gov-table"><thead><tr><th>의안</th><th>결과</th><th>찬성</th><th>반대</th><th>기권</th><th>내 투표</th></tr></thead>
        <tbody>{meeting.items.map((item) => <tr key={item.itemId}><td><b>제{item.seq}호</b> {item.title}{item.detail && <small>{item.detail}</small>}</td>
          <td><span className={item.passed ? "gov-badge pass" : "gov-badge fail"}>{item.passed ? "가결" : "부결"}</span></td>
          {(["for", "against", "abstain"] as Choice[]).map((choice) => <td key={choice}>{shares(item.tally[choice])}<small>{pct(item.tally[choice], meeting.totalShares)}</small></td>)}
          <td>{item.myVote ? CHOICE_LABEL[item.myVote] : "—"}</td></tr>)}</tbody></table>
    </div>
  </details>;
}

function Convene({ companies, reload }: { companies: Data["companies"]; reload: () => void }) {
  const [code, setCode] = useState(companies[0]?.code ?? "");
  const [place, setPlace] = useState("");
  const [at, setAt] = useState(() => localInput(new Date(Date.now() + 3_600_000)));
  const [hours, setHours] = useState("24");
  const [items, setItems] = useState([{ title: "", detail: "" }]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true); setMessage(null);
    try {
      const result = await api<{ holders: number }>("/api/governance/meetings", { method: "POST", body: JSON.stringify({ code, place, meetingEpoch: Math.floor(new Date(at).getTime() / 1000), votingHours: Number(hours), items }) });
      setMessage({ text: `소집했습니다. 주주 ${result.holders}명에게 Discord로 알립니다.`, ok: true });
      setPlace(""); setItems([{ title: "", detail: "" }]); reload();
    } catch (cause) { setMessage({ text: reason(cause), ok: false }); }
    finally { setBusy(false); }
  }
  return <form className="svc-card gov-convene" onSubmit={submit}>
    <header><h3>주주총회 소집</h3><p>최대주주 또는 관리자만 소집할 수 있습니다. 소집 시점 기준으로 의결권이 정해집니다. 법인 주주의 표는 그 법인의 과반 지배주주가 행사하며, 상호주(10% 초과)는 의결권이 없습니다.</p></header>
    <div className="svc-two">
      <label>회사 (보통주 코드)<input list="gov-companies" value={code} onChange={(event) => setCode(event.target.value.toUpperCase())} placeholder="예: INZ" required /></label>
      <datalist id="gov-companies">{companies.map((company) => <option key={company.code} value={company.code}>{company.name}</option>)}</datalist>
      <label>장소<input value={place} maxLength={200} onChange={(event) => setPlace(event.target.value)} placeholder="예: 본사 대회의실 · Discord #주총" required /></label>
      <label>일시<input type="datetime-local" value={at} onChange={(event) => setAt(event.target.value)} required /></label>
      <label>투표 기간<select value={hours} onChange={(event) => setHours(event.target.value)}>{[["1", "1시간"], ["6", "6시간"], ["24", "1일"], ["72", "3일"], ["168", "7일"]].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    </div>
    <fieldset className="gov-agenda"><legend>의제</legend>
      {items.map((item, index) => <div key={index} className="gov-agenda-row">
        <b>제{index + 1}호</b>
        <input value={item.title} maxLength={120} placeholder="의안 제목 (예: 이사 선임의 건)" required onChange={(event) => setItems(items.map((row, at) => at === index ? { ...row, title: event.target.value } : row))} />
        <textarea value={item.detail} maxLength={1000} rows={2} placeholder="설명 (선택)" onChange={(event) => setItems(items.map((row, at) => at === index ? { ...row, detail: event.target.value } : row))} />
        {items.length > 1 && <button type="button" className="gov-ghost" aria-label={`제${index + 1}호 의안 삭제`} onClick={() => setItems(items.filter((_, at) => at !== index))}>삭제</button>}
      </div>)}
      {items.length < 10 && <button type="button" className="gov-ghost" onClick={() => setItems([...items, { title: "", detail: "" }])}>+ 의안 추가</button>}
    </fieldset>
    {message && <p className={message.ok ? "account-success" : "account-error"} role={message.ok ? "status" : "alert"}>{message.text}</p>}
    <button className="svc-submit" disabled={busy}>{busy ? "소집 중…" : "주주총회 소집"}</button>
  </form>;
}

/** 주주총회: open meetings to vote in, convening a new one, and minutes of past ones. */
export function MeetingsPage(_props: ServiceProps) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => { api<Data>("/api/governance/meetings").then(setData).catch((cause) => setError(reason(cause))); }, []);
  useEffect(load, [load]);
  if (error && !data) return <p className="account-error" role="alert">{error}</p>;
  if (!data) return <p className="svc-empty">불러오는 중…</p>;
  return <div className="gov-page">
    <section><h3 className="svc-section-title">진행 중인 주주총회 <small>{data.open.length}</small></h3>
      {data.open.length ? data.open.map((meeting) => <OpenMeeting key={meeting.meetingId} meeting={meeting} reload={load} />) : <p className="svc-empty gov-empty">지금 투표할 수 있는 주주총회가 없습니다.</p>}
    </section>
    <Convene companies={data.companies} reload={load} />
    <section><h3 className="svc-section-title">지난 회의록 <small>{data.minutes.length}</small></h3>
      {data.minutes.length ? <div className="gov-minutes-list">{data.minutes.map((meeting) => <Minutes key={meeting.meetingId} meeting={meeting} />)}</div> : <p className="svc-empty gov-empty">아직 끝난 주주총회가 없습니다.</p>}
    </section>
  </div>;
}
