import { formatQuantity, moneyAdd, moneyCompare, moneyDivide, moneyMultiply, type Money } from "../../shared/money";
import { number } from "./common";

export const SLICE_COLORS = ["#0066cc", "#ff9500", "#30b0c7", "#af52de", "#ff2d55", "#34c759", "#a2845e"];

export type Slice = { label: string; value: Money; color: string };

/** A donut: slices with hairline gaps around a centre figure, and a legend of percentages. */
export function Donut({ slices, total, center, unit, label, suffix = "", quantityDigits = 0 }: { slices: Slice[]; total: Money; center: string; unit: string; label: string; suffix?: string; quantityDigits?: number }) {
  if (moneyCompare(total, 0) <= 0) return null;
  let angle = -Math.PI / 2;
  const arcs = slices.map((slice) => {
    const sweep = Number(moneyDivide(slice.value, total, 12)) * Math.PI * 2;
    const start = angle; angle += sweep;
    const [x1, y1, x2, y2] = [Math.cos(start), Math.sin(start), Math.cos(angle), Math.sin(angle)].map((value) => 100 + value * 90);
    const path = sweep >= Math.PI * 2 - 1e-9 ? "M100 10 A90 90 0 1 1 99.99 10 Z" : `M100 100 L${x1} ${y1} A90 90 0 ${sweep > Math.PI ? 1 : 0} 1 ${x2} ${y2} Z`;
    return { ...slice, path };
  });
  return <figure className="holders">
    <svg viewBox="0 0 200 200" role="img" aria-label={label}>
      {arcs.map((arc) => <path key={arc.label} d={arc.path} fill={arc.color} />)}
      <circle className="holders-hole" cx="100" cy="100" r="58" />
      <text className="holders-total" x="100" y="102" textAnchor="middle" style={{ fontSize: `${Math.min(22, 150 / Math.max(1, center.length))}px` }}>{center}</text>
      <text className="holders-unit" x="100" y="120" textAnchor="middle">{unit}</text>
    </svg>
    <figcaption><ul>{arcs.map((arc) => <li key={arc.label}><i style={{ background: arc.color }} /><span>{arc.label}</span><b>{moneyDivide(moneyMultiply(arc.value, 100), total, 2)}%</b><small>{formatQuantity(arc.value, suffix === "표" ? 2 : quantityDigits)}{suffix}</small></li>)}</ul></figcaption>
  </figure>;
}

/** A ring gauge filled clockwise from the top by `ratio` (0–1), with a centre figure. */
export function Gauge({ ratio, center, label }: { ratio: number; center: string; label?: string }) {
  const fill = Math.max(0, Math.min(1, ratio)) * 314;
  return <svg className="gauge" viewBox="0 0 120 120" role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
    <circle cx="60" cy="60" r="50" className="gauge-track" />
    <circle cx="60" cy="60" r="50" className="gauge-fill" strokeDasharray={`${fill} 314`} transform="rotate(-90 60 60)" />
    <text x="60" y="66" textAnchor="middle" style={{ fontSize: `${Math.min(23, 80 / Math.max(1, center.length))}px` }}>{center}</text>
  </svg>;
}

/** The largest six as slices, the rest as one, plus an optional remainder slice. */
export function topSlices(items: { label: string; value: Money }[], rest: string, remainder?: Slice): Slice[] {
  const others = items.slice(6).reduce<Money>((sum, item) => moneyAdd(sum, item.value), "0");
  return [...items.slice(0, 6).map((item, index) => ({ ...item, color: SLICE_COLORS[index] })),
    ...(moneyCompare(others, 0) > 0 ? [{ label: rest, value: others, color: SLICE_COLORS[6] }] : []), ...(remainder && moneyCompare(remainder.value, 0) > 0 ? [remainder] : [])];
}


export type VotingDistribution = { totalVotingRights: Money; votingHolders: { userId: string; name: string; votingRights: Money }[] };
export type EffectiveVotingDistribution = { totalVotingRights: Money; effectiveVotingHolders: { userId: string; name: string; votingRights: Money }[] };
export type Shareholders = { totalShares: number; holders: { userId: string; name: string; quantity: number }[]; totalVotingRights?: Money; votingHolders?: VotingDistribution["votingHolders"]; effectiveVotingHolders?: EffectiveVotingDistribution["effectiveVotingHolders"] };

/** Who owns a company or stock: the six largest holders, the rest together, and shares no one holds. */
export function ShareholderPie({ data }: { data: Shareholders }) {
  const held = data.holders.reduce((sum, item) => sum + item.quantity, 0);
  const total = Math.max(data.totalShares, held);
  const slices = topSlices(data.holders.map((item) => ({ label: item.name, value: item.quantity })), "기타 주주",
    { label: "미보유(유통 전)", value: total - held, color: "var(--chip)" });
  return <Donut slices={slices} total={total} center={number(total)} unit="발행 주식" label="주주 구성 원그래프" suffix="주" />;
}

export function VotingRightsPie({ data, effective = false }: { data: Shareholders; effective?: boolean }) {
  const holders = effective ? data.effectiveVotingHolders : data.votingHolders;
  const total = data.totalVotingRights ?? "0";
  if (data.totalVotingRights === undefined || holders === undefined) return <p className="empty">의결권 자료를 불러오지 못했습니다. 화면을 새로고침해 주세요.</p>;
  if (!holders.length || moneyCompare(total, 0) <= 0) return <p className="empty">표시할 의결권이 없습니다.</p>;
  const slices = topSlices(holders.map((holder) => ({ label: holder.name, value: holder.votingRights })), "기타 의결권");
  return <Donut slices={slices} total={total} center={number(total)} unit="의결권" label={effective ? "실질 의결권 원그래프" : "의결권 원그래프"} suffix="표" />;
}
