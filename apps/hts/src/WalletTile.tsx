import { totalInHome } from "./exactTotal";
import { minorMoney, type Money } from "../../shared/money";
import type { ApiUser } from "../../shared/api";
import type { FormEvent } from "react";
import { RefreshRateButton } from "./RefreshRateButton";
import type { CreditReport } from "./CreditPage";
import { KYC_RISK, KYC_STATUS, RATE_BENCHMARK_LABELS, compactMoney, date, money, type Lock, type TaxResidency } from "./appShared";
import type { Portfolio } from "./types";
import type { useAccountSettings } from "./useAccountSettings";

type Props = {
  lock: Lock;
  locked: string;
  me: ApiUser;
  portfolio: Portfolio | null;
  credit: CreditReport | null;
  homeCurrency: string;
  inHome: (nwx: Money) => Money;
  ratesBusy: boolean;
  onRefreshRates: () => void;
  taxResidency: TaxResidency | null;
  taxResidencyValue: string;
  onTaxResidencyValue: (value: string) => void;
  onSaveTaxResidency: (event: FormEvent<HTMLFormElement>) => void;
  settings: ReturnType<typeof useAccountSettings>;
};

/** Wallet summary, shortcuts, balances by kind, account details and the credit score. */
export function WalletTile({ lock, locked, me, portfolio, credit, homeCurrency, inHome, ratesBusy, onRefreshRates }: Props) {
  // 세무거주지는 국적과 동일하게 자동 적용하므로 이 화면에서는 바꾸지 않습니다(서버 기능은 그대로).
  return <section className={`wallet-tile${locked}`} aria-label="내 지갑" {...lock("로그인하고 내 지갑 보기")}>
    <section className="wallet-summary">
      <span>내 순자산 · {portfolio?.nationality ?? me.nationality} · {homeCurrency}<RefreshRateButton busy={ratesBusy} onRefresh={onRefreshRates} /></span>
      <strong>{compactMoney(totalInHome(portfolio?.net_worth ?? 0, portfolio, inHome))} <small>{homeCurrency}</small></strong>
      <p>총자산 {compactMoney(totalInHome(portfolio?.grand_total ?? 0, portfolio, inHome))} · 현금 {compactMoney(portfolio?.cash ?? me.cash)} {homeCurrency}</p>
      {portfolio?.kyc_profile && <p>KYC · {KYC_STATUS[portfolio.kyc_profile.status]} · {KYC_RISK[portfolio.kyc_profile.risk_tier]}{portfolio.kyc_profile.expires_epoch ? ` · 만료 ${date(portfolio.kyc_profile.expires_epoch)}` : ""}</p>}
      {/* 세무거주지는 지금 국적과 동일하게 자동 적용합니다. 직접 바꾸는 화면은 숨겼습니다(서버 기능은 그대로). */}
    </section>
    <div className="wallet-actions">
      <a href={`/commands/${encodeURIComponent("송금")}`}>송금</a>
      <a href={`/commands/${encodeURIComponent("계좌개설")}`}>계좌개설</a>
      <a href={`/commands/${encodeURIComponent("대출신청")}`}>대출</a>
      <a href="/portfolio" target="_blank" rel="noreferrer">내 주식</a>
    </div>
    <section className="data-panel wallet-panel">
      <div className="wallet-row"><span>현금</span><b>{compactMoney(portfolio?.cash || 0)} {homeCurrency}</b></div>
      {(portfolio?.wallets ?? []).filter((wallet) => wallet.is_foreign && wallet.balance_units).map((wallet) => <div className="wallet-row" key={wallet.currency_code}><span>외화 · {wallet.currency_code}</span><b>{compactMoney(minorMoney(wallet.balance_units, 6))} {wallet.currency_code}</b></div>)}
      <div className="wallet-row"><span>통장</span><b>{compactMoney(portfolio?.account_total || 0)} {homeCurrency}</b></div>
      <div className="wallet-row"><span>투자 평가액</span><b>{compactMoney(inHome(portfolio?.stock_total || 0))} {homeCurrency}</b></div>
      <div className="wallet-row"><span>매수 예치금</span><b>{compactMoney(inHome(portfolio?.order_reserved_total || 0))} {homeCurrency}</b></div>
      <div className="wallet-row"><span>대출</span><b>{compactMoney(inHome(portfolio?.loan_total || 0))} {homeCurrency}</b></div>
      <div className="wallet-row net-worth"><span>순자산</span><b>{compactMoney(totalInHome(portfolio?.net_worth || 0, portfolio, inHome))} {homeCurrency}</b></div>
      {(portfolio?.accounts ?? []).filter((account) => account.resolution_status === "active").map((account) => <div className="wallet-row account-detail" key={account.number}><span>{account.product_name} · {account.currency_code}<small>{account.number}</small>{account.rate_type === "floating" && account.next_repricing_epoch != null && <small>현재 연 {(account.contract_rate == null ? undefined : Number(account.contract_rate).toFixed(4)) ?? "—"}% · 지표 관측 기준일 {date(Math.max(0, account.next_repricing_epoch - account.repricing_lag_seconds))} · 재산정 예정 {date(account.next_repricing_epoch)} · {RATE_BENCHMARK_LABELS[account.benchmark_key || ""] || account.benchmark_key || "기준금리"} {account.benchmark_tenor_days}일 · 새 금리는 재산정 시 확정</small>}{account.rate_resets.map((reset, index) => <small key={`${reset.applied_epoch}-${index}`}>이전 재산정 {date(reset.applied_epoch)} · {RATE_BENCHMARK_LABELS[reset.benchmark_key] || reset.benchmark_key} {reset.benchmark_tenor_days}일 · 연 {reset.old_rate}% → {reset.new_rate}%</small>)}</span><b>{money(account.balance, account.decimal_places)} {account.currency_code}</b></div>)}
    </section>
    {credit && <section className="data-panel wallet-credit" aria-label="신용점수">
      <div className="panel-title"><h2>신용점수<a className="credit-link" href="/credit" aria-label="신용점수 자세히 보기">바로가기 ↗</a></h2><span>기준 {credit.anchor}점</span></div>
      <div className="credit-score"><b>{credit.score}</b><span>/ 1000</span></div>
      <div className="credit-meter" role="meter" aria-label="신용점수" aria-valuemin={0} aria-valuemax={1000} aria-valuenow={credit.score}><i style={{ width: `${Math.min(100, Math.max(0, credit.score / 10))}%` }} /><em style={{ left: `${Math.min(100, Math.max(0, credit.anchor / 10))}%` }} title={`기준점수 ${credit.anchor}점`} /></div>
      <p className="credit-note">기준점수에 요인을 더한 점수입니다. 연체 영향은 시간이 지나며 줄고, 정상 납부·완납은 점수를 올립니다. 실제 신용평가사 점수가 아닙니다.</p>
    </section>}

  </section>;
}
