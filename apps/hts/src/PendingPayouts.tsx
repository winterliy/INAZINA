import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { daysLeft, formatAmount, type Payout } from "./notificationFormat";
import "./notifications.css";

/** Payments still waiting for the receiver, listed on the account page so they can be reached even when the
 *  notification that announced them has been deleted. Renders nothing when there are none. */
export function PendingPayouts() {
  const [payouts, setPayouts] = useState<Payout[]>([]);
  useEffect(() => {
    let alive = true;
    api<{ payouts: Payout[] }>("/api/me/payouts").then((result) => { if (alive) setPayouts(result.payouts); }).catch(() => undefined);
    return () => { alive = false; };
  }, []);
  if (payouts.length === 0) return null;
  return <section className="account-section pending-payouts" aria-labelledby="pending-payouts-title">
    <header>
      <h2 id="pending-payouts-title">지급 대기</h2>
      <p>선택하지 않거나 임시계좌에서 인출하지 않으면 기한이 지난 뒤 지급한 곳으로 귀속됩니다.</p>
    </header>
    <ul className="pending-payout-list">{payouts.map((item) => <li key={item.payout_id}>
      <div>
        <b>{formatAmount(item.amount)} {item.currency}</b>
        <small>{item.status === "pending" ? "지급 방법 선택 대기" : `임시계좌 ${item.temp_number} 보관 중`} · 남은 기간 {daysLeft(item.expires_epoch)}일</small>
      </div>
      <a className="pending-payout-link" href={`/payouts/${item.payout_id}`}>{item.status === "pending" ? "선택하기" : "인출하기"}</a>
    </li>)}</ul>
  </section>;
}
