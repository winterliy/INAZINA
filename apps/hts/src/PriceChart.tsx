import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactNode, WheelEvent as ReactWheelEvent } from "react";
import { axisWidth, panelHeight, PRICE_LABEL_PX, priceTicks, TIME_LABEL_PX, timeTicks } from "./chartScale";

export type Point = { time: number; price: number };
export type Candle = { time: number; open: number; high: number; low: number; close: number; ticks: number; volume?: number };
export type Drawing = { id: string; type: "trend" | "horizontal"; anchors: Point[] };
export type Tool = "select" | "trend" | "horizontal";
type View = { minTime: number; maxTime: number; minPrice: number; maxPrice: number };
type Gesture =
  | { kind: "pan"; start: { x: number; y: number }; view: View }
  | { kind: "draw"; start: Point }
  | { kind: "move"; start: Point; drawing: Drawing; anchor: number | null };

// The price axis (its width) and the lower panels (their height) are sized per render from chartScale.
const BOTTOM = 26, TOP = 12, LEFT = 8;
// Pixels per candle in the live view, like an HTS: a wider window shows more history instead of fatter candles.
const BAR_SPACING = 9;
const money = (value: number) => new Intl.NumberFormat("ko-KR", { maximumFractionDigits: value < 10 ? 2 : 0 }).format(value || 0);

export type Series = { label: string; kind: "line" | "bar" | "marker" | "profile"; levels: number[]; color: string; values: (number | null)[] };
export type IndicatorData = { upper: Record<string, Series[]>; lower: Record<string, Series[]> };
export type IndicatorChoice = { upper: string[]; lower: string[] };
/** Discord /그래프 defaults: 이동평균선 and 거래량. */
export const DEFAULT_INDICATORS: IndicatorChoice = { upper: ["sma"], lower: ["volume"] };
const entries = (labels: Record<string, string>) => Object.entries(labels).map(([key, label]) => ({ key, label }));
/** Every Discord /그래프 상단·하단 지표 (functions/technical_indicators.py), so the menu is whole before the server answers. */
export const INDICATOR_CATALOG: IndicatorCatalog = {
  upper: entries({ high_low: "최고최저가", sma: "이동평균선", ichimoku: "일목균형표", bollinger: "볼린저 밴드", supertrend: "슈퍼트렌드", volume_profile: "매물대분석",
    envelope: "엔벨로프", fractals: "윌리엄스 프랙탈", ema: "지수이동평균선", sar: "파라볼릭 SAR", price_channel: "프라이스 채널", vwap: "VWAP", anchored_vwap: "고정 VWAP" }),
  lower: entries({ volume: "거래량", mass: "매스 인덱스", momentum: "모멘텀", volume_osc: "볼륨 오실레이터", bb_percent: "볼린저 밴드 %B", bb_width: "볼린저 밴드폭",
    stochastic: "스토캐스틱", stoch_rsi: "스토캐스틱 RSI", williams_r: "윌리엄스 R", disparity: "이격도", intraday_intensity: "일중 강도지수", chaikin: "체이킨 오실레이터",
    trix: "트릭스", price_osc: "프라이스 오실레이터", ad: "AD 라인", adx: "ADX", cci: "CCI", dmi: "DMI", macd: "MACD", mfi: "MFI", obv: "OBV", roc: "ROC", rmi: "RMI", rsi: "RSI" }),
};
export type IndicatorCatalog = { upper: { key: string; label: string }[]; lower: { key: string; label: string }[] };
type Value = { time: number; value: number };

/**
 * Price chart with persistent drawings.
 *
 * Dragging the empty chart pans it. Pressing on the chart never selects page
 * text or other elements: pointer and mouse defaults are cancelled.
 * Any interaction (drawing, moving, panning, zooming) freezes the axes, so new
 * quotes arriving every few seconds no longer rescale the chart under the lines.
 * "실시간" returns to the auto-fitted, live-following view.
 * Like an HTS, the scales re-plan for the space: price steps for the height, calendar
 * steps for the width, an axis as wide as its labels, and panels sized to the chart.
 */
