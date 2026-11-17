import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { navigate } from "./nav";
import { daysLeft, formatAmount, type Payout } from "./notificationFormat";
import "./notifications.css";

const errorText = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;

const dateText = (epoch: number) => new Date(epoch * 1000).toLocaleDateString("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit" });

/** Where a notification's "선택하기" leads: exchange now or take a temporary account; later, withdraw it. */
export function PayoutPage({ payoutId }: { payoutId: number }) {
  const [payout, setPayout] = useState<Payout | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  const load = useCallback(async () => {
    try {
      const result = await api<{ payouts: Payout[] }>("/api/me/payouts");
      setPayout(result.payouts.find((item) => item.payout_id === payoutId) ?? null);
    } catch (cause) {
      setError(errorText(cause, "지급 내역을 불러오지 못했습니다."));
      setPayout(null);
    }
  }, [payoutId]);
  useEffect(() => { void load(); }, [load]);

  const act = async (action: "convert" | "temp" | "withdraw", message: (result: Payout) => string) => {
    setBusy(true);
    setError("");
    try {
      const result = await api<{ payout: Payout }>(`/api/me/payouts/${action}`, { method: "POST", body: JSON.stringify({ payoutId }) });
      setDone(message(result.payout));
      await load();
    } catch (cause) {
      setError(errorText(cause, "처리하지 못했습니다. 잠시 후 다시 시도해주세요."));
    } finally {
      setBusy(false);
    }
  };

  if (payout === undefined) return <section className="payout-page"><p className="payout-meta">불러오는 중…</p></section>;
  if (payout === null) return <section className="payout-page">
    <h1>지급 내역을 찾을 수 없습니다</h1>
    <p className="payout-meta">{done || error || "이미 처리되었거나 기한이 지난 지급입니다."}</p>
    <button type="button" className="payout-pill" onClick={() => navigate("/account")}>내 계정으로</button>
  </section>;

  const fee = `수수료: ${formatAmount(payout.withdraw_fee)} ${payout.currency}`;
  return <section className="payout-page" aria-labelledby="payout-title">
    <span className="payout-state">{payout.status === "pending" ? "선택 대기" : "임시계좌 보관 중"}</span>
    {payout.status === "pending" ? <>
      <h1 id="payout-title">지급 방법을 선택해주세요</h1>
      <p className="payout-meta">{payout.currency} 통장이 없어 아래 금액을 바로 받을 수 없습니다.</p>
      <div className="payout-amount">{formatAmount(payout.amount)} {payout.currency}</div>
      <p className="payout-meta">{dateText(payout.expires_epoch)}까지 선택하지 않으면 지급한 곳으로 귀속됩니다. (남은 기간 {daysLeft(payout.expires_epoch)}일)</p>
      <div className="payout-choices">
        <article className="payout-choice">
          <h2>1. 지금 환전해서 받기</h2>
          <ul>
            <li>실시간 환율로 내 국적 통화로 바꿔 지갑에 바로 입금합니다</li>
            <li>환율은 선택하는 순간 확정됩니다</li>
          </ul>
          <button type="button" className="payout-cta" disabled={busy}
            onClick={() => void act("convert", () => "환전해서 지갑에 입금했습니다.")}>환전해서 받기</button>
        </article>
        <article className="payout-choice">
          <h2>2. 임시계좌로 받기</h2>
          <ul>
            <li>임시계좌가 발급되고 30일 동안 보관됩니다</li>
            <li>{payout.currency} 통장을 개설하면 그 통장으로 인출할 수 있습니다</li>
            <li>인출할 때 보관 수수료 3%가 차감됩니다 ({fee})</li>
            <li>송금·입금 등 다른 금융 활동은 할 수 없습니다</li>
          </ul>
          <button type="button" className="payout-cta alt" disabled={busy}
            onClick={() => void act("temp", (result) => `임시계좌 ${result.temp_number}가 발급되었습니다.`)}>임시계좌 발급받기</button>
        </article>
      </div>
    </> : <>
      <h1 id="payout-title">임시계좌 {payout.temp_number}</h1>
      <div className="payout-amount">{formatAmount(payout.amount)} {payout.currency}</div>
      <div className="payout-facts">
        <div><span>보관 기한</span><b>{dateText(payout.expires_epoch)} (D-{daysLeft(payout.expires_epoch)})</b></div>
        <div><span>보관 수수료 3%</span><b>−{formatAmount(payout.withdraw_fee)} {payout.currency}</b></div>
        <div><span>인출 받는 금액</span><b>{formatAmount(payout.withdraw_net)} {payout.currency}</b></div>
      </div>
      <p className="payout-note">인출하면 임시계좌는 사라지고, 수수료({formatAmount(payout.withdraw_fee)} {payout.currency})가 차감된 금액이 내 {payout.currency} 통장으로 입금됩니다. 인출하려면 {payout.currency} 통장이 있어야 합니다.</p>
      <div className="payout-actions">
        <button type="button" className="payout-pill" onClick={() => navigate("/bank")}>{payout.currency} 통장 개설하기</button>
        <button type="button" className="payout-pill primary" disabled={busy}
          onClick={() => void act("withdraw", (result) => `${formatAmount(result.withdraw_net)} ${result.currency}를 인출했습니다.`)}>내 {payout.currency} 통장으로 인출</button>
      </div>
      <p className="payout-meta">기한까지 인출하지 않으면 지급한 곳으로 귀속됩니다.</p>
    </>}
    {done && <p className="payout-note" role="status">{done}</p>}
    {error && <p className="payout-error" role="alert">{error}</p>}
  </section>;
}
