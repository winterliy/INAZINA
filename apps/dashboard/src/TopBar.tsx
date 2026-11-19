import type { ApiUser } from "../../shared/api";
import { NAV_GROUPS, groupOf } from "./nav";
import { Logo } from "./Logo";

type Props = {
  user: ApiUser;
  page: string;
  pages: { id: string }[];
  /** Pending counts keyed by page id (unread inquiries, naturalization queue). */
  badges: Record<string, number>;
  connected: boolean;
  onPage: (page: string) => void;
  onLogout: () => void;
};

/** One bar: the logo (back to the main page), the four groups, the connection dot, log out, and the account. */
export function TopBar({ user, page, pages, badges, connected, onPage, onLogout }: Props) {
  const known = new Set(pages.map((item) => item.id));
  const groups = NAV_GROUPS.map((group) => ({ ...group, pages: group.pages.filter((id) => known.has(id)) })).filter((group) => group.pages.length);
  const active = groupOf(page);
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
        <span className={connected ? "live ok" : "live"} role="img" aria-label={connected ? "연결됨" : "연결 확인 중"} title={connected ? "API · Python 연결됨" : "연결 확인 중"} />
        <button type="button" className="gnav-text" onClick={onLogout}>{user.delegatedBy ? "관리자로 돌아가기" : "로그아웃"}</button>
        <a className="avatar" href="#account" aria-label="내 계정" aria-current={page === "account" ? "true" : undefined}
          onClick={(event) => { event.preventDefault(); onPage("account"); }}>{user.nickname.slice(0, 1)}</a>
      </div>
    </div>
  </header>;
}
