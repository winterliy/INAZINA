import { useState } from "react";
import { get, post } from "./api.ts";
import { Attempt, Err, Field, useLoad } from "./ui.tsx";
import { Calendar, Chat, Contacts, Home, Mail, type Me } from "./pages1.tsx";
import { Approvals, Boards, Docs, Drive } from "./pages2.tsx";
import { Attendance, Org, Projects } from "./pages3.tsx";

const pages = [["home", "홈"], ["mail", "내부 메일"], ["chat", "메신저"], ["calendar", "캘린더"], ["drive", "드라이브"], ["docs", "문서"], ["approvals", "전자결재"], ["boards", "게시판/공지"], ["projects", "프로젝트/업무"], ["attendance", "근태/휴가"], ["contacts", "연락처"], ["org", "계정/조직"]] as const;

function Gate({ state, reload }: { state: string; reload: () => void }) {
  const [code, setCode] = useState(""); const [message, setMessage] = useState("");
  return <main style={{ maxWidth: 460, margin: "10vh auto" }}><div className="card">
    <h2>INAZINA Work</h2>
    {state === "retired" && <p>이 계정은 사용할 수 없습니다.</p>}
    {state === "pending" && <><p>가입 신청이 접수되었습니다. 관리자가 승인하면 들어올 수 있습니다.</p><button className="ghost" onClick={reload}>상태 새로 확인</button></>}
    {(state === "none" || state === "rejected") && <>
      <p>이 서비스는 허용된 계정만 사용할 수 있습니다.{state === "rejected" ? " 이전 신청은 승인되지 않았습니다(하루 뒤 다시 신청할 수 있습니다)." : ""}</p>
      <Field label="초대 코드가 있다면 입력하세요"><input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="off" /></Field>
      {!code && <Field label="초대 코드가 없다면 신청 메시지(선택)"><input value={message} maxLength={300} onChange={(e) => setMessage(e.target.value)} /></Field>}
      <Attempt action={async () => { await post("/access/apply", code ? { code } : { message }); reload(); }}>{code ? "코드로 입장" : "가입 신청"}</Attempt></>}
  </div></main>;
}

export function App() {
  const access = useLoad<{ state: string }>(() => get("/access"));
  if (!access.data) return <main><Err text={access.error} /><p className="muted">불러오는 중…</p></main>;
  if (access.data.state !== "member") return <Gate state={access.data.state} reload={access.reload} />;
  return <Workspace />;
}

function Workspace() {
  const [page, setPage] = useState<string>(location.hash.slice(1) || "home");
  const me = useLoad<Me>(() => get("/me"));
  const home = useLoad<any>(() => get("/home"), [page]);
  const go = (p: string) => { setPage(p); location.hash = p; };
  if (!me.data) return <main><Err text={me.error} /><p className="muted">불러오는 중…</p></main>;
  const badge: Record<string, number> = { mail: home.data?.unreadMail ?? 0, approvals: home.data?.pendingApprovals ?? 0 };
  return <div className="shell">
    <nav><h1>INAZINA Work</h1>{pages.map(([key, label]) => <button key={key} className={page === key ? "on" : ""} onClick={() => go(key)}>{label}{badge[key] ? <span className="badge new">{badge[key]}</span> : null}</button>)}<div className="me">{me.data.member.name}<br />{me.data.member.role === "admin" ? "관리자" : "멤버"}</div></nav>
    <main key={page}>
      {page === "home" && <Home go={go} />}{page === "mail" && <Mail />}{page === "chat" && <Chat me={me.data} />}{page === "calendar" && <Calendar />}{page === "drive" && <Drive />}
      {page === "docs" && <Docs />}{page === "approvals" && <Approvals me={me.data} />}{page === "boards" && <Boards me={me.data} />}{page === "projects" && <Projects />}
      {page === "attendance" && <Attendance me={me.data} />}{page === "contacts" && <Contacts />}{page === "org" && <Org me={me.data} />}
    </main></div>;
}
