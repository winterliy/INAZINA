import { number } from "./common";

export const SLICE_COLORS = ["#0066cc", "#ff9500", "#30b0c7", "#af52de", "#ff2d55", "#34c759", "#a2845e"];

export type Slice = { label: string; value: number; color: string };

/** A donut: slices with hairline gaps around a centre figure, and a legend of percentages. */
export function Donut({ slices, total, center, unit, label, suffix = "" }: { slices: Slice[]; total: number; center: string; unit: string; label: string; suffix?: string }) {
  if (!total) return null;
  let angle = -Math.PI / 2;
  const arcs = slices.map((slice) => {
    const sweep = (slice.value / total) * Math.PI * 2;
    const start = angle; angle += sweep;
    const [x1, y1, x2, y2] = [Math.cos(start), Math.sin(start), Math.cos(angle), Math.sin(angle)].map((value) => 100 + value * 90);
    const path = sweep >= Math.PI * 2 - 1e-9 ? "M100 10 A90 90 0 1 1 99.99 10 Z" : `M100 100 L${x1} ${y1} A90 90 0 ${sweep > Math.PI ? 1 : 0} 1 ${x2} ${y2} Z`;
    return { ...slice, path };
  });
  return <figure className="holders">
    <svg viewBox="0 0 200 200" role="img" aria-label={label}>
      {arcs.map((arc) => <path key={arc.label} d={arc.path} fill={arc.color} />)}
      <circle className="holders-hole" cx="100" cy="100" r="58" />
      <text className="holders-total" x="100" y="102" textAnchor="middle">{center}</text>
      <text className="holders-unit" x="100" y="120" textAnchor="middle">{unit}</text>
    </svg>
    <figcaption><ul>{arcs.map((arc) => <li key={arc.label}><i style={{ background: arc.color }} /><span>{arc.label}</span><b>{(arc.value / total * 100).toFixed(2)}%</b><small>{number(arc.value)}{suffix}</small></li>)}</ul></figcaption>
  </figure>;
}

/** The largest six as slices, the rest as one, plus an optional remainder slice. */
export function topSlices(items: { label: string; value: number }[], rest: string, remainder?: Slice): Slice[] {
  const others = items.slice(6).reduce((sum, item) => sum + item.value, 0);
  return [...items.slice(0, 6).map((item, index) => ({ ...item, color: SLICE_COLORS[index] })),
    ...(others ? [{ label: rest, value: others, color: SLICE_COLORS[6] }] : []), ...(remainder && remainder.value > 0 ? [remainder] : [])];
}

