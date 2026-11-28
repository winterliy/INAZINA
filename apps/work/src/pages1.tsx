import { useEffect, useState } from "react";
import { del, get, post } from "./api.ts";
import { Attempt, Empty, Err, Field, useLoad, when } from "./ui.tsx";

export type Me = { member: { id: number; name: string; role: string; mustChange?: boolean } };
type Member = { id: number; display_name: string; job_title: string | null; phone: string | null; role: string };
export const useMembers = () => useLoad<Member[]>(() => get("/members"));

function MemberPick({ value, onChange, exclude }: { value: number[]; onChange: (v: number[]) => void; exclude?: number }) {
  const { data } = useMembers();
  return <select multiple value={value.map(String)} onChange={(e) => onChange([...e.target.selectedOptions].map((o) => Number(o.value)))} style={{ minWidth: 200, height: 90 }}>
    {(data ?? []).filter((m) => m.id !== exclude).map((m) => <option key={m.id} value={m.id}>{m.display_name}{m.job_title ? ` (${m.job_title})` : ""}</option>)}
  </select>;
}
export { MemberPick };

export function Home({ go }: { go: (page: string) => void }) {
  const { data, error, reload } = useLoad<any>(() => get("/home"));
  if (!data) return <Err text={error} />;
  return <>
    <h2>홈</h2><Err text={error} />
    <div className="grid">
      <div className="card"><h3>내 할 일</h3><p>안 읽은 메일 <b>{data.unreadMail}</b> · 내 차례 결재 <b>{data.pendingApprovals}</b></p>
        <div className="row"><button className="ghost" onClick={() => go("mail")}>메일</button><button className="ghost" onClick={() => go("approvals")}>결재</button></div>
        {data.myTasks.length === 0 ? <Empty text="맡은 업무가 없습니다." /> : data.myTasks.map((t: any) => <div key={t.id} className="msg">{t.title} <span className="muted">{t.project}{t.due_date ? ` · ~${t.due_date}` : ""}</span></div>)}</div>
      <div className="card"><h3>이번 주 일정</h3>{data.events.length === 0 ? <Empty /> : data.events.map((e: any) => <div key={e.id} className="msg"><b>{when(e.starts_at)}</b>{e.title}</div>)}</div>
      <div className="card"><h3>공지</h3>{data.notices.length === 0 ? <Empty /> : data.notices.map((n: any) => <div key={n.id} className="msg">{n.must_read && !n.read_by_me ? <span className="badge new">필독</span> : null} {n.title} <span className="muted">{when(n.created_at)}</span></div>)}</div>
      <div className="card"><h3>알림 <Attempt className="ghost" action={async () => { await post("/notifications/read"); reload(); }}>모두 읽음</Attempt></h3>
        {data.notifications.length === 0 ? <Empty /> : data.notifications.map((n: any) => <div key={n.id} className="msg" style={{ opacity: n.read_at ? 0.55 : 1 }}>{n.text} <span className="muted">{when(n.created_at)}</span></div>)}</div>
    </div>
  </>;
}

