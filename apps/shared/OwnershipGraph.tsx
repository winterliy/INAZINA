import "./OwnershipGraph.css";

export type OwnershipGraphData = {
  nodes: { id: string; kind: string; code: string; name: string }[];
  edges: { from: string; to: string; equity_percent: string; voting_percent: string; root_equity_percent: string; root_voting_percent: string; depth: number; direct?: boolean; cycle?: boolean }[];
  beneficial_owners?: { id: string; name: string; root_equity_percent: string; root_voting_percent: string }[];
  truncated?: boolean;
  root_kind?: "user" | "organization";
};

const CARD_WIDTH = 228;
const CARD_HEIGHT = 92;
const LEVEL_GAP = 154;
const NODE_GAP = 28;

function percent(value: string | undefined) {
  const number = Number(value);
  return Number.isFinite(number) ? `${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(number)}%` : "—";
}

export function OwnershipGraph({ graph }: { graph?: OwnershipGraphData }) {
  if (!graph?.nodes.length) return <p className="empty">지분 자료가 없거나 표시할 주주가 없습니다.</p>;

  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
  const markerId = `ownership-arrow-${graph.root_kind ?? "graph"}-${graph.nodes[0]?.id.replace(/[^a-zA-Z0-9_-]/g, "-") ?? "root"}`;
  const userRoot = graph.root_kind === "user" ? graph.nodes.find((node) => node.kind === "user") : undefined;
  const organizationRoot = graph.nodes.find((node) => node.kind === "company" && graph.edges.some((edge) => edge.to === node.id));
  const root = userRoot ?? organizationRoot;
  const levels = new Map<string, number>();
  if (root) levels.set(root.id, 0);
  for (let depth = 0; depth < 6; depth += 1) {
    for (const edge of graph.edges) {
      if (userRoot && levels.has(edge.from) && !levels.has(edge.to)) levels.set(edge.to, levels.get(edge.from)! + 1);
      if (!userRoot && levels.has(edge.to) && !levels.has(edge.from)) levels.set(edge.from, levels.get(edge.to)! + 1);
    }
  }

  const maxLevel = Math.max(0, ...levels.values());
  const grouped = new Map<number, string[]>();
  for (const node of graph.nodes) {
    const logicalLevel = levels.get(node.id) ?? 1;
    const visualLevel = userRoot ? logicalLevel : maxLevel - logicalLevel;
    grouped.set(visualLevel, [...(grouped.get(visualLevel) ?? []), node.id]);
  }
  const positions = new Map<string, { x: number; y: number }>();
  for (const [level, ids] of grouped) {
    const rowWidth = ids.length * CARD_WIDTH + Math.max(0, ids.length - 1) * NODE_GAP;
    const width = Math.max(CARD_WIDTH, ...[...grouped.values()].map((items) => items.length * CARD_WIDTH + Math.max(0, items.length - 1) * NODE_GAP));
    const startX = (width - rowWidth) / 2;
    ids.forEach((id, index) => positions.set(id, { x: startX + index * (CARD_WIDTH + NODE_GAP), y: 20 + level * LEVEL_GAP }));
  }
  const width = Math.max(CARD_WIDTH, ...[...grouped.values()].map((items) => items.length * CARD_WIDTH + Math.max(0, items.length - 1) * NODE_GAP));
  const height = 40 + (Math.max(0, ...grouped.keys()) * LEVEL_GAP) + CARD_HEIGHT;

  return <figure className="ownership-diagram" aria-label={userRoot ? "개인 보유 기업 지배구조" : "기업 지분 및 의결권 구조"}>
    {graph.truncated && <p className="hint">표시 한도 때문에 일부 소수 지분이나 하위 기업이 생략됐습니다.</p>}
    <div className="ownership-diagram-scroll" role="group" aria-label="소유 관계 도식. 가로 스크롤로 전체 구조를 볼 수 있습니다.">
      <div className="ownership-diagram-canvas" style={{ width, height }}>
        <svg className="ownership-diagram-edges" width={width} height={height} aria-hidden="true">
          <defs><marker id={markerId} markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L8,4 L0,8 z" /></marker></defs>
          {graph.edges.map((edge, index) => {
            const from = positions.get(edge.from);
            const to = positions.get(edge.to);
            if (!from || !to) return null;
            const x1 = from.x + CARD_WIDTH / 2;
            const y1 = from.y + CARD_HEIGHT;
            const x2 = to.x + CARD_WIDTH / 2;
            const y2 = to.y;
            const midY = (y1 + y2) / 2;
            const label = `${percent(edge.depth === 0 ? edge.equity_percent : edge.root_equity_percent)} 경제 · ${percent(edge.depth === 0 ? edge.voting_percent : edge.root_voting_percent)} 의결`;
            return <g key={`${edge.from}-${edge.to}-${edge.depth}-${index}`} className={edge.cycle ? "ownership-edge cycle" : "ownership-edge"}>
              <path d={`M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`} markerEnd={`url(#${markerId})`} />
              <text x={(x1 + x2) / 2} y={midY - 4}>{edge.cycle ? "순환 보유 · " : edge.depth > 0 ? "경로 합산 · " : ""}{label}</text>
            </g>;
          })}
        </svg>
        {[...grouped.entries()].sort(([a], [b]) => a - b).flatMap(([level, ids]) => ids.map((id) => {
          const node = nodeById.get(id)!;
          const point = positions.get(id)!;
          const connections = graph.edges.filter((edge) => edge.from === id);
          return <article className={`ownership-node ${node.kind === "user" ? "person" : "company"}`} key={id} style={{ left: point.x, top: point.y }}>
            <span className="ownership-node-kind">{node.kind === "company" ? "법인" : "개인"} · {level + 1}단계</span>
            <strong>{node.name}</strong>
            <small>{connections.length ? `${connections.length}개 보유 관계` : node.code}</small>
          </article>;
        }))}
      </div>
    </div>
    {!!graph.beneficial_owners?.length && <details className="help ownership-beneficial">
      <summary>경로 말단 보유자 합산 (참고치)</summary>
      <p className="hint">최대 5단계 단순 경로 기여의 합입니다. 순환 출자에 대한 최종 지배 판정은 아닙니다.</p>
      <ul>{graph.beneficial_owners.map((owner) => <li key={owner.id}>{owner.name} · 경제 {percent(owner.root_equity_percent)} · 의결 {percent(owner.root_voting_percent)}</li>)}</ul>
    </details>}
  </figure>;
}
