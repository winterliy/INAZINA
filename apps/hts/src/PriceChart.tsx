import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, ReactNode, WheelEvent as ReactWheelEvent } from "react";

export type Point = { time: number; price: number };
export type Candle = { time: number; open: number; high: number; low: number; close: number; ticks: number };
export type Drawing = { id: string; type: "trend" | "horizontal"; anchors: Point[] };
export type Tool = "select" | "trend" | "horizontal";
type View = { minTime: number; maxTime: number; minPrice: number; maxPrice: number };
type Gesture =
  | { kind: "draw"; start: Point }
  | { kind: "move"; start: Point; drawing: Drawing; anchor: number | null };

const AXIS = 72, BOTTOM = 26, TOP = 12, LEFT = 8;
// Pixels per candle in the live view, like an HTS: a wider window shows more history instead of fatter candles.
const BAR_SPACING = 9;
const money = (value: number) => new Intl.NumberFormat("ko-KR", { maximumFractionDigits: value < 10 ? 2 : 0 }).format(value || 0);

/**
 * Price chart with persistent drawings.
 *
 * Dragging the empty chart does nothing (no panning); use ←/→ and the wheel.
 * Any interaction (drawing, moving, zooming) freezes the axes, so new
 * quotes arriving every few seconds no longer rescale the chart under the lines.
 * "실시간" returns to the auto-fitted, live-following view.
 */
