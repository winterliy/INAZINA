import type { ApiUser } from "../../shared/api";
import { NAV_GROUPS, groupOf } from "./nav";
import { Logo } from "../../shared/Logo";

type Props = {
  user: ApiUser;
  page: string;
  pages: { id: string }[];
  /** Pending counts keyed by page id (unread inquiries, naturalization queue). */
  badges: Record<string, number>;
  /** "checking" until the first health answer; "down" when the server or Python is unreachable. */
  connection: "checking" | "ok" | "down";
  onPage: (page: string) => void;
  onLogout: () => void;
};

/** One bar: the logo (back to the main page), the four groups, the connection dot, log out, and the account. */
export function TopBar({ user, page, pages, badges, connection, onPage, onLogout }: Props) {
  const known = new Set(pages.map((item) => item.id));
  const groups = NAV_GROUPS.map((group) => ({ ...group, pages: group.pages.filter((id) => known.has(id)) })).filter((group) => group.pages.length);
  const active = groupOf(page);
  const connectionLabel = connection === "ok" ? "연결됨" : connection === "checking" ? "연결 확인 중" : "연결 끊김";
  const connectionTitle = connection === "ok" ? "API · Python 연결됨" : connection === "checking" ? "연결 확인 중" : "연결 끊김 · API 또는 Python 서버에 닿지 않습니다";
  const total = (ids: string[]) => ids.reduce((sum, id) => sum + (badges[id] ?? 0), 0);
  return <header className="gnav">
    <div className="gnav-in">
      <a className="gnav-logo" href="#home" aria-label="inazina 메인 페이지" onClick={(event) => { event.preventDefault(); if (page === "home") window.scrollTo({ top: 0 }); onPage("home"); }}><Logo /></a>
      <nav className="gnav-groups" aria-label="메뉴 그룹">
        {groups.map((group) => {
          const count = total(group.pages);
          return <a key={group.id} href={`#${group.pages[0]}`} className="gnav-group" aria-current={active?.id === group.id ? "true" : undefined}
            onClick={(event) => { event.preventDefault(); onPage(active?.id === group.id ? page : group.pages[0]); }}>
            {group.label}{count > 0 && <span className="nav-badge" aria-label={`대기 ${count}건`}>{count}</span>}
          </a>;
        })}
      </nav>
      <div className="gnav-meta">
        <span className={connection === "ok" ? "live ok" : connection === "checking" ? "live pending" : "live"} role="img" aria-label={connectionLabel} title={connectionTitle} />
        <button type="button" className="gnav-text" onClick={onLogout}>{user.delegatedBy ? "관리자로 돌아가기" : "로그아웃"}</button>
        <a className="avatar" href="#account" aria-label="내 계정" aria-current={page === "account" ? "true" : undefined}
          onClick={(event) => { event.preventDefault(); onPage("account"); }}>{Array.from(user.nickname)[0] ?? ""}</a>
      </div>
    </div>
  </header>;
}
