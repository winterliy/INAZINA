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

/** A ring gauge filled clockwise from the top by `ratio` (0–1), with a centre figure. */
export function Gauge({ ratio, center, label }: { ratio: number; center: string; label?: string }) {
  const fill = Math.max(0, Math.min(1, ratio)) * 314;
  return <svg className="gauge" viewBox="0 0 120 120" role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
    <circle cx="60" cy="60" r="50" className="gauge-track" />
    <circle cx="60" cy="60" r="50" className="gauge-fill" strokeDasharray={`${fill} 314`} transform="rotate(-90 60 60)" />
    <text x="60" y="66" textAnchor="middle">{center}</text>
  </svg>;
}

/** The largest six as slices, the rest as one, plus an optional remainder slice. */
export function topSlices(items: { label: string; value: number }[], rest: string, remainder?: Slice): Slice[] {
  const others = items.slice(6).reduce((sum, item) => sum + item.value, 0);
  return [...items.slice(0, 6).map((item, index) => ({ ...item, color: SLICE_COLORS[index] })),
    ...(others ? [{ label: rest, value: others, color: SLICE_COLORS[6] }] : []), ...(remainder && remainder.value > 0 ? [remainder] : [])];
}


export type Shareholders = { totalShares: number; holders: { userId: string; name: string; quantity: number }[] };

/** Who owns a company or stock: the six largest holders, the rest together, and shares no one holds. */
export function ShareholderPie({ data }: { data: Shareholders }) {
  const held = data.holders.reduce((sum, item) => sum + item.quantity, 0);
  const total = Math.max(data.totalShares, held);
  const slices = topSlices(data.holders.map((item) => ({ label: item.name, value: item.quantity })), "기타 주주",
    { label: "미보유(유통 전)", value: total - held, color: "var(--chip)" });
  return <Donut slices={slices} total={total} center={number(total)} unit="발행 주식" label="주주 구성 원그래프" suffix="주" />;
}
