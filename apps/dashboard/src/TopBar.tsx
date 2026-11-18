import { useEffect, useRef, useState } from "react";
import { appUrl, type ApiUser } from "../../shared/api";
import { NAV_GROUPS, groupOf } from "./nav";

type Props = {
  user: ApiUser;
  page: string;
  pages: { id: string; label: string }[];
  /** Pending counts keyed by page id (unread inquiries, naturalization queue). */
  badges: Record<string, number>;
  health: string;
  connected: boolean;
  busy: boolean;
  onPage: (page: string) => void;
  onRefresh: () => void;
  onLogout: () => void;
};

/** Two bars: groups with the connection/account controls on top, the open group's pages below. */
export function TopBar({ user, page, pages, badges, health, connected, busy, onPage, onRefresh, onLogout }: Props) {
  const known = new Set(pages.map((item) => item.id));
  const labels = new Map(pages.map((item) => [item.id, item.label]));
  const groups = NAV_GROUPS.map((group) => ({ ...group, pages: group.pages.filter((id) => known.has(id)) })).filter((group) => group.pages.length);
  const active = groupOf(page) ?? (page === "account" ? undefined : groups[0]);
  const lastPage = useRef<Record<string, string>>({});
  if (active) lastPage.current[active.id] = page;
  const [menuOpen, setMenuOpen] = useState(false);
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: Event) => { if (!menu.current?.contains(event.target as Node)) setMenuOpen(false); };
    const esc = (event: KeyboardEvent) => { if (event.key === "Escape") setMenuOpen(false); };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", esc); };
  }, [menuOpen]);
  const total = (ids: string[]) => ids.reduce((sum, id) => sum + (badges[id] ?? 0), 0);
  const subs = active ? groups.find((group) => group.id === active.id) : undefined;

  return <>
    <header className="gnav">
      <div className="gnav-in">
        <div className="brand"><strong>INAZINA</strong><small>운영 Dashboard</small></div>
        <nav className="gnav-groups" aria-label="메뉴 그룹">
          {groups.map((group) => {
            const count = total(group.pages);
            return <button key={group.id} type="button" className="gnav-group" aria-current={active?.id === group.id ? "true" : undefined}
              onClick={() => onPage(lastPage.current[group.id] ?? group.pages[0])}>
              {group.label}
              {count > 0 && <span className="nav-badge" aria-label={`대기 ${count}건`}>{count}</span>}
            </button>;
          })}
        </nav>
        <div className="gnav-meta">
          <span className="status" title={`127.0.0.1 · ${health}`}><span className={connected ? "dot ok" : "dot"} />{health}</span>
          <button type="button" className="gnav-icon" onClick={onRefresh} disabled={busy} aria-label="새로고침" title="새로고침">↻</button>
          <div className="gnav-menu" ref={menu}>
            <button type="button" className="gnav-account" aria-haspopup="menu" aria-expanded={menuOpen} aria-current={page === "account" ? "page" : undefined} onClick={() => setMenuOpen((open) => !open)}>
              <span aria-hidden="true">{user.nickname.slice(0, 1)}</span>{user.nickname} ▾
            </button>
            {menuOpen && <div className="gnav-popover" role="menu">
              <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); onPage("account"); }}>내 계정</button>
              <a role="menuitem" href={appUrl("hts")}>나루 열기</a>
              <hr />
              <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); onLogout(); }}>{user.delegatedBy ? "관리자로 돌아가기" : "로그아웃"}</button>
            </div>}
          </div>
        </div>
      </div>
    </header>
    <nav className="subnav" aria-label="관리 메뉴">
      <div className="subnav-in">
        {subs?.pages.map((id) => <button key={id} type="button" className="subnav-item" aria-current={page === id ? "page" : undefined} onClick={() => onPage(id)}>
          {labels.get(id)}
          {(badges[id] ?? 0) > 0 && <span className="nav-badge" aria-label={`대기 ${badges[id]}건`}>{badges[id]}</span>}
        </button>)}
        {!subs && <span className="subnav-note">내 계정</span>}
        {subs?.external && <a className="subnav-ext" href={appUrl("dashboard", `/${subs.external.hash}`)} target="_blank" rel="noopener noreferrer">{subs.external.label} ↗</a>}
      </div>
    </nav>
  </>;
}
