import { useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import { ApiError, api } from "../../shared/api";
import { exactMoney, moneyMultiply, validMoney, type Money } from "../../shared/money";
import { errorText, requestKey, wholeNumber, type Book } from "./appShared";
import type { Instrument } from "./types";

type Options = {
  selected: Instrument | null;
  selectedCode: string;
  quote: { code: string; price: Money; at: number } | null;
  pricedAt: number;
  book: Book;
  submitLock: MutableRefObject<boolean>;
  setBusy: (busy: boolean) => void;
  setNotice: (text: string) => void;
  notify: (text: string) => void;
  /** Runs after an accepted order: refresh account, chart and instrument list. */
  afterOrder: () => Promise<unknown>;
  pollQuote: () => Promise<void>;
  onPlaced: () => void;
};

/** The order ticket: its fields, the price it is made at, and the submit with its double-click lock and idempotency key. */
export function useOrderSubmit({ selected, selectedCode, quote, pricedAt, book, submitLock, setBusy, setNotice, notify, afterOrder, pollQuote, onPlaced }: Options) {
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("1");
  const [price, setPrice] = useState("");
  const [orderType, setOrderType] = useState<"limit" | "market" | "current">("limit");
  const [review, setReview] = useState(false);
  // Idempotency key of the order being confirmed. The same order content keeps the key, so a retry after a timeout
  // is replayed by the server instead of placed twice; changed content (a new price) gets a new key.
  const orderRequest = useRef<{ signature: string; key: string } | null>(null);
  useEffect(() => { if (selected) setPrice(String(selected.price)); }, [selected?.code]);

  // The latest known price is whichever of the chart's last trade and the refreshed instrument list was read last.
  const quoteFresh = quote?.code === selectedCode && (!selected || quote.at >= pricedAt);
  const current = quoteFresh ? quote!.price : selected?.price ?? quote?.price ?? 0;
  const currentAt = quoteFresh ? quote!.at : selected ? pricedAt || null : null;
  // Market orders are priced by the server against the live book; this estimate uses the visible levels.
  const marketEstimate = (() => {
    const levels = (side === "buy" ? book.asks : book.bids) ?? [];
    let needed = Number(quantity) || 0;
    for (const level of levels) { needed -= Number(level.quantity); if (needed <= 0) return { price: String(level.limit_price), enough: true }; }
    return { price: levels.length ? String(levels[levels.length - 1].limit_price) : "", enough: false };
  })();
  const orderPrice = orderType === "market" ? marketEstimate.price : orderType === "current" ? String(current || "") : price;
  const orderReady = Boolean(selected) && validMoney(orderPrice, true) && wholeNumber(quantity) && Number(quantity) > 0 && (orderType !== "market" || marketEstimate.enough);
  const orderAmount = validMoney(orderPrice) && validMoney(quantity) ? moneyMultiply(orderPrice, quantity) : "0";

  async function submitOrder() {
    // A double click is stopped here, before the request; the clientOrderId covers a retry after a lost response.
    if (submitLock.current || !orderReady) return;
    submitLock.current = true;
    setBusy(true);
    try {
      const order = { assetCode: selectedCode, side, quantity: Number(quantity), orderType, limitPrice: exactMoney(orderPrice) };
      const signature = JSON.stringify(order);
      if (orderRequest.current?.signature !== signature) orderRequest.current = { signature, key: requestKey() };
      const result = await api<{ message: string }>("/api/market/orders", {
        method: "POST", body: JSON.stringify({ ...order, clientOrderId: orderRequest.current.key }),
      });
      orderRequest.current = null;
      setReview(false);
      notify(result.message);
      onPlaced();
      await afterOrder();
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "price_changed") {
        // The server holds the latest price and refused the old one. Take it now; the dialog announces the new price.
        await pollQuote();
      } else if ((cause as { status?: number })?.status !== 401) setNotice(errorText(cause, "주문을 제출하지 못했습니다."));
    } finally {
      submitLock.current = false;
      setBusy(false);
    }
  }

  return { side, setSide, quantity, setQuantity, price, setPrice, orderType, setOrderType, review, setReview, current, currentAt, marketEstimate, orderPrice, orderReady, orderAmount, submitOrder };
}
