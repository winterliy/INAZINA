import { useEffect, useRef, useState } from "react";
import { formatChange } from "./ExchangeHeatmap";

type Band = { code: string; name: string; percent: number; color: string };

/** Portfolio hero background: one vertical band per holding, width = share of value. */
export function AllocationBands({ bands, onSelect }: { bands: Band[]; onSelect: (code: string) => void }) {
  const [hover, setHover] = useState<Band | null>(null);
  if (!bands.length) return <div className="bg-bands empty" aria-hidden="true" />;
  return <div className={hover ? "bg-bands focusing" : "bg-bands"} onPointerLeave={() => setHover(null)}>
    {bands.map((band) => <button type="button" key={band.code} className={hover?.code === band.code ? "on" : ""} style={{ flexGrow: band.percent, background: band.color }}
      aria-label={`${band.name} ${band.percent.toFixed(1)}%`} onPointerEnter={() => setHover(band)} onFocus={() => setHover(band)} onBlur={() => setHover(null)} onClick={() => onSelect(band.code)} />)}
    {hover && <p className="bg-hero-tip" role="status"><b>{hover.name}</b> {hover.code} · {hover.percent.toFixed(1)}%</p>}
  </div>;
}

type Dot = { code: string; name: string; volume: number; change: number };
const GAP = 18;

/** Heatmap hero background: a dot grid split into column slices per stock (width = volume, colour = 24h change). Dots swell near the pointer. */
export function DotField({ stocks, onSelect }: { stocks: Dot[]; onSelect?: (code: string) => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<Dot | null>(null);
  const hoverRef = useRef<Dot | null>(null);
  const stocksRef = useRef(stocks);
  stocksRef.current = stocks;

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    let pointer: { x: number; y: number } | null = null;
    let frame = 0, visible = true, width = 0, height = 0;
    // Column slices: each stock owns a share of the width proportional to its volume.
    const sliceAt = (x: number) => {
      const list = stocksRef.current;
      const total = list.reduce((sum, item) => sum + Math.max(item.volume, 0), 0) || list.length;
      let edge = 0;
      for (const item of list) { edge += (total === list.length ? 1 : Math.max(item.volume, 0)) / total * width; if (x <= edge) return item; }
      return list[list.length - 1] ?? null;
    };
    const colour = (item: Dot | null, boost: number) => {
      if (!item) return `rgba(163,163,163,${.35 + boost * .4})`;
      const strength = Math.min(1, .5 + Math.abs(item.change) / 6);
      const rgb = item.change > 0 ? "31,138,91" : item.change < 0 ? "217,59,28" : "163,163,163";
      return `rgba(${rgb},${Math.min(1, strength * (.55 + boost * .6))})`;
    };
    const draw = (time: number) => {
      context.clearRect(0, 0, width, height);
      for (let x = GAP / 2; x < width; x += GAP) {
        const item = sliceAt(x);
        for (let y = GAP / 2; y < height; y += GAP) {
          let boost = 0;
          if (pointer) { const distance = Math.hypot(x - pointer.x, y - pointer.y); boost = Math.max(0, 1 - distance / 140); }
          else if (!still) boost = Math.max(0, Math.sin(x / 90 + y / 140 - time / 1400)) * .35;
          context.fillStyle = colour(item, boost);
          context.beginPath(); context.arc(x, y, 2 + boost * 4.5, 0, Math.PI * 2); context.fill();
        }
      }
      if (visible && !still) frame = requestAnimationFrame(draw);
    };
    const resize = () => {
      const box = canvas.getBoundingClientRect(), ratio = devicePixelRatio || 1;
      width = box.width; height = box.height;
      canvas.width = width * ratio; canvas.height = height * ratio;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      if (still) draw(0);
    };
    // Track the pointer over the whole hero (window listener), so passing over the
    // glass panel or tooltip on top of the canvas does not count as leaving it.
    const move = (event: PointerEvent) => {
      const box = canvas.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) { if (pointer) leave(); return; }
      pointer = { x: event.clientX - box.left, y: event.clientY - box.top };
      const item = sliceAt(pointer.x);
      if (item !== hoverRef.current) { hoverRef.current = item; setHover(item); }
      if (still) draw(0);
    };
    function leave() { pointer = null; hoverRef.current = null; setHover(null); if (still) draw(0); }
    const observer = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; cancelAnimationFrame(frame); if (visible && !still) frame = requestAnimationFrame(draw); });
    const sizer = new ResizeObserver(resize);
    sizer.observe(canvas); observer.observe(canvas);
    window.addEventListener("pointermove", move, { passive: true }); document.documentElement.addEventListener("pointerleave", leave);
    resize();
    return () => { cancelAnimationFrame(frame); sizer.disconnect(); observer.disconnect(); window.removeEventListener("pointermove", move); document.documentElement.removeEventListener("pointerleave", leave); };
  }, []);

  return <div className="bg-dots">
    <canvas ref={canvasRef} role="img" aria-label="종목별 거래량(폭)과 24시간 변동률(색)을 점으로 나타낸 배경" style={{ cursor: hover && onSelect ? "pointer" : "default" }} onClick={() => hover && onSelect?.(hover.code)} />
    {hover && <p className="bg-hero-tip" role="status"><b>{hover.name}</b> {hover.code} · <span className={hover.change > 0 ? "up" : hover.change < 0 ? "down" : ""}>{formatChange(hover.change)}</span></p>}
  </div>;
}
