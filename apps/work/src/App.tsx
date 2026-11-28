import { useEffect, useState } from "react";
import { ApiError, errorText, get, post } from "./api.ts";
import { Attempt, Err, Field, useLoad } from "./ui.tsx";
import { Calendar, Chat, Contacts, Home, Mail, type Me } from "./pages1.tsx";
import { Approvals, Boards, Docs, Drive } from "./pages2.tsx";
import { Attendance, Org, Projects } from "./pages3.tsx";

const pages = [["home", "홈"], ["mail", "내부 메일"], ["chat", "메신저"], ["calendar", "캘린더"], ["drive", "드라이브"], ["docs", "문서"], ["approvals", "전자결재"], ["boards", "게시판/공지"], ["projects", "프로젝트/업무"], ["attendance", "근태/휴가"], ["contacts", "연락처"], ["org", "계정/조직"]] as const;

function Login({ done }: { done: () => void }) {
  const [mode, setMode] = useState<"login" | "join" | "apply">("login");
  const [f, setF] = useState({ id: "", password: "", name: "", code: "", message: "" });
  const [notice, setNotice] = useState("");
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return <main style={{ maxWidth: 420, margin: "8vh auto" }}><div className="card">
    <h2>INAZINA Work</h2>
    <div className="row" style={{ marginBottom: 12 }}>{([["login", "로그인"], ["join", "초대 코드로 가입"], ["apply", "가입 신청"]] as const).map(([k, l]) => <button key={k} className={mode === k ? "" : "ghost"} onClick={() => { setMode(k); setNotice(""); }}>{l}</button>)}</div>
    {notice && <p>{notice}</p>}
    <Field label="아이디"><input value={f.id} onChange={set("id")} autoComplete="username" autoCapitalize="none" /></Field>
    {mode !== "login" && <Field label="이름(표시용)"><input value={f.name} onChange={set("name")} maxLength={80} /></Field>}
    <Field label={mode === "login" ? "비밀번호" : "비밀번호(10자 이상)"}><input type="password" value={f.password} onChange={set("password")} autoComplete={mode === "login" ? "current-password" : "new-password"} /></Field>
    {mode === "join" && <Field label="초대 코드"><input value={f.code} onChange={set("code")} autoComplete="off" /></Field>}
    {mode === "apply" && <Field label="신청 메시지(선택)"><input value={f.message} maxLength={300} onChange={set("message")} /></Field>}
    <Attempt action={async () => {
      if (mode === "login") { await post("/auth/login", { id: f.id, password: f.password }); done(); }
      else if (mode === "join") { await post("/auth/signup", { id: f.id.trim().toLowerCase(), password: f.password, name: f.name, code: f.code }); done(); }
      else { await post("/auth/apply", { id: f.id.trim().toLowerCase(), password: f.password, name: f.name, message: f.message }); setNotice("가입 신청이 접수되었습니다. 승인되면 이 아이디와 비밀번호로 로그인하세요."); setMode("login"); setF({ ...f, password: "", message: "" }); }
    }}>{mode === "login" ? "로그인" : mode === "join" ? "가입하고 입장" : "신청"}</Attempt>
    <p className="muted">이 서비스의 아이디와 비밀번호는 INAZINA 게임 계정과 별개입니다.</p>
  </div></main>;
}

function ForceChange({ done }: { done: () => void }) {
  const [cur, setCur] = useState(""); const [next, setNext] = useState("");
  return <main style={{ maxWidth: 420, margin: "8vh auto" }}><div className="card"><h2>비밀번호를 바꿔 주세요</h2>
    <p className="muted">임시 비밀번호로 로그인했습니다. 새 비밀번호를 정해야 계속 쓸 수 있습니다.</p>
    <Field label="현재(임시) 비밀번호"><input type="password" value={cur} onChange={(e) => setCur(e.target.value)} autoComplete="current-password" /></Field>
    <Field label="새 비밀번호(10자 이상)"><input type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" /></Field>
    <Attempt action={async () => { await post("/auth/password", { current: cur, next }); done(); }}>변경</Attempt></div></main>;
}

export function App() {
  const [me, setMe] = useState<Me | "none" | undefined>();
  const [error, setError] = useState("");
  const load = () => get<Me>("/me").then(setMe).catch((e) => { if (e instanceof ApiError && e.status === 401) setMe("none"); else setError(errorText(e)); });
  useEffect(() => { void load(); }, []);
  if (error) return <main><Err text={error} /></main>;
  if (!me) return <main><p className="muted">불러오는 중…</p></main>;
  if (me === "none") return <Login done={() => void load()} />;
  if (me.member.mustChange) return <ForceChange done={() => void load()} />;
  return <Workspace me={me} onLogout={() => { setMe("none"); }} />;
}

function Workspace({ me: first, onLogout }: { me: Me; onLogout: () => void }) {
  const [page, setPage] = useState<string>(location.hash.slice(1) || "home");
  const me = { data: first };
  const home = useLoad<any>(() => get("/home"), [page]);
  const go = (p: string) => { setPage(p); location.hash = p; };
  const badge: Record<string, number> = { mail: home.data?.unreadMail ?? 0, approvals: home.data?.pendingApprovals ?? 0 };
  return <div className="shell">
    <nav><h1>INAZINA Work</h1>{pages.map(([key, label]) => <button key={key} className={page === key ? "on" : ""} onClick={() => go(key)}>{label}{badge[key] ? <span className="badge new">{badge[key]}</span> : null}</button>)}<div className="me">{me.data.member.name}<br />{me.data.member.role === "admin" ? "관리자" : "멤버"}<br /><br /><Attempt className="ghost" action={async () => { await post("/auth/logout"); onLogout(); }}>로그아웃</Attempt></div></nav>
    <main key={page}>
      {page === "home" && <Home go={go} />}{page === "mail" && <Mail />}{page === "chat" && <Chat me={me.data} />}{page === "calendar" && <Calendar />}{page === "drive" && <Drive />}
      {page === "docs" && <Docs />}{page === "approvals" && <Approvals me={me.data} />}{page === "boards" && <Boards me={me.data} />}{page === "projects" && <Projects />}
      {page === "attendance" && <Attendance me={me.data} />}{page === "contacts" && <Contacts />}{page === "org" && <Org me={me.data} onLeft={onLogout} />}
    </main></div>;
}