export function Mail() {
  const [box, setBox] = useState("inbox");
  const [openId, setOpenId] = useState<number>();
  const [composing, setComposing] = useState(false);
  const list = useLoad<any[]>(() => get(`/mail?box=${box}`), [box]);
  const detail = useLoad<any>(() => (openId ? get(`/mail/${openId}`) : Promise.resolve(undefined)), [openId]);
  const [to, setTo] = useState<number[]>([]); const [cc, setCc] = useState<number[]>([]);
  const [subject, setSubject] = useState(""); const [text, setText] = useState("");
  return <>
    <h2>내부 메일</h2>
    <div className="row" style={{ marginBottom: 10 }}>{["inbox", "sent", "trash"].map((b) => <button key={b} className={box === b ? "" : "ghost"} onClick={() => { setBox(b); setOpenId(undefined); }}>{{ inbox: "받은 편지함", sent: "보낸 편지함", trash: "휴지통" }[b]}</button>)}
      <button onClick={() => setComposing(!composing)}>{composing ? "닫기" : "새 메일"}</button></div>
    {composing && <div className="card">
      <Field label="받는 사람(여러 명: Cmd/Ctrl 클릭)"><MemberPick value={to} onChange={setTo} /></Field><Field label="참조"><MemberPick value={cc} onChange={setCc} /></Field>
      <Field label="제목"><input value={subject} onChange={(e) => setSubject(e.target.value)} maxLength={200} /></Field><Field label="내용"><textarea value={text} onChange={(e) => setText(e.target.value)} /></Field>
      <Attempt action={async () => { await post("/mail", { to, cc, subject, body: text }); setComposing(false); setSubject(""); setText(""); setTo([]); setCc([]); list.reload(); }}>보내기</Attempt></div>}
    <Err text={list.error} />
    <div className="split"><div className="card list">{(list.data ?? []).length === 0 ? <Empty /> : list.data!.map((m) =>
      <div key={m.id} className={openId === m.id ? "on" : ""} onClick={() => setOpenId(m.id)}><span>{!m.read_at && box === "inbox" && <span className="badge new">NEW</span>} {m.subject}<br /><span className="muted">{m.sender ?? m.recipients}</span></span><span className="muted">{when(m.sent_at)}</span></div>)}</div>
      <div className="card">{detail.data ? <><h3>{detail.data.subject}</h3><p className="muted">보낸 사람 {detail.data.sender} · 받는 사람 {detail.data.recipients.map((r: any) => r.display_name).join(", ")} · {when(detail.data.sent_at)}</p><pre style={{ whiteSpace: "pre-wrap", font: "inherit" }}>{detail.data.body}</pre>
        {box === "inbox" && <Attempt className="ghost" action={async () => { await del(`/mail/${openId}`); setOpenId(undefined); list.reload(); }}>휴지통으로</Attempt>}</> : <Empty text="메일을 선택하세요." />}</div></div>
  </>;
}

export function Chat({ me }: { me: Me }) {
  const channels = useLoad<any[]>(() => get("/chat/channels"));
  const [current, setCurrent] = useState<number>();
  const [messages, setMessages] = useState<any[]>([]);
  const [text, setText] = useState(""); const [picked, setPicked] = useState<number[]>([]); const [name, setName] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (!current) return;
    let alive = true;
    const pull = () => get(`/chat/channels/${current}/messages`).then((m) => alive && setMessages(m)).catch(() => {});
    void pull(); const timer = setInterval(pull, 4000);
    return () => { alive = false; clearInterval(timer); };
  }, [current]);
  useEffect(() => { const t = setInterval(channels.reload, 8000); return () => clearInterval(t); }, [channels.reload]);
  return <>
    <h2>메신저</h2><Err text={error || channels.error} />
    <div className="split"><div>
      <div className="card list">{(channels.data ?? []).map((c) => <div key={c.id} className={current === c.id ? "on" : ""} onClick={() => setCurrent(c.id)}><span>{c.kind === "group" ? "# " : ""}{c.name}</span>{c.unread > 0 && <span className="badge new">{c.unread}</span>}</div>)}</div>
      <div className="card"><Field label="대화 상대(1명이면 1:1, 여러 명이면 그룹)"><MemberPick value={picked} onChange={setPicked} exclude={me.member.id} /></Field>
        {picked.length > 1 && <Field label="그룹 이름"><input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} /></Field>}
        <Attempt action={async () => { const r = await post("/chat/channels", { kind: picked.length > 1 ? "group" : "direct", name, memberIds: picked }); setPicked([]); setName(""); channels.reload(); setCurrent(r.id); }}>대화 시작</Attempt></div></div>
    <div className="card">{!current ? <Empty text="대화를 선택하세요." /> : <>
      <div style={{ minHeight: 260, maxHeight: 460, overflow: "auto" }}>{messages.map((m) => <div key={m.id} className="msg"><b>{m.sender}</b>{m.body ?? <i className="muted">삭제된 메시지</i>} <span className="muted">{when(m.created_at)}</span>{m.sender_id === me.member.id && m.body && <button className="ghost" style={{ marginLeft: 6, padding: "0 6px" }} onClick={() => del(`/chat/messages/${m.id}`).then(() => get(`/chat/channels/${current}/messages`).then(setMessages))}>삭제</button>}</div>)}</div>
      <div className="row"><input style={{ flex: 1 }} value={text} maxLength={4000} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && text.trim() && !e.nativeEvent.isComposing) { void post(`/chat/channels/${current}/messages`, { body: text }).then(() => { setText(""); return get(`/chat/channels/${current}/messages`).then(setMessages); }).catch(() => setError("보내지 못했습니다.")); } }} placeholder="메시지를 입력하고 Enter" /></div></>}</div></div>
  </>;
}

