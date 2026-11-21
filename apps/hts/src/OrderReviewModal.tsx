import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { moneyCompare, validMoney, type Money } from "../../shared/money";

type Props = {
  side: "buy" | "sell";
  kind: "limit" | "market" | "current";
  kindLabel: string;
  priceLabel: string;
  /** Live price: it follows the market while the modal is open (limit prices stay as typed). */
  price: Money;
  /** When `price` was read from the server (ms), for the "N초 전" chip. */
  priceAt: number | null;
  currency: string;
  nameLine: ReactNode;
  quantity: string;
  amount: string;
  busy: boolean;
  format: (value: Money, digits: number) => string;
  onClose: () => void;
  onConfirm: () => void;
};

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const differs = (a: Money, b: Money) => validMoney(String(a), true) && validMoney(String(b), true) ? moneyCompare(a, b) !== 0 : a !== b;

/** The order confirmation dialog: focus starts on 돌아가기, Esc or a backdrop click closes it, Tab stays inside. */
export function OrderReviewModal({ side, kind, kindLabel, priceLabel, price, priceAt, currency, nameLine, quantity, amount, busy, format, onClose, onConfirm }: Props) {
  const dialog = useRef<HTMLElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const pressedOnBackdrop = useRef(false);
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  // Initial focus, scroll lock and focus restore.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    backRef.current?.focus();
    return () => { document.body.style.overflow = overflow; previous?.focus?.(); };
  }, []);

  // A live price that moves while the dialog is open is applied and announced.
  const last = useRef(price);
  const [changed, setChanged] = useState<{ from: Money; to: Money } | null>(null);
  useEffect(() => {
    if (kind !== "limit" && differs(last.current, price)) setChanged({ from: last.current, to: price });
    last.current = price;
  }, [price, kind]);

  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const age = priceAt === null ? null : Math.max(0, Math.floor((now - priceAt) / 1000));

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") { event.stopPropagation(); if (!busyRef.current) closeRef.current(); return; }
    if (event.key !== "Tab") return;
    const items = Array.from(dialog.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (!items.length) { event.preventDefault(); return; }
    const first = items[0], end = items[items.length - 1];
    const inside = dialog.current?.contains(document.activeElement);
    if (event.shiftKey && (document.activeElement === first || !inside)) { event.preventDefault(); end.focus(); }
    else if (!event.shiftKey && (document.activeElement === end || !inside)) { event.preventDefault(); first.focus(); }
  }

  return <div className="modal-backdrop" onMouseDown={(event) => { pressedOnBackdrop.current = event.target === event.currentTarget; }} onClick={(event) => { if (pressedOnBackdrop.current && event.target === event.currentTarget && !busy) onClose(); pressedOnBackdrop.current = false; }}>
    <section ref={dialog} className="review-modal" role="dialog" aria-modal="true" aria-labelledby="review-title" onKeyDown={onKeyDown}>
      <p className="eyebrow">ORDER REVIEW</p>
      <h2 id="review-title">주문을 확인하세요</h2>
      <dl>
        <div><dt>종목</dt><dd>{nameLine}</dd></div>
        <div><dt>유형</dt><dd className={side === "buy" ? "buy-text" : "sell-text"}>{kindLabel} {side === "buy" ? "매수" : "매도"}</dd></div>
        <div><dt>{priceLabel}</dt><dd>{format(price || "0", 2)} {currency}{kind !== "limit" && age !== null && <span className="review-chip">{age}초 전 {kind === "current" ? "체결가" : "호가"}</span>}</dd></div>
        <div><dt>수량</dt><dd>{format(Number(quantity), 3)}</dd></div>
        <div><dt>예상 금액</dt><dd>{format(amount, 2)} {currency}</dd></div>
      </dl>
      <div className="review-live" role="status" aria-live="polite">{changed && <p className="review-changed">체결가가 {format(changed.from, 2)}에서 {format(changed.to, 2)}으로 바뀌어 갱신했습니다.</p>}</div>
      <div className="modal-actions">
        <button ref={backRef} type="button" disabled={busy} onClick={onClose}>돌아가기</button>
        <button type="button" className={side === "buy" ? "buy-confirm" : "sell-confirm"} disabled={busy} onClick={onConfirm}>{busy ? "제출 중" : "주문 확정"}</button>
      </div>
      <p className="review-hint">Esc 닫기</p>
    </section>
  </div>;
}
