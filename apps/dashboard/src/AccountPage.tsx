import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, appUrl, type ApiUser } from "../../shared/api";
import { Card, Field, date, errorText, number, type Ctx } from "./common";
import { IdentityCard } from "./IdentityCard";
import { AuditHeatmap } from "./ActivityTiles";
import { LoginHistoryCard, type LoginEvent } from "./LoginHistoryCard";

type Financial = { company: string; periods: string[]; report: { period: string; currency: string; places: number; summary: { assets: number; liabilities: number; equity: number }; income: Record<string, number>; checks: { balance_sheet: boolean } } };

/** The administrator's own work over the last week, from the audit log. */
function OwnHeatmap({ userId }: { userId: string }) {
  const [entries, setEntries] = useState<{ time: number }[]>([]);
  useEffect(() => {
    api<{ entries: { actorId: string; time: number }[] }>("/api/admin/audit").then((result) => setEntries(result.entries.filter((entry) => entry.actorId === userId))).catch(() => undefined);
  }, [userId]);
  return <AuditHeatmap entries={entries} />;
}

/** A company account's own statements, read-only. */
function OwnFinancial({ say }: { say: Ctx["say"] }) {
  const [financial, setFinancial] = useState<Financial | null>(null);
  const [period, setPeriod] = useState("");
  useEffect(() => {
    api<{ financial: Financial | null }>(`/api/me/company-financial${period ? `?period=${encodeURIComponent(period)}` : ""}`).then((result) => setFinancial(result.financial))
      .catch((cause) => say(errorText(cause, "재무제표를 불러오지 못했습니다."), true));
  }, [period]);
  if (!financial) return null;
  const { report } = financial;
  const money = (units: number) => `${number(units / 10 ** report.places)} ${report.currency}`;
  return <Card title={`${financial.company} 재무제표`} caption="원장에서 계산한 이 법인의 재무 상태입니다. 수정은 관리자가 합니다."
    actions={<select aria-label="기간" value={report.period} onChange={(event) => setPeriod(event.target.value)}>{Array.from(new Set([report.period, ...financial.periods])).map((item) => <option key={item}>{item}</option>)}</select>}>
    <div className="stats">
      <div><small>총자산</small><b>{money(report.summary.assets)}</b></div>
      <div><small>총부채</small><b>{money(report.summary.liabilities)}</b></div>
      <div><small>자기자본</small><b>{money(report.summary.equity)}</b></div>
      <div><small>당기순이익</small><b>{money(report.income.profit ?? 0)}</b></div>
    </div>
    <p className="hint">대차 {report.checks.balance_sheet ? "일치" : "불일치"}</p>
  </Card>;
}

const ROLE: Record<ApiUser["role"], string> = { admin: "관리자", user: "사용자", nation: "국가", company: "법인" };

/** The signed-in administrator: profile, login ID, execution identity and their own recent logins. */
export function AccountPage({ ctx, user, onLogout }: { ctx: Ctx; user: ApiUser; onLogout: () => void }) {
  const { busy, run, say } = ctx;
  const [loginId, setLoginId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [password, setPassword] = useState("");
  const admin = user.role === "admin";
  const [logins, setLogins] = useState<LoginEvent[]>([]);

  useEffect(() => {
    api<{ htsId: string | null }>("/api/hts/account").then((account) => { setLoginId(account.htsId); setInput(account.htsId ?? ""); })
      .catch((cause) => say(errorText(cause, "계정 정보를 불러오지 못했습니다."), true));
  }, []);

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void run(() => api<{ htsId: string }>("/api/hts/account", { method: "POST", body: JSON.stringify({ htsId: input, password }) })
      .catch((cause) => { throw cause instanceof Error && cause.message === "invalid_credentials" ? new Error("현재 비밀번호가 일치하지 않습니다.") : cause; }),
    "아이디를 저장하지 못했습니다.", (result) => { setLoginId(result.htsId); setPassword(""); say("로그인 아이디를 저장했습니다."); });
  }

  const recent = logins.slice(0, 10);
  const failed = recent.filter((item) => !item.success).length;

  return <>
    <section className="account-hero rise">
      <span className="account-avatar" aria-hidden="true">{user.nickname.slice(0, 1)}</span>
      <div className="account-hero-name"><h2>{user.nickname}</h2><p>{ROLE[user.role]} · {user.nationality} · <span className="mono">{user.userId}</span></p></div>
      <dl className="account-hero-facts">
        <div><dt>로그인 아이디</dt><dd>{loginId ?? "미설정"}</dd></div>
        {admin ? <>
          <div><dt>최근 로그인</dt><dd>{logins.length ? date(logins[0].time) : "—"}</dd></div>
          <div><dt>최근 10건 중 실패</dt><dd>{failed}건</dd></div>
        </> : <>
          <div><dt>지갑</dt><dd>{number(user.cash)} {user.currency ?? ""}</dd></div>
          <div><dt>신용점수</dt><dd>{user.creditScore}점</dd></div>
        </>}
      </dl>
      <button className="account-logout" type="button" onClick={onLogout}>{user.delegatedBy ? "관리자로 돌아가기" : "로그아웃"}</button>
    </section>
    <div className="account-grid">
      <Card title="로그인 아이디" caption="대시보드와 HTS 로그인에 쓰는 아이디입니다. 바꾸려면 현재 비밀번호를 입력하세요.">
        <form className="form-grid" onSubmit={save}>
          <Field label="아이디"><input autoComplete="username" minLength={3} maxLength={32} pattern="[A-Za-z0-9_.\-]{3,32}" required value={input} onChange={(event) => setInput(event.target.value)} placeholder="예: inazina_admin" /></Field>
          <Field label="현재 비밀번호"><input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} /></Field>
          <button className="btn primary" disabled={busy || !input || input === loginId}>{loginId ? "아이디 변경" : "아이디 설정"}</button>
        </form>
        <p className="hint">영문, 숫자, 점, 밑줄, 하이픈으로 3~32자</p>
      </Card>
      {admin ? <IdentityCard ctx={ctx} /> : <Card title="HTS" caption="주식 거래, 송금, 은행, 투자상품, 복권 · 게임을 HTS에서 이용합니다.">
        <div className="actions"><a className="btn primary" href={appUrl("hts")}>HTS 열기</a><a className="soft" href={appUrl("hts", "/transfer")}>송금</a><a className="soft" href={appUrl("hts", "/bank")}>은행</a><a className="soft" href={appUrl("hts", "/board")}>게시판</a></div>
      </Card>}
    </div>
    {user.role === "company" && <OwnFinancial say={say} />}
    {admin && <OwnHeatmap userId={user.userId} />}
    {admin && <LoginHistoryCard ctx={ctx} userId={user.userId} onLoaded={setLogins} />}
  </>;
}
