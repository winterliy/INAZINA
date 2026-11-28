import { useState } from "react";
import { get } from "./api.ts";
import { Err, useLoad } from "./ui.tsx";
import { Calendar, Chat, Contacts, Home, Mail, type Me } from "./pages1.tsx";
import { Approvals, Boards, Docs, Drive } from "./pages2.tsx";
import { Attendance, Org, Projects } from "./pages3.tsx";

const pages = [["home", "홈"], ["mail", "내부 메일"], ["chat", "메신저"], ["calendar", "캘린더"], ["drive", "드라이브"], ["docs", "문서"], ["approvals", "전자결재"], ["boards", "게시판/공지"], ["projects", "프로젝트/업무"], ["attendance", "근태/휴가"], ["contacts", "연락처"], ["org", "계정/조직"]] as const;

export function App() {
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
