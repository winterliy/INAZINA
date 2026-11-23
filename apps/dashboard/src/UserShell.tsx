import { Suspense, type ReactNode } from "react";
import { appUrl, type ApiUser } from "../../shared/api";
import { Logo } from "../../shared/Logo";
import type { Ctx } from "./common";
import { AccountPage } from "./lazyPages";

/** Users, country and company accounts get their own account dashboard, like the HTS. */
export function UserShell({ user, ctx, notice, onLogout }: { user: ApiUser; ctx: Ctx; notice: ReactNode; onLogout: () => void }) {
  return <div className="shell">
    <header className="gnav"><div className="gnav-in">
      <a className="gnav-logo" href="#account" aria-label="inazina 내 대시보드"><Logo /></a>
      <div className="gnav-meta"><a className="gnav-text" href={appUrl("hts")}>나루 열기 ↗</a><button type="button" className="gnav-text" onClick={onLogout}>{user.delegatedBy ? "관리자로 돌아가기" : "로그아웃"}</button></div>
    </div></header>
    <main className="content user-view">
      {user.delegatedBy && <div className="notice" role="status"><span>관리자 {user.delegatedByName ?? user.delegatedBy}님이 {user.nickname} 계정으로 로그인 중입니다.</span></div>}
      {notice}
      <Suspense fallback={<p className="loading">계정 화면을 불러오는 중…</p>}><AccountPage ctx={ctx} user={user} onLogout={onLogout} /></Suspense>
      <footer className="bot"><div className="bot-in"><span className="bot-status"><Logo className="bot-logo" /></span><span>© INAZINA. All rights reserved.</span></div></footer>
    </main>
  </div>;
}
