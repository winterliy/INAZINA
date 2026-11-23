import { ORDER_TYPES, money, type Lock } from "./appShared";
import type { Instrument } from "./types";
import type { Money } from "../../shared/money";

type Props = {
  lock: Lock;
  locked: string;
  selected: Instrument | null;
  homeCurrency: string;
  orderType: "limit" | "market" | "current";
  onOrderType: (type: "limit" | "market" | "current") => void;
  side: "buy" | "sell";
  onSide: (side: "buy" | "sell") => void;
  price: string;
  onPrice: (price: string) => void;
  quantity: string;
  onQuantity: (quantity: string) => void;
  orderPrice: string;
  marketEnough: boolean;
  orderAmount: Money;
  orderReady: boolean;
  busy: boolean;
  onReview: () => void;
};

/** The order ticket: type, side, price, quantity and the review button. */
export function OrderEntry({ lock, locked, selected, homeCurrency, orderType, onOrderType, side, onSide, price, onPrice, quantity, onQuantity, orderPrice, marketEnough, orderAmount, orderReady, busy, onReview }: Props) {
  return <section className={`data-panel order-entry${locked}`} {...lock("로그인하고 주문하기")}><div className="panel-title"><h2>주문 입력</h2><span>{ORDER_TYPES.find(([id]) => id === orderType)![1]}</span></div>
      <div className="order-types" role="group" aria-label="주문 유형">{ORDER_TYPES.map(([id, label]) => <button key={id} type="button" aria-pressed={orderType === id} className={orderType === id ? "active" : ""} onClick={() => onOrderType(id)}>{label}</button>)}</div>
      <div className="order-sides"><button className={side === "buy" ? "buy active" : "buy"} onClick={() => onSide("buy")}>매수</button><button className={side === "sell" ? "sell active" : "sell"} onClick={() => onSide("sell")}>매도</button></div>
      {orderType === "limit" && <label>지정가 ({selected?.quote_currency || homeCurrency})<input inputMode="numeric" value={price} onChange={(event) => onPrice(event.target.value.replace(/[^\d.]/g, ""))} /></label>}
      {orderType === "current" && <label>현재가 ({selected?.quote_currency || homeCurrency})<input value={orderPrice ? money(orderPrice, 2) : "—"} readOnly aria-readonly="true" /></label>}
      {orderType === "market" && <label>예상 체결가 ({selected?.quote_currency || homeCurrency})<input value={orderPrice ? money(orderPrice, 2) : "—"} readOnly aria-readonly="true" /></label>}
      {orderType === "market" && Number(quantity) > 0 && !marketEnough && <p className="risk-note">상대 호가 수량이 부족해 시장가로 모두 체결할 수 없습니다.</p>}
      <label>주문 수량<input inputMode="numeric" value={quantity} onChange={(event) => onQuantity(event.target.value.replace(/\D/g, ""))} /></label>
      <div className="form-total"><span>주문 금액</span><b>{money(orderAmount, 2)} {selected?.quote_currency || homeCurrency}</b></div>
      <button className={side === "buy" ? "submit buy-submit" : "submit sell-submit"} disabled={!orderReady || busy} onClick={() => onReview()}>{side === "buy" ? "매수 주문 검토" : "매도 주문 검토"}</button>
      <p className="risk-note">{orderType === "market" ? "시장가는 상대 호가를 순서대로 체결하며, 실제 체결가는 제출 시점 호가로 정해집니다. " : orderType === "current" ? "현재가는 마지막 체결가로 지정가 주문을 냅니다. " : ""}호가를 누르면 그 가격이 지정가로 들어갑니다. 펀드 가입·환매와 소수점 수량은 명령어의 매수·매도를 사용하세요.</p>
    </section>;
}
