import { moneyCompare, type Money } from "../../../shared/money";
import { useEffect, useState } from "react";
import { api } from "../../../shared/api";
import { VALUES } from "../commandKit";
import { won, type ServiceProps } from "./ServiceKit";

type Event = { kind: "transfer_out" | "transfer_in" | "basic_income" | "exchange"; epoch: number; currency: string; amount: Money; counterpart: string; counterpartUserId?: string; account: string | null };
type Tab = "money" | "orders";
const KIND: Record<Event["kind"], string> = { transfer_out: "보냄", transfer_in: "받음", basic_income: "기본소득", exchange: "환전" };
const when = (epoch: number) => new Date(epoch * 1000).toLocaleString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

/** 거래내역: money in and out, and orders — a statement instead of a command result. */
export function HistoryPage({ orders }: ServiceProps) {
  const [tab, setTab] = useState<Tab>("money");
  const [events, setEvents] = useState<Event[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { api<{ events: Event[] }>("/api/me/history").then((result) => setEvents(result.events)).catch((cause) => setError(cause instanceof Error ? cause.message : "거래내역을 불러오지 못했습니다.")); }, []);
  const income = (events ?? []).filter((item) => moneyCompare(item.amount, 0) > 0).length, spend = (events ?? []).length - income;

  return <div className="svc-history">
    <div className="svc-tabs" role="tablist" aria-label="거래내역">
      <button type="button" role="tab" aria-selected={tab === "money"} className={tab === "money" ? "active" : ""} onClick={() => setTab("money")}>입출금 {events ? events.length : ""}</button>
      <button type="button" role="tab" aria-selected={tab === "orders"} className={tab === "orders" ? "active" : ""} onClick={() => setTab("orders")}>주문 {orders.length}</button>
    </div>
    {tab === "money" && <section className="svc-card">
      {events && <p className="svc-hint">최근 {events.length}건 · 입금 {income}건 · 출금 {spend}건</p>}
      {error && <p className="account-error" role="alert">{error}</p>}
      <div className="svc-statement">{(events ?? []).map((item, index) => <div key={`${item.epoch}-${index}`} className="svc-statement-row">
        <time>{when(item.epoch)}</time>
        <span className={`svc-kind ${item.kind}`}>{KIND[item.kind]}</span>
        <span className="svc-who">{item.counterpart}{item.counterpartUserId && <small>사용자 ID · {item.counterpartUserId}</small>}{item.account && <small>{item.account}</small>}</span>
        <b className={moneyCompare(item.amount, 0) > 0 ? "plus" : "minus"}>{moneyCompare(item.amount, 0) > 0 ? "+" : ""}{won(item.amount, 2)} <small>{item.currency}</small></b>
      </div>)}</div>
      {events && !events.length && <p className="svc-empty">아직 입출금 내역이 없습니다.</p>}
      {!events && !error && <p className="svc-hint">불러오는 중…</p>}
    </section>}
    {tab === "orders" && <section className="svc-card">
      <div className="svc-statement">{orders.map((order) => <div key={order.order_id} className="svc-statement-row">
        <time>#{order.order_id}</time>
        <span className={`svc-kind ${order.side === "buy" ? "transfer_in" : "transfer_out"}`}>{order.side === "buy" ? "매수" : "매도"}</span>
        <span className="svc-who">{order.asset_code}<small>{VALUES[order.status] ?? order.status} · 잔량 {won(order.remaining_quantity, 3)}</small></span>
        <b>{won(order.limit_price, 2)}</b>
      </div>)}</div>
      {!orders.length && <p className="svc-empty">최근 주문이 없습니다.</p>}
    </section>}
  </div>;
}
