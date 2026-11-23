import { money } from "./appShared";
import type { Book } from "./appShared";
import type { Money } from "../../shared/money";

type Props = { book: Book; current: Money; onPickPrice: (price: string) => void };

/** Asks above, bids below, with the latest price between; a click puts that price into the ticket. */
export function OrderBookPanel({ book, current, onPickPrice }: Props) {
  return <section className="data-panel orderbook-panel"><div className="panel-title"><h2>호가</h2><span>수량 · 가격</span></div><div className="book-head"><span>수량</span><span>가격</span></div>
    <div className="book-side asks">{[...book.asks].reverse().map((item, index) => <button type="button" className="book-row" key={`${item.limit_price}-${index}`} title="이 가격으로 주문" onClick={() => onPickPrice(String(item.limit_price))}><span>{money(item.quantity, 3)}</span><b>{money(item.limit_price, 2)}</b></button>)}</div>
    <div className="book-current"><span>현재가</span><strong>{money(current)}</strong></div>
    <div className="book-side bids">{book.bids.map((item, index) => <button type="button" className="book-row" key={`${item.limit_price}-${index}`} title="이 가격으로 주문" onClick={() => onPickPrice(String(item.limit_price))}><span>{money(item.quantity, 3)}</span><b>{money(item.limit_price, 2)}</b></button>)}</div>
  </section>;
}
