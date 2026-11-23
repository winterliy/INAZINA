import { totalInHome } from "./exactTotal";
import { SERVICES } from "./services/ServiceKit";
import { AccountWithdrawal } from "./AccountWithdrawal";
import { PasswordSection } from "./PasswordSection";
import { PendingPayouts } from "./PendingPayouts";
import { RefreshRateButton } from "./RefreshRateButton";
import type { ApiUser } from "../../shared/api";
import type { Money } from "../../shared/money";
import type { CreditReport } from "./CreditPage";
import { KYC_RISK, KYC_STATUS, ROLE_LABEL, date, money } from "./appShared";
import type { Portfolio } from "./types";
import type { useAccountSettings } from "./useAccountSettings";

type Props = {
  me: ApiUser;
  portfolio: Portfolio | null;
  credit: CreditReport | null;
  homeCurrency: string;
  homeDigits: number;
  inHome: (nwx: Money) => Money;
  ratesBusy: boolean;
  onRefreshRates: () => void;
  onLogout: () => void;
  settings: ReturnType<typeof useAccountSettings>;
};

/** The /account page: profile card, login ID, password, KYC, ownership link and shortcuts. */
export function AccountPage({ me, portfolio, credit, homeCurrency, homeDigits, inHome, ratesBusy, onRefreshRates, onLogout, settings }: Props) {
  const { htsId, htsIdInput, setHtsIdInput, currentPassword, setCurrentPassword, settingsMessage, settingsError, settingsBusy } = settings;
  return <main className="account-page">
  <aside className="account-card" aria-label="프로필">
    <span className="account-avatar" aria-hidden="true">{me.nickname.slice(0, 1)}</span>
    <h1>{me.nickname}</h1>
    <p>{ROLE_LABEL[me.role] ?? me.role} · {me.nationality}</p>
    <dl>
      <div><dt>로그인 아이디</dt><dd>{htsId || "미설정"}</dd></div>
      <div><dt>지갑</dt><dd>{money(portfolio?.cash ?? me.cash, homeDigits)} {homeCurrency}</dd></div>
      <div><dt>순자산 <RefreshRateButton busy={ratesBusy} onRefresh={onRefreshRates} /></dt><dd>{money(totalInHome(portfolio?.net_worth ?? 0, portfolio, inHome), homeDigits)} {homeCurrency}</dd></div>
      {credit && <div><dt>신용점수</dt><dd>{credit.score}점</dd></div>}
    </dl>
    <button type="button" onClick={onLogout}>{me.delegatedBy ? "관리자로 돌아가기" : "로그아웃"}</button>
  </aside>
  <div className="account-sections">
    <PendingPayouts />
    <section className="account-section">
      <header><h2>로그인 아이디</h2><p>나루와 대시보드 로그인에 쓰는 아이디입니다. 영문, 숫자, 점, 밑줄, 하이픈으로 3~32자.</p></header>
      <form className="account-form" onSubmit={settings.saveHtsId}>
        <label>아이디<input autoComplete="username" minLength={3} maxLength={32} pattern="[A-Za-z0-9_.\-]{3,32}" value={htsIdInput} onChange={(event) => setHtsIdInput(event.target.value)} placeholder="예: inazina_user" required /></label>
        <label>현재 비밀번호<input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /></label>
        {settingsMessage && <p className="account-success" role="status">{settingsMessage}</p>}{settingsError && <p className="account-error" role="alert">{settingsError}</p>}
        <button className="account-save" disabled={settingsBusy || !htsIdInput}>{settingsBusy ? "저장 중…" : htsId ? "아이디 변경" : "아이디 설정"}</button>
      </form>
    </section>
    {!me.delegatedBy && <PasswordSection />}
    {portfolio?.kyc_profile && <section className="account-section">
      <header><h2>본인 확인 (KYC)</h2><p>국적별 확인 상태입니다.</p></header>
      <dl className="account-facts"><div><dt>상태</dt><dd>{KYC_STATUS[portfolio.kyc_profile.status]}</dd></div><div><dt>위험 등급</dt><dd>{KYC_RISK[portfolio.kyc_profile.risk_tier]}</dd></div><div><dt>만료</dt><dd>{portfolio.kyc_profile.expires_epoch ? date(portfolio.kyc_profile.expires_epoch) : "—"}</dd></div></dl>
    </section>}
    <section className="account-section">
      <header><h2>보유 기업 지배구조</h2><p>보유 주식의 의결권과 지배하는 자회사 관계를 넓은 화면에서 확인합니다.</p></header>
      <a className="account-save ownership-open" href="/ownership" target="_blank" rel="noreferrer">조직도 새 창에서 보기 ↗</a>
    </section>
    <section className="account-section">
      <header><h2>바로가기</h2></header>
      <nav className="account-links">{SERVICES.map((item) => <a key={item.path} href={item.path}><b>{item.label}</b><small>{item.description}</small></a>)}</nav>
    </section>
    <AccountWithdrawal user={me} />
  </div>
</main>;
}