export function PriceChart({ points, candles, candleInterval, mode, fallbackPrice, drawings, onDrawingsChange, tool, onToolChange, resetKey, controls, label }: {
  points: Point[]; candles: Candle[]; candleInterval: number; mode: "candle" | "line"; fallbackPrice: number;
  drawings: Drawing[]; onDrawingsChange: (next: Drawing[]) => void; tool: Tool; onToolChange: (tool: Tool) => void;
  resetKey: string; controls: ReactNode; label: string;
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
      const span = (current.maxTime - current.minTime) * (size.w - AXIS - LEFT) / (before - AXIS - LEFT);
      return { ...current, minTime: current.maxTime - span, ...fit(current.maxTime - span, current.maxTime) };
    });
    // fit reads the latest series; only width changes should trigger this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size.w]);

  const showCandles = mode === "candle" && candles.length > 0;
  const series = useMemo(() => showCandles ? candles.map((item) => ({ time: item.time, price: item.close }))
    : points.length ? points : [{ time: Date.now() / 1000, price: fallbackPrice || 1 }], [showCandles, candles, points, fallbackPrice]);

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
      const capacity = Math.max(10, Math.floor((size.w - AXIS - LEFT) / BAR_SPACING));
      minTime = end - capacity * candleInterval;
    }
    return { minTime, maxTime, ...fit(minTime, maxTime) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [series, showCandles, candles, candleInterval, size.w]);
  const v = view ?? auto;

  const R = size.w - AXIS, B = size.h - BOTTOM;
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
    const at = local(event), point = toPoint(at.x, at.y);
    if (tool === "select") { setSelected(""); return; }
    freeze();
    if (tool === "horizontal") {
      commit([...drawings, { id: crypto.randomUUID(), type: "horizontal", anchors: [point] }]);
      onToolChange("select");
      return;
    }
    gesture.current = { kind: "draw", start: point };
    setDraft([point, point]);
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function startMove(event: ReactPointerEvent<SVGElement>, drawing: Drawing, anchor: number | null) {
    if (tool !== "select" || event.button !== 0) return;
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
    if (active.kind === "draw") {
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
  const timeText = (time: number) => new Date(time * 1000).toLocaleString("ko-KR", span > 2 * 86400 ? { month: "numeric", day: "numeric" } : span > 6 * 3600 ? { day: "numeric", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
  const visibleCandles = showCandles ? candles.filter((item) => item.time + candleInterval >= v.minTime && item.time <= v.maxTime) : [];
  const linePath = showCandles ? "" : series.filter((item) => item.time >= v.minTime - span && item.time <= v.maxTime + span).map((item, index) => `${index ? "L" : "M"}${X(item.time).toFixed(1)},${Y(item.price).toFixed(1)}`).join(" ");
  const candleWidth = Math.max(1, Math.min(28, (candleInterval / span) * (R - LEFT) * .7));
  const hoverPoint = hover && hover.x >= LEFT && hover.x <= R && hover.y >= TOP && hover.y <= B ? toPoint(hover.x, hover.y) : null;
  const last = series.at(-1)!;

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
        <button className={tool === "select" ? "selected" : ""} title="선택 · 선 이동 · 끝점 조절" onClick={() => onToolChange("select")}>선택</button>
        <button className={tool === "trend" ? "selected" : ""} title="드래그해서 추세선" onClick={() => onToolChange("trend")}>추세선</button>
        <button className={tool === "horizontal" ? "selected" : ""} title="클릭한 가격에 수평선" onClick={() => onToolChange("horizontal")}>수평선</button>
        <button title="되돌리기 (Ctrl/⌘+Z)" disabled={!history.current.length} onClick={undo}>되돌리기</button>
        <button title="선택한 선 삭제 (Delete)" disabled={!selected} onClick={remove}>삭제</button>
        <button title="모든 선 삭제" disabled={!drawings.length} onClick={() => { if (window.confirm("이 종목의 차트 선을 모두 삭제할까요?")) { commit([]); setSelected(""); } }}>전체 삭제</button>
      </div>
    </div>
    <div className="chart-canvas" ref={wrapRef}>
      <svg ref={svgRef} width={size.w} height={size.h} viewBox={`0 0 ${size.w} ${size.h}`} role="img" aria-label={`${label} 차트`}
        className={`chart-svg ${tool !== "select" ? "drawing-mode" : ""}`}
        onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={() => { gesture.current = null; setDraft(null); }} onPointerLeave={() => setHover(null)} onWheel={wheel} onDoubleClick={() => setView(null)}>
        <defs><clipPath id="plot"><rect x={LEFT} y={TOP} width={R - LEFT} height={B - TOP} /></clipPath></defs>
        {Array.from({ length: 5 }, (_, index) => { const y = TOP + ((B - TOP) / 4) * index; return <g key={`y${index}`}><line x1={LEFT} x2={R} y1={y} y2={y} className="grid-line" /><text x={R + 8} y={y + 4} className="axis-label">{money(v.maxPrice - ((v.maxPrice - v.minPrice) / 4) * index)}</text></g>; })}
        {Array.from({ length: 5 }, (_, index) => { const x = LEFT + ((R - LEFT) / 4) * index; return <g key={`x${index}`}><line x1={x} x2={x} y1={TOP} y2={B} className="grid-line vertical" /><text x={x} y={size.h - 8} textAnchor={index === 0 ? "start" : index === 4 ? "end" : "middle"} className="axis-label">{timeText(v.minTime + (span / 4) * index)}</text></g>; })}
        <g clipPath="url(#plot)">
          {showCandles ? visibleCandles.map((item) => {
            const x = X(item.time + candleInterval / 2), top = Y(Math.max(item.open, item.close)), bottom = Y(Math.min(item.open, item.close));
            return <g key={item.time} className={item.close >= item.open ? "candle up" : "candle down"}><title>{`${new Date(item.time * 1000).toLocaleString("ko-KR")}\n시 ${money(item.open)} 고 ${money(item.high)} 저 ${money(item.low)} 종 ${money(item.close)}`}</title><line x1={x} x2={x} y1={Y(item.high)} y2={Y(item.low)} /><rect x={x - candleWidth / 2} y={top} width={candleWidth} height={Math.max(1, bottom - top)} /></g>;
          }) : <><path d={linePath} className="price-path" />{linePath && <path d={`${linePath} L${X(series.at(-1)!.time)},${B} L${X(series[0].time)},${B} Z`} className="price-area" />}</>}
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
        <g className="price-tag"><rect x={R + 2} y={Y(last.price) - 10} width={AXIS - 4} height={20} rx={6} /><text x={R + 8} y={Y(last.price) + 4}>{money(last.price)}</text></g>
        {hoverPoint && !gesture.current && <g className="hover-tag"><rect x={R + 2} y={hover!.y - 10} width={AXIS - 4} height={20} rx={6} /><text x={R + 8} y={hover!.y + 4}>{money(hoverPoint.price)}</text></g>}
      </svg>
      {!points.length && !candles.length && <span className="chart-empty">이 구간에 저장된 가격 기록이 없습니다.</span>}
    </div>
  </section>;
}
