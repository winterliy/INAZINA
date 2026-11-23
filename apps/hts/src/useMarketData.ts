import { useCallback, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import { api } from "../../shared/api";
import type { Money } from "../../shared/money";
import { DEFAULT_INDICATORS, INDICATOR_CATALOG, type Candle, type IndicatorCatalog, type IndicatorChoice, type IndicatorData, type Point } from "./PriceChart";
import { INDICATOR_KEY, type Book } from "./appShared";
import { usePolling } from "./usePolling";
import type { Instrument } from "./types";

type Options = {
  reportPollError: (key: string, cause: unknown, fallback: string) => void;
  lastPollError: MutableRefObject<Map<string, string>>;
};

/** The market side of the screen: instrument list (5s), chart and order book (5s), and the selected instrument's quote (1s). */
export function useMarketData({ reportPollError, lastPollError }: Options) {
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [pricedAt, setPricedAt] = useState(0);
  const [range, setRange] = useState<string>("1d");
  const [interval, setIntervalSeconds] = useState<number | null>(null);
  const [points, setPoints] = useState<Point[]>([]);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [candleInterval, setCandleInterval] = useState(600);
  const [book, setBook] = useState<Book>({ bids: [], asks: [] });
  const [quote, setQuote] = useState<{ code: string; price: Money; at: number } | null>(null);
  // Same 상단·하단 지표 as Discord /그래프; the choice is a per-viewer convenience kept in this browser.
  const [indicators, setIndicators] = useState<IndicatorChoice>(() => {
    try { const saved = JSON.parse(localStorage.getItem(INDICATOR_KEY) ?? "null"); if (Array.isArray(saved?.upper) && Array.isArray(saved?.lower)) return saved; } catch { /* default */ }
    return DEFAULT_INDICATORS;
  });
  const [indicatorData, setIndicatorData] = useState<IndicatorData>({ upper: {}, lower: {} });
  const [indicatorCatalog, setIndicatorCatalog] = useState<IndicatorCatalog>(INDICATOR_CATALOG);
  const indicatorQuery = `${indicators.upper.join(",")}|${indicators.lower.join(",")}`;
  const indicatorRef = useRef(indicators);
  const catalogLoaded = useRef(false);
  const marketSeq = useRef(0);
  indicatorRef.current = indicators;
  function chooseIndicators(next: IndicatorChoice) {
    setIndicators(next);
    try { localStorage.setItem(INDICATOR_KEY, JSON.stringify(next)); } catch { /* not remembered */ }
  }

  // The instrument list (prices) refreshes every 5s while the tab is visible; the first answer also picks the instrument.
  const loadInstruments = useCallback(async () => {
    try {
      const market = await api<{ instruments: Instrument[] }>("/api/market/instruments");
      lastPollError.current.delete("instruments");
      setInstruments(market.instruments);
      setPricedAt(Date.now());
      const linked = new URLSearchParams(location.search).get("code");
      setSelectedCode((current) => current || market.instruments.find((item) => item.code === linked)?.code || market.instruments[0]?.code || "");
    } catch (cause) { reportPollError("instruments", cause, "종목 목록을 불러오지 못했습니다."); }
  }, [reportPollError, lastPollError]);
  usePolling(loadInstruments, 5000, true);

  const refreshMarket = useCallback(async (code: string, timeframe: string, candleSeconds: number | null) => {
    if (!code) return;
    // Only the newest request may write: a slower answer for the previous instrument or range is dropped.
    const seq = ++marketSeq.current;
    try {
      const query = new URLSearchParams({ range: timeframe });
      if (candleSeconds) query.set("interval", String(candleSeconds));
      if (indicatorRef.current.upper.length) query.set("upper", indicatorRef.current.upper.join(","));
      if (indicatorRef.current.lower.length) query.set("lower", indicatorRef.current.lower.join(","));
      if (!catalogLoaded.current) query.set("catalog", "1");
      const [chart, orderBook] = await Promise.all([
        api<{ points: Point[]; candles?: Candle[]; interval?: number; indicators?: IndicatorData; catalog?: IndicatorCatalog }>(`/api/market/chart/${encodeURIComponent(code)}?${query}`),
        api<Book>(`/api/market/book/${encodeURIComponent(code)}`),
      ]);
      if (seq !== marketSeq.current) return;
      lastPollError.current.delete("market");
      setPoints(chart.points.map((point) => ({ ...point, price: Number(point.price) })));
      setCandles((chart.candles ?? []).map((candle) => ({ ...candle, open: Number(candle.open), high: Number(candle.high), low: Number(candle.low), close: Number(candle.close) })));
      if (chart.interval) setCandleInterval(chart.interval);
      setIndicatorData(chart.indicators ?? { upper: {}, lower: {} });
      if (chart.catalog) { setIndicatorCatalog(chart.catalog); catalogLoaded.current = true; }
      setBook(orderBook);
    } catch (cause) {
      if (seq === marketSeq.current) reportPollError("market", cause, "시세를 불러오지 못했습니다.");
    }
  }, [reportPollError, lastPollError]);

  // Runs now and every 5s while the tab is visible; a new instrument, range or indicator choice restarts it.
  usePolling(() => refreshMarket(selectedCode, range, interval), 5000, Boolean(selectedCode), [selectedCode, range, interval, indicatorQuery, refreshMarket]);
  // The selected instrument's latest price, straight from the server every second (a one-row read, unlike the
  // instrument list). This is the price a "현재가" order is made at, and the server checks it again on arrival.
  const pollQuote = useCallback(async () => {
    const code = selectedCode;
    if (!code) return;
    try {
      const fresh = await api<{ price: Money }>(`/api/market/quote/${encodeURIComponent(code)}`);
      lastPollError.current.delete("quote");
      setQuote({ code, price: fresh.price, at: Date.now() });
    } catch (cause) { reportPollError("quote", cause, "현재가를 불러오지 못했습니다."); }
  }, [selectedCode, reportPollError, lastPollError]);
  usePolling(pollQuote, 1000, Boolean(selectedCode), [selectedCode]);

  /** Re-reads the chart and book for the instrument and range on screen. */
  const reloadMarket = () => refreshMarket(selectedCode, range, interval);

  return {
    instruments, selectedCode, setSelectedCode, selected: instruments.find((item) => item.code === selectedCode) ?? null, pricedAt, quote,
    range, setRange, interval, setIntervalSeconds, points, candles, candleInterval, book,
    indicators, indicatorData, indicatorCatalog, chooseIndicators,
    loadInstruments, reloadMarket, pollQuote,
  };
}
