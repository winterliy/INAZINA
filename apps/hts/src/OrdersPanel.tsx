import { OPEN } from "./commandKit";
import { tabListKeys } from "./tabKeys";
import { STATUS, money, type Lock } from "./appShared";
import type { Order } from "./types";

type Props = {
  lock: Lock;
  locked: string;
  orders: Order[];
  orderFilter: "open" | "all";
  onOrderFilter: (filter: "open" | "all") => void;
  busy: boolean;
  onChoose: (code: string) => void;
  onCancel: (order: Order) => void;
};

/** Open / all orders table with a cancel button on each open order. */
export function OrdersPanel({ lock, locked, orders, orderFilter, onOrderFilter, busy, onChoose, onCancel }: Props) {
  const visibleOrders = orders.filter((order) => orderFilter === "all" || OPEN.has(order.status));
  const openCount = orders.filter((order) => OPEN.has(order.status)).length;
  return <section className={`data-panel orders-panel${locked}`} {...lock("로그인하고 주문 보기")}>
    <div className="panel-title"><h2>주문 · 체결</h2><div className="order-tabs" role="tablist" aria-label="주문 필터" onKeyDown={tabListKeys}><button type="button" role="tab" id="order-tab-open" aria-controls="orders-panel" tabIndex={orderFilter === "open" ? 0 : -1} aria-selected={orderFilter === "open"} className={orderFilter === "open" ? "selected" : ""} onClick={() => onOrderFilter("open")}>미체결 {openCount}</button><button type="button" role="tab" id="order-tab-all" aria-controls="orders-panel" tabIndex={orderFilter === "all" ? 0 : -1} aria-selected={orderFilter === "all"} className={orderFilter === "all" ? "selected" : ""} onClick={() => onOrderFilter("all")}>전체</button></div></div>
    <p className="orders-hint">미체결·부분 체결·예약 주문은 오른쪽 <b>취소</b> 버튼으로 바로 취소할 수 있어요. 남은 예치금과 수량은 즉시 돌려받습니다.</p>
    <div className="table-scroll" id="orders-panel" role="tabpanel" aria-labelledby={`order-tab-${orderFilter}`}><table><thead><tr><th>주문</th><th>종목</th><th>구분</th><th className="r">가격</th><th className="r">수량</th><th className="r">체결</th><th className="r">잔량</th><th>상태</th><th /></tr></thead><tbody>{visibleOrders.map((order) => <tr key={order.order_id}>
      <td className="mono">#{order.order_id}</td><td><button className="link" onClick={() => onChoose(order.asset_code)}>{order.asset_code}</button></td>
      <td className={order.side === "buy" ? "buy-text" : "sell-text"}>{order.side === "buy" ? "매수" : "매도"}</td><td className="r">{money(order.limit_price, 2)}</td><td className="r">{money(order.quantity, 3)}</td><td className="r">{money(order.filled_quantity, 3)}</td><td className="r">{money(order.remaining_quantity, 3)}</td>
      <td><span className={OPEN.has(order.status) ? "status open" : "status"}>{STATUS[order.status] ?? order.status}</span></td>
      <td>{OPEN.has(order.status) && <button className="cancel-button" disabled={busy} onClick={() => void onCancel(order)}>취소</button>}</td>
    </tr>)}</tbody></table>{!visibleOrders.length && <p className="empty">{orderFilter === "open" ? "미체결 주문이 없습니다." : "최근 주문이 없습니다."}</p>}</div>
  </section>;
}
