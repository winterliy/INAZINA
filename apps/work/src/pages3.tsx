import { useState } from "react";
import { api, del, get, post } from "./api.ts";
import { Attempt, Empty, Err, Field, useLoad, when } from "./ui.tsx";
import { MemberPick, type Me } from "./pages1.tsx";

export function Projects() {
  const list = useLoad<any[]>(() => get("/projects"));
  const [id, setId] = useState<number>();
  const p = useLoad<any>(() => (id ? get(`/projects/${id}`) : Promise.resolve(undefined)), [id]);
  const [name, setName] = useState(""); const [people, setPeople] = useState<number[]>([]);
  const [t, setT] = useState({ title: "", assigneeId: "", dueDate: "" });
  const cols: [string, string][] = [["todo", "할 일"], ["doing", "진행 중"], ["done", "완료"]];
  const move = (task: any, status: string) => api("PATCH", `/tasks/${task.id}`, { status, assigneeId: task.assignee_id, dueDate: task.due_date }).then(p.reload);
  return <>
    <h2>프로젝트/업무</h2><Err text={list.error || p.error} />
    <div className="split"><div><div className="card list">{(list.data ?? []).map((x) => <div key={x.id} className={id === x.id ? "on" : ""} onClick={() => setId(x.id)}><span>{x.name}</span><span className="badge">{x.open_tasks}</span></div>)}{(list.data ?? []).length === 0 && <Empty />}</div>
      <div className="card"><Field label="새 프로젝트"><input value={name} onChange={(e) => setName(e.target.value)} /></Field><Field label="참여자"><MemberPick value={people} onChange={setPeople} /></Field><Attempt action={async () => { const r = await post("/projects", { name, memberIds: people }); setName(""); setPeople([]); list.reload(); setId(r.id); }}>만들기</Attempt></div></div>
    <div>{!p.data ? <div className="card"><Empty text="프로젝트를 선택하세요." /></div> : <>
      <div className="card"><div className="row"><input placeholder="새 업무" value={t.title} onChange={(e) => setT({ ...t, title: e.target.value })} />
        <select value={t.assigneeId} onChange={(e) => setT({ ...t, assigneeId: e.target.value })}><option value="">담당자 없음</option>{p.data.members.map((m: any) => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select>
        <input type="date" value={t.dueDate} onChange={(e) => setT({ ...t, dueDate: e.target.value })} /><Attempt action={async () => { await post(`/projects/${id}/tasks`, { title: t.title, assigneeId: t.assigneeId || null, dueDate: t.dueDate || null }); setT({ title: "", assigneeId: "", dueDate: "" }); p.reload(); }}>추가</Attempt></div></div>
      <div className="grid">{cols.map(([s, label]) => <div key={s} className="card"><h3>{label}</h3>{p.data.tasks.filter((x: any) => x.status === s).map((x: any) => <div key={x.id} className="card"><b>{x.title}</b><br /><span className="muted">{p.data.members.find((m: any) => m.id === x.assignee_id)?.display_name ?? "담당 없음"}{x.due_date ? ` · ~${x.due_date}` : ""}</span>
        <div className="row">{cols.filter(([o]) => o !== s).map(([o, l]) => <button key={o} className="ghost" style={{ padding: "0 8px" }} onClick={() => move(x, o)}>→ {l}</button>)}</div></div>)}</div>)}</div></>}</div></div>
  </>;
}

export function Attendance({ me }: { me: Me }) {
  const month = new Date().toISOString().slice(0, 7);
  const list = useLoad<any[]>(() => get(`/attendance?month=${month}`));
  const leave = useLoad<any[]>(() => get("/leave"));
  const all = useLoad<any[]>(() => (me.member.role === "admin" ? get("/leave?all=1") : Promise.resolve([])));
  const [f, setF] = useState({ kind: "annual", startsOn: "", endsOn: "", reason: "" });
  const kinds: Record<string, string> = { annual: "연차", half: "반차", sick: "병가", other: "기타" };
  const states: Record<string, string> = { pending: "대기", approved: "승인", rejected: "반려", cancelled: "취소" };
  return <>
    <h2>근태/휴가</h2><Err text={list.error || leave.error} />
    <div className="grid"><div className="card"><h3>출퇴근 ({month})</h3><div className="row"><Attempt action={async () => { await post("/attendance/clock-in"); list.reload(); }}>출근</Attempt><Attempt className="ghost" action={async () => { await post("/attendance/clock-out"); list.reload(); }}>퇴근</Attempt></div>
      <table><tbody>{(list.data ?? []).map((a) => <tr key={a.work_date}><td>{a.work_date}</td><td>{when(a.clock_in).slice(11)}</td><td>{when(a.clock_out).slice(11)}</td></tr>)}</tbody></table></div>
      <div className="card"><h3>휴가 신청</h3><Field label="종류"><select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>{Object.entries(kinds).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
        <div className="row"><Field label="시작"><input type="date" value={f.startsOn} onChange={(e) => setF({ ...f, startsOn: e.target.value })} /></Field><Field label="끝"><input type="date" value={f.endsOn} onChange={(e) => setF({ ...f, endsOn: e.target.value })} /></Field></div>
        <Field label="사유"><input value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} /></Field><Attempt action={async () => { await post("/leave", f); leave.reload(); }}>신청</Attempt>
        <table><tbody>{(leave.data ?? []).map((l) => <tr key={l.id}><td>{kinds[l.kind]}</td><td>{l.starts_on}~{l.ends_on}</td><td>{states[l.status]}</td><td>{l.status === "pending" && <Attempt className="ghost" action={async () => { await post(`/leave/${l.id}/cancel`); leave.reload(); }}>취소</Attempt>}</td></tr>)}</tbody></table></div>
      {me.member.role === "admin" && <div className="card"><h3>휴가 승인(관리자)</h3><table><tbody>{(all.data ?? []).map((l) => <tr key={l.id}><td>{l.member}</td><td>{kinds[l.kind]} {l.starts_on}~{l.ends_on}</td><td>{states[l.status]}</td><td>{l.status === "pending" && <div className="row"><Attempt action={async () => { await post(`/leave/${l.id}/decide`, { decision: "approved" }); all.reload(); }}>승인</Attempt><Attempt className="danger" action={async () => { await post(`/leave/${l.id}/decide`, { decision: "rejected" }); all.reload(); }}>반려</Attempt></div>}</td></tr>)}</tbody></table></div>}</div>
  </>;
}

function Access() {
  const reqs = useLoad<any[]>(() => get("/access/requests"));
  const invites = useLoad<any[]>(() => get("/invites"));
  const [note, setNote] = useState(""); const [code, setCode] = useState("");
  return <div className="card"><h3>입장 관리(관리자)</h3>
    <p className="muted">허용된 계정만 들어올 수 있습니다. 초대 코드를 주거나, 가입 신청을 승인하세요.</p>
    <h3>가입 신청</h3>{(reqs.data ?? []).length === 0 ? <Empty text="대기 중인 신청이 없습니다." /> : reqs.data!.map((r) => <div key={r.id} className="msg"><b>{r.display_name}</b><span className="muted">{r.user_ref} · {when(r.requested_at)} {r.message}</span>
      <div className="row"><Attempt action={async () => { await post(`/access/requests/${r.id}/decide`, { decision: "approved" }); reqs.reload(); }}>승인</Attempt><Attempt className="danger" action={async () => { await post(`/access/requests/${r.id}/decide`, { decision: "rejected" }); reqs.reload(); }}>거절</Attempt></div></div>)}
    <h3>초대 코드</h3><div className="row"><input placeholder="메모(누구에게 주는지)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={120} /><Attempt action={async () => { const r = await post("/invites", { note, days: 7 }); setCode(r.code); setNote(""); invites.reload(); }}>7일짜리 코드 만들기</Attempt></div>
    {code && <p>코드: <b>{code}</b> <span className="muted">(지금 한 번만 보입니다. 한 사람만 쓸 수 있습니다.)</span></p>}
    <table><tbody>{(invites.data ?? []).map((i) => <tr key={i.id}><td>{i.note}</td><td className="muted">~{when(i.expires_at)}</td><td>{i.used_at ? "사용됨" : "미사용"}</td><td>{!i.used_at && <button className="ghost" onClick={() => del(`/invites/${i.id}`).then(invites.reload)}>취소</button>}</td></tr>)}</tbody></table></div>;
}

export function Org({ me }: { me: Me }) {
  const members = useLoad<any[]>(() => get("/members"));
  const deps = useLoad<any>(() => get("/departments"));
  const profile = useLoad<any>(() => get("/me"));
  const [p, setP] = useState<{ displayName: string; jobTitle: string; phone: string }>();
  const [dep, setDep] = useState({ name: "", parentId: "" }); const [assign, setAssign] = useState({ departmentId: "", memberId: "" });
  const form = p ?? (profile.data ? { displayName: profile.data.profile.display_name, jobTitle: profile.data.profile.job_title ?? "", phone: profile.data.profile.phone ?? "" } : undefined);
  const isAdmin = me.member.role === "admin";
  const tree = (parent: number | null, depth = 0): any[] => (deps.data?.departments ?? []).filter((d: any) => d.parent_id === parent).flatMap((d: any) => [
    <div key={d.id} className="msg" style={{ paddingLeft: depth * 18 }}><b>{d.name}</b> <span className="muted">{(deps.data.members as any[]).filter((m) => m.department_id === d.id).map((m) => `${members.data?.find((x) => x.id === m.member_id)?.display_name ?? "?"}${m.is_head ? "(장)" : ""}`).join(", ")}</span></div>, ...tree(d.id, depth + 1)]);
  return <>
    <h2>계정/조직</h2><Err text={members.error || deps.error} />
    <div className="grid"><div className="card"><h3>내 프로필</h3>{form && <><Field label="이름"><input value={form.displayName} onChange={(e) => setP({ ...form, displayName: e.target.value })} /></Field><Field label="직위"><input value={form.jobTitle} onChange={(e) => setP({ ...form, jobTitle: e.target.value })} /></Field><Field label="전화"><input value={form.phone} onChange={(e) => setP({ ...form, phone: e.target.value })} /></Field>
      <Attempt action={async () => { await api("PATCH", "/me", form); setP(undefined); members.reload(); profile.reload(); }}>저장</Attempt></>}</div>
      <div className="card"><h3>조직도</h3>{tree(null)}{(deps.data?.departments ?? []).length === 0 && <Empty />}
        {isAdmin && <><h3>부서 추가</h3><div className="row"><input placeholder="부서 이름" value={dep.name} onChange={(e) => setDep({ ...dep, name: e.target.value })} /><select value={dep.parentId} onChange={(e) => setDep({ ...dep, parentId: e.target.value })}><option value="">최상위</option>{(deps.data?.departments ?? []).map((d: any) => <option key={d.id} value={d.id}>{d.name}</option>)}</select><Attempt action={async () => { await post("/departments", { name: dep.name, parentId: dep.parentId || null }); setDep({ name: "", parentId: "" }); deps.reload(); }}>추가</Attempt></div>
          <div className="row" style={{ marginTop: 8 }}><select value={assign.departmentId} onChange={(e) => setAssign({ ...assign, departmentId: e.target.value })}><option value="">부서</option>{(deps.data?.departments ?? []).map((d: any) => <option key={d.id} value={d.id}>{d.name}</option>)}</select><select value={assign.memberId} onChange={(e) => setAssign({ ...assign, memberId: e.target.value })}><option value="">구성원</option>{(members.data ?? []).map((m) => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select><Attempt action={async () => { await post(`/departments/${assign.departmentId}/members`, { memberId: Number(assign.memberId) }); deps.reload(); }}>배정</Attempt></div></>}</div>
      {isAdmin && <Access />}
      <div className="card"><h3>구성원</h3><table><tbody>{(members.data ?? []).map((m) => <tr key={m.id}><td>{m.display_name}</td><td className="muted">{m.job_title}</td><td className="muted">{m.phone}</td><td>{isAdmin && m.id !== me.member.id ? <select value={m.role} onChange={(e) => api("PATCH", `/members/${m.id}`, { role: e.target.value, status: "active" }).then(members.reload)}><option value="admin">관리자</option><option value="member">멤버</option><option value="guest">게스트</option></select> : <span className="badge">{m.role}</span>}</td></tr>)}</tbody></table></div></div>
  </>;
}
