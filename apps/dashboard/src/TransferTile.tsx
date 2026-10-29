import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { number, plain, type Ctx } from "./common";

type Identity = { identity: { account: string; name: string } };
type Citizen = { userId: string; nickname: string };

/** 송금: the national $송금 from the current $login identity to one recipient, as a from → to card. */
export function TransferTile({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [from, setFrom] = useState<Identity["identity"] | null>(null);
  const [recipient, setRecipient] = useState("");
  const [found, setFound] = useState<Citizen | null>(null);
  const [amount, setAmount] = useState("");

  const load = useCallback(async () => setFrom((await api<Identity>("/api/admin/companies/identity")).identity), []);
  useEffect(() => { load().catch(() => undefined); }, [load]);

  // Name the recipient once typing stops, so the card shows who receives the money.
  useEffect(() => {
    const id = recipient.trim();
    setFound(null);
    if (!id) return;
    const timer = window.setTimeout(() => {
      api<{ users: Citizen[] }>(`/api/admin/users?search=${encodeURIComponent(id)}`)
        .then((result) => setFound(result.users.find((user) => user.userId === id) ?? null)).catch(() => undefined);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [recipient]);

  function send() {
    const to = recipient.trim();
    const value = Number(amount);
    if (!to || !(value > 0)) { say("받는 사람과 0보다 큰 금액을 입력해주세요.", true); return; }
    if (!window.confirm(`${from?.name ?? "국고"} 명의로 ${found?.nickname ?? to}에게 ${number(value)}을(를) 보낼까요?\n\n$송금 ${to} ${amount}`)) return;
    void run(() => api<{ message: string; success: boolean }>("/api/admin/commands", { method: "POST", body: JSON.stringify({ national: true, command: "송금", values: [to, amount, ""] }) }),
      "송금하지 못했습니다.", (result) => { say(plain(result.message), !result.success); if (result.success) setAmount(""); });
  }

  return <section className="tile tile-dark transfer-tile" aria-label="송금">
    <p className="tile-label">송금 · 국가 명의 $송금</p>
    <div className="transfer-route">
      <div><small>보내는 곳</small><b>{from?.name ?? "…"}</b><small className="mono">{from?.account ?? ""}</small></div>
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" /></svg>
      <label><small>받는 곳</small><b>{found?.nickname ?? "받는 사람"}</b><input placeholder="ID 또는 계좌번호" value={recipient} onChange={(event) => setRecipient(event.target.value)} /></label>
    </div>
    <div className="transfer-amount">
      <input inputMode="decimal" aria-label="금액" placeholder="금액" value={amount} onChange={(event) => setAmount(event.target.value)} />
      <button className="btn primary" type="button" disabled={busy} onClick={send}>보내기</button>
    </div>
  </section>;
}