export function Calendar() {
  const [month, setMonth] = useState(new Date().toISOString().slice(0, 7));
  const events = useLoad<any[]>(() => { const [y, m] = month.split("-").map(Number); const last = new Date(y, m, 0).getDate(); return get(`/calendar/events?from=${month}-01&to=${month}-${String(last).padStart(2, "0")}`); }, [month]);
  const cals = useLoad<any[]>(() => get("/calendar/calendars"));
  const [f, setF] = useState({ calendarId: "", title: "", startsAt: "", endsAt: "", location: "" }); const [guests, setGuests] = useState<number[]>([]);
  return <>
    <h2>캘린더</h2><div className="row" style={{ marginBottom: 10 }}><input type="month" value={month} onChange={(e) => setMonth(e.target.value)} /></div>
    <Err text={events.error} />
    <div className="grid"><div className="card"><h3>일정</h3>{(events.data ?? []).length === 0 ? <Empty /> : events.data!.map((e) => <div key={e.id} className="msg"><b>{when(e.starts_at)}</b>{e.title} <span className="muted">[{e.calendar}]{e.location ? ` ${e.location}` : ""}{e.attendees ? ` · ${e.attendees}` : ""}</span>
      {e.my_response && <span> <select value={e.my_response} onChange={(x) => post(`/calendar/events/${e.id}/respond`, { response: x.target.value }).then(events.reload)}><option value="pending" disabled>응답 전</option><option value="yes">참석</option><option value="maybe">미정</option><option value="no">불참</option></select></span>}
      <button className="ghost" style={{ marginLeft: 6, padding: "0 6px" }} onClick={() => del(`/calendar/events/${e.id}`).then(events.reload).catch(() => {})}>삭제</button></div>)}</div>
      <div className="card"><h3>일정 추가</h3>
        <Field label="캘린더"><select value={f.calendarId} onChange={(e) => setF({ ...f, calendarId: e.target.value })}><option value="">선택</option>{(cals.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name} ({c.kind === "personal" ? "개인" : c.kind === "shared" ? "공유" : "자원"})</option>)}</select></Field>
        <Field label="제목"><input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field>
        <div className="row"><Field label="시작"><input type="datetime-local" value={f.startsAt} onChange={(e) => setF({ ...f, startsAt: e.target.value })} /></Field><Field label="끝"><input type="datetime-local" value={f.endsAt} onChange={(e) => setF({ ...f, endsAt: e.target.value })} /></Field></div>
        <Field label="장소"><input value={f.location} onChange={(e) => setF({ ...f, location: e.target.value })} /></Field><Field label="참석자"><MemberPick value={guests} onChange={setGuests} /></Field>
        <Attempt action={async () => { await post("/calendar/events", { ...f, calendarId: Number(f.calendarId), attendees: guests }); setF({ ...f, title: "" }); events.reload(); }}>추가</Attempt>
        <p className="muted">개인 캘린더가 없으면 먼저 만드세요.</p><Attempt className="ghost" action={async () => { await post("/calendar/calendars", { kind: "personal", name: "내 캘린더" }); cals.reload(); }}>내 캘린더 만들기</Attempt></div></div>
  </>;
}

export function Contacts() {
  const list = useLoad<any[]>(() => get("/contacts"));
  const [f, setF] = useState({ name: "", organization: "", email: "", phone: "", memo: "", shared: false });
  return <>
    <h2>연락처</h2><Err text={list.error} />
    <div className="grid"><div className="card"><table><tbody>{(list.data ?? []).map((c) => <tr key={c.id}><td>{c.name}{c.shared ? <span className="badge">공유</span> : null}</td><td>{c.organization}</td><td>{c.email}</td><td>{c.phone}</td><td><button className="ghost" onClick={() => del(`/contacts/${c.id}`).then(list.reload).catch(() => {})}>삭제</button></td></tr>)}</tbody></table>{(list.data ?? []).length === 0 && <Empty />}</div>
      <div className="card"><h3>추가</h3>{(["name", "organization", "email", "phone", "memo"] as const).map((k) => <Field key={k} label={{ name: "이름", organization: "소속", email: "이메일", phone: "전화", memo: "메모" }[k]}><input value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></Field>)}
        <label><input type="checkbox" checked={f.shared} onChange={(e) => setF({ ...f, shared: e.target.checked })} /> 모두에게 공유</label><br /><br />
        <Attempt action={async () => { await post("/contacts", f); setF({ name: "", organization: "", email: "", phone: "", memo: "", shared: false }); list.reload(); }}>저장</Attempt></div></div>
  </>;
}