export function PriceChart({ points, candles, candleInterval, mode, fallbackPrice, drawings, onDrawingsChange, tool, onToolChange, resetKey, controls, label, indicators, data, catalog, onIndicatorsChange }: {
  points: Point[]; candles: Candle[]; candleInterval: number; mode: "candle" | "line"; fallbackPrice: number;
  drawings: Drawing[]; onDrawingsChange: (next: Drawing[]) => void; tool: Tool; onToolChange: (tool: Tool) => void;
  resetKey: string; controls: ReactNode; label: string;
  /** Discord /그래프 상단·하단 지표: the choice, the server-computed series aligned to candles, and the option list. */
  indicators: IndicatorChoice; data: IndicatorData; catalog: IndicatorCatalog; onIndicatorsChange: (next: IndicatorChoice) => void;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ w: 900, h: 420 });
  const [view, setView] = useState<View | null>(null);
  const [selected, setSelected] = useState("");
  const [draft, setDraft] = useState<Point[] | null>(null);
  const [hover, setHover] = useState<{ x: number; y: number } | null>(null);
  const gesture = useRef<Gesture | null>(null);
  const history = useRef<Drawing[][]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);
  const toggleIndicator = (group: keyof IndicatorChoice, key: string) => onIndicatorsChange({ ...indicators, [group]: indicators[group].includes(key) ? indicators[group].filter((item) => item !== key) : [...indicators[group], key] });

  useEffect(() => {
    if (!settingsOpen) return;
    const close = (event: MouseEvent) => { if (!settingsRef.current?.contains(event.target as Node)) setSettingsOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setSettingsOpen(false); };
    document.addEventListener("mousedown", close);
    window.addEventListener("keydown", escape);
    return () => { document.removeEventListener("mousedown", close); window.removeEventListener("keydown", escape); };
  }, [settingsOpen]);

  useLayoutEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setSize({ w: Math.max(320, entry.contentRect.width), h: Math.max(240, entry.contentRect.height) }));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => { setView(null); setSelected(""); setDraft(null); history.current = []; }, [resetKey]);

  // Resizing keeps the time scale and the right edge, so a wider window reveals older data.
  const lastWidth = useRef(size.w);
  useEffect(() => {
    const before = lastWidth.current;
    lastWidth.current = size.w;
    if (before === size.w) return;
    setView((current) => {
      if (!current) return current;
      const span = (current.maxTime - current.minTime) * (size.w - axis - LEFT) / (before - axis - LEFT);
      return { ...current, minTime: current.maxTime - span, ...fit(current.maxTime - span, current.maxTime) };
    });
    // fit reads the latest series; only width changes should trigger this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size.w]);

  const showCandles = mode === "candle" && candles.length > 0;
  const series = useMemo(() => showCandles ? candles.map((item) => ({ time: item.time, price: item.close }))
    : points.length ? points : [{ time: Date.now() / 1000, price: fallbackPrice || 1 }], [showCandles, candles, points, fallbackPrice]);
  // Wide enough for the largest price this series can show.
  const axis = useMemo(() => axisWidth([money(Math.max(...series.map((item) => item.price)) * 1.1)]), [series]);

  // Price range fitted to the data inside [minTime, maxTime].
  function fit(minTime: number, maxTime: number) {
    const inside = showCandles
      ? candles.filter((item) => item.time + candleInterval >= minTime && item.time <= maxTime).flatMap((item) => [item.low, item.high])
      : series.filter((item) => item.time >= minTime && item.time <= maxTime).map((item) => item.price);
    const prices = inside.length ? inside : showCandles ? candles.flatMap((item) => [item.low, item.high]) : series.map((item) => item.price);
    let low = Math.min(...prices), high = Math.max(...prices);
    if (!Number.isFinite(low) || !Number.isFinite(high)) { low = 1; high = 2; }
    if (low === high) { low *= .99; high *= 1.01; }
    const gap = (high - low) * .08;
    return { minPrice: Math.max(0, low - gap), maxPrice: high + gap };
  }

  // Live view: latest data, as many candles as the width holds at BAR_SPACING.
  const auto = useMemo<View>(() => {
    const start = series[0].time, end = series.at(-1)!.time + (showCandles ? candleInterval : 0);
    let minTime = start === end ? start - 1800 : start;
    const maxTime = start === end ? end + 1800 : end;
    if (showCandles) {
      const capacity = Math.max(10, Math.floor((size.w - axis - LEFT) / BAR_SPACING));
      minTime = end - capacity * candleInterval;
    }
    return { minTime, maxTime, ...fit(minTime, maxTime) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [series, showCandles, candles, candleInterval, size.w, axis]);
  const v = view ?? auto;

  // Lower panels stack under the price plot in catalog order, sharing its time axis; the canvas grows per panel.
  // A chosen panel shows even before its series arrive (it says so), so the choice is visible at once.
  const lowers = catalog.lower.map((item) => item.key).filter((key) => indicators.lower.includes(key));
  const PANEL_GAP = 10, PANEL_H = panelHeight(size.h - BOTTOM - TOP, lowers.length);
  const R = size.w - axis, B = size.h - BOTTOM - (PANEL_GAP + PANEL_H) * lowers.length;
  const panelTop = (index: number) => B + PANEL_GAP + (PANEL_GAP + PANEL_H) * index;
  const plotBottom = size.h - BOTTOM;
  const X = (time: number) => LEFT + ((time - v.minTime) / (v.maxTime - v.minTime)) * (R - LEFT);
  const Y = (price: number) => B - ((price - v.minPrice) / (v.maxPrice - v.minPrice)) * (B - TOP);
  const toPoint = (x: number, y: number): Point => ({
    time: v.minTime + ((x - LEFT) / (R - LEFT)) * (v.maxTime - v.minTime),
    price: v.maxPrice - ((y - TOP) / (B - TOP)) * (v.maxPrice - v.minPrice),
  });
  const local = (event: { clientX: number; clientY: number }) => {
    const rect = svgRef.current!.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  const freeze = () => { if (!view) setView(auto); return view ?? auto; };
  const commit = (next: Drawing[]) => { history.current.push(drawings); onDrawingsChange(next); };

  function zoom(factor: number, centerX?: number) {
    const current = freeze();
    const anchor = centerX === undefined ? (current.minTime + current.maxTime) / 2 : toPoint(centerX, 0).time;
    const span = Math.max(60, (current.maxTime - current.minTime) * factor);
    const ratio = (anchor - current.minTime) / (current.maxTime - current.minTime);
    const minTime = anchor - span * ratio, maxTime = anchor + span * (1 - ratio);
    setView({ minTime, maxTime, ...fit(minTime, maxTime) });
  }
  function shift(direction: number) {
    const current = freeze();
    const step = (current.maxTime - current.minTime) * .25 * direction;
    setView({ minTime: current.minTime + step, maxTime: current.maxTime + step, ...fit(current.minTime + step, current.maxTime + step) });
  }

  function down(event: ReactPointerEvent<SVGSVGElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    const at = local(event), point = toPoint(at.x, at.y);
    freeze();
    if (tool === "horizontal") {
      commit([...drawings, { id: crypto.randomUUID(), type: "horizontal", anchors: [point] }]);
      onToolChange("select");
      return;
    }
    gesture.current = tool === "trend" ? { kind: "draw", start: point } : { kind: "pan", start: at, view: view ?? auto };
    if (tool === "trend") setDraft([point, point]); else setSelected("");
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function startMove(event: ReactPointerEvent<SVGElement>, drawing: Drawing, anchor: number | null) {
    if (tool !== "select" || event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    freeze();
    setSelected(drawing.id);
    const at = local(event);
    history.current.push(drawings);
    gesture.current = { kind: "move", start: toPoint(at.x, at.y), drawing, anchor };
    svgRef.current?.setPointerCapture(event.pointerId);
  }

  function move(event: ReactPointerEvent<SVGSVGElement>) {
    const at = local(event);
    setHover(at);
    const active = gesture.current;
    if (!active) return;
    if (active.kind === "pan") {
      // The price range refits to what the drag brings into view, as an HTS does.
      const dt = ((at.x - active.start.x) / (R - LEFT)) * (active.view.maxTime - active.view.minTime);
      const minTime = active.view.minTime - dt, maxTime = active.view.maxTime - dt;
      setView({ minTime, maxTime, ...fit(minTime, maxTime) });
    } else if (active.kind === "draw") {
      setDraft([active.start, toPoint(at.x, at.y)]);
    } else {
      const now = toPoint(at.x, at.y);
      const dt = now.time - active.start.time, dp = now.price - active.start.price;
      const original = active.drawing;
      onDrawingsChange(drawings.map((item) => item.id !== original.id ? item : {
        ...original,
        anchors: original.anchors.map((anchor, index) => active.anchor === null || active.anchor === index
          ? { time: Math.round(anchor.time + dt), price: Math.max(0.01, anchor.price + dp) } : anchor),
      }));
    }
  }

  function up(event: ReactPointerEvent<SVGSVGElement>) {
    const active = gesture.current;
    gesture.current = null;
    if (active?.kind === "draw") {
      const at = local(event), end = toPoint(at.x, at.y);
      const moved = Math.abs(X(end.time) - X(active.start.time)) + Math.abs(Y(end.price) - Y(active.start.price)) > 4;
      if (moved) {
        const id = crypto.randomUUID();
        commit([...drawings, { id, type: "trend", anchors: [{ ...active.start, time: Math.round(active.start.time) }, { ...end, time: Math.round(end.time) }] }]);
        setSelected(id);
        onToolChange("select");
      }
      setDraft(null);
    }
  }

  function wheel(event: ReactWheelEvent<SVGSVGElement>) {
    zoom(event.deltaY > 0 ? 1.15 : 1 / 1.15, local(event).x);
  }

  function undo() { const last = history.current.pop(); if (last) onDrawingsChange(last); }
  function remove() { if (!selected) return; commit(drawings.filter((item) => item.id !== selected)); setSelected(""); }

  useEffect(() => {
    function key(event: KeyboardEvent) {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;
      if ((event.key === "Delete" || event.key === "Backspace") && selected) { event.preventDefault(); remove(); }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") { event.preventDefault(); undo(); }
      if (event.key === "Escape") { gesture.current = null; setDraft(null); onToolChange("select"); }
    }
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  // React's onWheel is passive; a native listener lets the chart zoom without scrolling the page.
  useEffect(() => {
    const element = svgRef.current;
    if (!element) return;
    const stop = (event: WheelEvent) => event.preventDefault();
    element.addEventListener("wheel", stop, { passive: false });
    return () => element.removeEventListener("wheel", stop);
  }, []);

  const span = v.maxTime - v.minTime;
  const yTicks = priceTicks(v.minPrice, v.maxPrice, Math.max(2, Math.floor((B - TOP) / PRICE_LABEL_PX)));
  const tickText = new Intl.NumberFormat("ko-KR", { minimumFractionDigits: yTicks.decimals, maximumFractionDigits: yTicks.decimals });
  const xTicks = timeTicks(v.minTime, v.maxTime, Math.max(2, Math.floor((R - LEFT) / TIME_LABEL_PX))).ticks
    .filter((tick) => X(tick.time) >= LEFT + 24 && X(tick.time) <= R - 24);
  const timeText = (time: number) => new Date(time * 1000).toLocaleString("ko-KR", span > 2 * 86400 ? { month: "numeric", day: "numeric" } : span > 6 * 3600 ? { day: "numeric", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
  const visibleCandles = showCandles ? candles.filter((item) => item.time + candleInterval >= v.minTime && item.time <= v.maxTime) : [];
  const visibleVolumeCandles = showCandles ? visibleCandles : candles.filter((item) => item.time + candleInterval >= v.minTime && item.time <= v.maxTime);
  // Traded quantity from the server; tick count when an older server sent none.
  const volumeOf = (item: Candle) => item.volume ?? item.ticks;
  const maxTicks = visibleVolumeCandles.reduce((max, item) => Math.max(max, volumeOf(item)), 1);
  const near = (item: Value) => item.time >= v.minTime - span && item.time <= v.maxTime + span;
  const path = (values: Value[], y: (value: number) => number) => values.filter(near).map((item, index) => `${index ? "L" : "M"}${X(item.time).toFixed(1)},${y(item.value).toFixed(1)}`).join(" ");

  // Server series are index-aligned to candles; leading shifts (일목 선행스팬) run past the last candle.
  const seriesTime = (index: number) => candles.length ? (index < candles.length ? candles[index].time : candles.at(-1)!.time + (index - candles.length + 1) * candleInterval) + candleInterval / 2 : 0;
  const points2 = (item: Series): Value[] => item.values.flatMap((value, index) => value === null ? [] : [{ time: seriesTime(index), value }]);
  const visibleIndex = (index: number) => { const time = seriesTime(index); return time >= v.minTime - span * .1 && time <= v.maxTime + span * .1; };
  const upperSeries = useMemo(() => catalog.upper.filter((item) => indicators.upper.includes(item.key)).flatMap((item) => (data.upper[item.key] ?? []).map((entry) => ({ key: item.key, entry, values: points2(entry) }))),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [catalog, indicators.upper, data, candles, candleInterval]);
  const linePath = showCandles ? "" : series.filter((item) => item.time >= v.minTime - span && item.time <= v.maxTime + span).map((item, index) => `${index ? "L" : "M"}${X(item.time).toFixed(1)},${Y(item.price).toFixed(1)}`).join(" ");
  const candleWidth = Math.max(1, Math.min(28, (candleInterval / span) * (R - LEFT) * .7));
  const hoverPoint = hover && hover.x >= LEFT && hover.x <= R && hover.y >= TOP && hover.y <= B ? toPoint(hover.x, hover.y) : null;
  const last = series.at(-1)!;

  function lowerPanel(key: string, index: number) {
    const top = panelTop(index), bottom = top + PANEL_H;
    const label = catalog.lower.find((item) => item.key === key)?.label ?? key;
    const empty = (text: string) => <text x={R - 4} y={top + 14} textAnchor="end" className="panel-empty">{text}</text>;
    let body: ReactNode, legend: ReactNode = null;
    if (key === "volume") {
      body = <><g clipPath={`url(#${key}-plot)`}>{visibleVolumeCandles.map((item) => { const x = X(item.time + candleInterval / 2); const height = Math.max(1, (volumeOf(item) / maxTicks) * (PANEL_H - 18)); return <rect key={item.time} x={x - candleWidth / 2} y={bottom - height} width={Math.max(1, candleWidth)} height={height} className={item.close >= item.open ? "volume-bar up" : "volume-bar down"}><title>{`${timeText(item.time)} · 거래량 ${money(volumeOf(item))}`}</title></rect>; })}</g>
        {!candles.length && empty("거래 데이터 없음")}</>;
    } else {
      const group = data.lower[key] ?? [];
      const shown = group.flatMap((item) => item.values.filter((value, at): value is number => value !== null && visibleIndex(at)));
      const levels = group.flatMap((item) => item.levels);
      let low = Math.min(...shown, ...levels), high = Math.max(...shown, ...levels);
      if (!Number.isFinite(low) || !Number.isFinite(high)) { low = 0; high = 1; }
      if (low === high) { low -= 1; high += 1; }
      const pad = (high - low) * .08, inner = PANEL_H - 18;
      const y = (value: number) => bottom - 2 - ((value - (low - pad)) / (high - low + pad * 2)) * inner;
      const width = Math.max(1, candleWidth * .8);
      body = <>{[...new Set(levels)].map((level) => <g key={level}><line x1={LEFT} x2={R} y1={y(level)} y2={y(level)} className="indicator-guide" /><text x={R + 8} y={y(level) + 4} className="axis-label">{money(level)}</text></g>)}
        <g clipPath={`url(#${key}-plot)`}>{group.map((item) => item.kind === "bar"
          ? <g key={item.label}>{item.values.map((value, at) => value === null || !visibleIndex(at) ? null : <rect key={at} x={X(seriesTime(at)) - width / 2} y={Math.min(y(0), y(value))} width={width} height={Math.max(1, Math.abs(y(value) - y(0)))} className={value >= 0 ? "macd-bar up" : "macd-bar down"} />)}</g>
          : <path key={item.label} d={path(points2(item), y)} className="indicator-line" style={{ stroke: item.color }} />)}</g>
        {!shown.length && empty(key in data.lower ? "기록이 더 쌓이면 표시됩니다" : "지표 데이터를 받는 중…")}</>;
      legend = group.map((item) => <tspan key={item.label} dx={8} style={{ fill: item.color }}>● {item.label}</tspan>);
    }
    return <g key={key} className="auxiliary-panel"><line x1={LEFT} x2={R} y1={top} y2={top} className="panel-divider" /><text x={LEFT + 4} y={top + 13} className="panel-label">{label}{legend}</text>{body}</g>;
  }

  // 매물대분석: traded volume by price band, drawn from the right edge of the price plot.
  const profileBars = () => {
    const bins = 24, step = (v.maxPrice - v.minPrice) / bins, totals = new Array(bins).fill(0);
    for (const item of candles) {
      const typical = (item.high + item.low + item.close) / 3, bin = Math.floor((typical - v.minPrice) / step);
      if (bin >= 0 && bin < bins) totals[bin] += volumeOf(item);
    }
    const most = Math.max(1, ...totals), reach = (R - LEFT) * .28;
    return totals.map((total, bin) => total ? <rect key={bin} x={R - (total / most) * reach} y={Y(v.minPrice + step * (bin + 1))} width={(total / most) * reach} height={Math.max(1, Y(v.minPrice + step * bin) - Y(v.minPrice + step * (bin + 1)) - 1)} className="profile-bar" /> : null);
  };

  // Indicator layers depend on the view and data, not on the pointer: hovering only redraws the crosshair.
  const viewKey = `${v.minTime}:${v.maxTime}:${v.minPrice}:${v.maxPrice}:${size.w}:${size.h}`;
  const overlayLayer = useMemo(() => <>
          {indicators.upper.includes("volume_profile") && profileBars()}
          {upperSeries.filter((item) => item.entry.kind !== "profile").map(({ key, entry, values }) => entry.kind === "marker"
            ? <g key={`${key}-${entry.label}`} style={{ fill: entry.color }}>{values.filter(near).map((item) => <circle key={item.time} cx={X(item.time)} cy={Y(item.value)} r={2.4} />)}</g>
            : <path key={`${key}-${entry.label}`} d={path(values, Y)} className="overlay-path" style={{ stroke: entry.color }} />)}
  </>,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [viewKey, upperSeries, indicators.upper, candles]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const lowerLayer = useMemo(() => lowers.map(lowerPanel), [viewKey, lowers.join(","), data, candles, candleInterval, catalog]);

  return <section className="chart-panel">
    <div className="chart-head">
      <div className="segmented">{controls}</div>
      <div className="drawing-tools">
        <button title="이전 구간" onClick={() => shift(-1)}>←</button>
        <button title="다음 구간" onClick={() => shift(1)}>→</button>
        <button title="확대 (휠)" onClick={() => zoom(1 / 1.5)}>＋</button>
        <button title="축소 (휠)" onClick={() => zoom(1.5)}>－</button>
        <button className={view ? "live" : "live selected"} title="자동 맞춤 · 최신 시세 따라가기" onClick={() => setView(null)}>{view ? "실시간" : "● 실시간"}</button>
        <span className="divider" aria-hidden="true" />
        <button className={tool === "select" ? "selected" : ""} title="선택 · 선 이동 · 끝점 조절 · 빈 곳 드래그로 이동" onClick={() => onToolChange("select")}>선택</button>
        <button className={tool === "trend" ? "selected" : ""} title="드래그해서 추세선" onClick={() => onToolChange("trend")}>추세선</button>
        <button className={tool === "horizontal" ? "selected" : ""} title="클릭한 가격에 수평선" onClick={() => onToolChange("horizontal")}>수평선</button>
        <button title="되돌리기 (Ctrl/⌘+Z)" disabled={!history.current.length} onClick={undo}>되돌리기</button>
        <button title="선택한 선 삭제 (Delete)" disabled={!selected} onClick={remove}>삭제</button>
        <button title="모든 선 삭제" disabled={!drawings.length} onClick={() => { if (window.confirm("이 종목의 차트 선을 모두 삭제할까요?")) { commit([]); setSelected(""); } }}>전체 삭제</button>
      </div>
      <div className="chart-settings" ref={settingsRef}>
        <button type="button" className={settingsOpen ? "chart-settings-button selected" : "chart-settings-button"} aria-label="보조지표 · 하단 그래프 설정" title="보조지표 · 하단 그래프 설정" aria-expanded={settingsOpen} onClick={() => setSettingsOpen((open) => !open)}>
          <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /><circle cx="12" cy="12" r="3" /></svg>
        </button>
        {settingsOpen && <div className="chart-settings-menu" role="group" aria-label="차트 설정">
          {(["upper", "lower"] as const).map((group) => <section key={group}>
            <p>{group === "upper" ? `보조지표 · 가격 위 (${indicators.upper.length})` : `하단 그래프 (${indicators.lower.length})`}</p>
            <div className="chart-toggle-grid">{catalog[group].map((item) => <label key={item.key} className="chart-toggle"><span>{item.label}</span><input type="checkbox" role="switch" checked={indicators[group].includes(item.key)} onChange={() => toggleIndicator(group, item.key)} /></label>)}</div>
          </section>)}
          <button type="button" className="chart-reset" onClick={() => onIndicatorsChange(DEFAULT_INDICATORS)}>기본값 (이동평균선 · 거래량)</button>
        </div>}
      </div>
    </div>
    <div className="chart-canvas" ref={wrapRef} style={{ "--lower-panels": lowers.length } as CSSProperties}>
      <svg ref={svgRef} width={size.w} height={size.h} viewBox={`0 0 ${size.w} ${size.h}`} role="img" aria-label={`${label} 차트`}
        className={`chart-svg ${tool !== "select" ? "drawing-mode" : gesture.current?.kind === "pan" ? "panning" : ""}`}
        onPointerDown={down} onMouseDown={(event) => event.preventDefault()} onDragStart={(event) => event.preventDefault()} onPointerMove={move} onPointerUp={up} onPointerCancel={() => { gesture.current = null; setDraft(null); }} onPointerLeave={() => setHover(null)} onWheel={wheel} onDoubleClick={() => setView(null)}>
        <defs><clipPath id="plot"><rect x={LEFT} y={TOP} width={R - LEFT} height={B - TOP} /></clipPath>{lowers.map((key, index) => <clipPath key={key} id={`${key}-plot`}><rect x={LEFT} y={panelTop(index)} width={R - LEFT} height={PANEL_H} /></clipPath>)}</defs>
        {yTicks.ticks.map((tick) => { const y = Y(tick); return <g key={`y${tick}`}><line x1={LEFT} x2={R} y1={y} y2={y} className="grid-line" /><text x={R + 8} y={y + 4} className="axis-label">{tickText.format(tick)}</text></g>; })}
        {xTicks.map((tick) => { const x = X(tick.time); return <g key={`x${tick.time}`}><line x1={x} x2={x} y1={TOP} y2={lowers.length ? plotBottom : B} className="grid-line vertical" /><text x={x} y={size.h - 8} textAnchor="middle" className="axis-label">{tick.label}</text></g>; })}
        <g clipPath="url(#plot)">
          {showCandles ? visibleCandles.map((item) => {
            const x = X(item.time + candleInterval / 2), top = Y(Math.max(item.open, item.close)), bottom = Y(Math.min(item.open, item.close));
            return <g key={item.time} className={item.close >= item.open ? "candle up" : "candle down"}><title>{`${new Date(item.time * 1000).toLocaleString("ko-KR")}\n시 ${money(item.open)} 고 ${money(item.high)} 저 ${money(item.low)} 종 ${money(item.close)}`}</title><line x1={x} x2={x} y1={Y(item.high)} y2={Y(item.low)} /><rect x={x - candleWidth / 2} y={top} width={candleWidth} height={Math.max(1, bottom - top)} /></g>;
          }) : <><path d={linePath} className="price-path" />{linePath && <path d={`${linePath} L${X(series.at(-1)!.time)},${B} L${X(series[0].time)},${B} Z`} className="price-area" />}</>}
          {overlayLayer}
          <line x1={LEFT} x2={R} y1={Y(last.price)} y2={Y(last.price)} className="last-line" />
          {drawings.map((drawing) => {
            const active = drawing.id === selected;
            const [a, b] = drawing.anchors;
            const ends = drawing.type === "horizontal" ? [{ x: LEFT, y: Y(a.price) }, { x: R, y: Y(a.price) }] : [{ x: X(a.time), y: Y(a.price) }, { x: X(b.time), y: Y(b.price) }];
            return <g key={drawing.id} className={active ? "drawing selected-drawing" : "drawing"}>
              <line x1={ends[0].x} y1={ends[0].y} x2={ends[1].x} y2={ends[1].y} className="drawing-line" />
              <line x1={ends[0].x} y1={ends[0].y} x2={ends[1].x} y2={ends[1].y} className="drawing-hit" onPointerDown={(event) => startMove(event, drawing, null)} />
              {active && drawing.type === "trend" && ends.map((end, index) => <circle key={index} cx={end.x} cy={end.y} r={6} className="drawing-handle" onPointerDown={(event) => startMove(event, drawing, index)} />)}
              {drawing.type === "horizontal" && <text x={R - 4} y={ends[0].y - 5} textAnchor="end" className="drawing-label">{money(a.price)}</text>}
            </g>;
          })}
          {draft && <line x1={X(draft[0].time)} y1={Y(draft[0].price)} x2={X(draft[1].time)} y2={Y(draft[1].price)} className="drawing-draft" />}
          {hoverPoint && !gesture.current && <g className="crosshair"><line x1={hover!.x} x2={hover!.x} y1={TOP} y2={B} /><line x1={LEFT} x2={R} y1={hover!.y} y2={hover!.y} /></g>}
        </g>
        {lowerLayer}
        {upperSeries.length > 0 && <text x={LEFT + 6} y={TOP + 14} className="overlay-legend">{upperSeries.filter((item) => item.entry.kind !== "profile").map(({ key, entry }) => <tspan key={`${key}-${entry.label}`} dx={6} style={{ fill: entry.color }}>● {entry.label}</tspan>)}</text>}
        <g className="price-tag"><rect x={R + 2} y={Y(last.price) - 10} width={axis - 4} height={20} rx={6} /><text x={R + 8} y={Y(last.price) + 4}>{money(last.price)}</text></g>
        {hoverPoint && !gesture.current && <g className="hover-tag"><rect x={R + 2} y={hover!.y - 10} width={axis - 4} height={20} rx={6} /><text x={R + 8} y={hover!.y + 4}>{money(hoverPoint.price)}</text></g>}
      </svg>
      {!points.length && !candles.length && <span className="chart-empty">이 구간에 저장된 가격 기록이 없습니다.</span>}
    </div>
  </section>;
}
