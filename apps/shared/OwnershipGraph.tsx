import { useEffect, useRef, useState } from "react";
import "./OwnershipGraph.css";

export type OwnershipGraphData = {
  nodes: { id: string; kind: string; code: string; name: string }[];
  edges: { from: string; to: string; equity_percent: string; voting_percent: string; root_equity_percent: string; root_voting_percent: string; depth: number; direct?: boolean; cycle?: boolean }[];
  beneficial_owners?: { id: string; name: string; root_equity_percent: string; root_voting_percent: string }[];
  truncated?: boolean;
  root_kind?: "user" | "organization";
  root_id?: string;
};

const CARD_WIDTH = 280;
const CARD_HEIGHT = 144;
const LEVEL_GAP = 360;
const NODE_GAP = 200;
const RADIAL_LAYOUT_MIN_NODES = 7;
const RADIAL_LAYOUT_MIN_EDGES = 8;

function percent(value: string | undefined) {
  const number = Number(value);
  return Number.isFinite(number) ? `${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(number)}%` : "—";
}

export function OwnershipGraph({ graph, fullScreen = false, focusedNodeId, focusRequest = 0 }: { graph?: OwnershipGraphData; fullScreen?: boolean; focusedNodeId?: string; focusRequest?: number }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [viewportWidth, setViewportWidth] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [showAllRelationships, setShowAllRelationships] = useState(false);
  useEffect(() => setShowAllRelationships(false), [graph]);
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setViewportWidth(element.clientWidth));
    observer.observe(element);
    setViewportWidth(element.clientWidth);
    return () => observer.disconnect();
  }, [graph?.nodes.length, fullScreen]);
  useEffect(() => {
    if (!focusedNodeId) return;
    const container = scrollRef.current;
    const node = container ? [...container.querySelectorAll<HTMLElement>("[data-node-id]")].find((item) => item.dataset.nodeId === focusedNodeId) : undefined;
    if (!container || !node) return;
    const containerRect = container.getBoundingClientRect();
    const nodeRect = node.getBoundingClientRect();
    const left = container.scrollLeft + nodeRect.left - containerRect.left - (container.clientWidth - nodeRect.width) / 2;
    const top = container.scrollTop + nodeRect.top - containerRect.top - (container.clientHeight - nodeRect.height) / 2;
    container.scrollTo({ left: Math.max(0, left), top: Math.max(0, top), behavior: "smooth" });
    node.classList.add("ownership-node-focused");
    const timeout = window.setTimeout(() => node.classList.remove("ownership-node-focused"), 1500);
    return () => {
      window.clearTimeout(timeout);
      node.classList.remove("ownership-node-focused");
    };
  }, [focusedNodeId, focusRequest, graph?.nodes.length, viewportWidth, zoom]);
  // A company with no holders is a single node and no edge: show the empty note, not a blank canvas.
  if (!graph?.nodes.length || !graph.edges.length) return <p className="empty">지분 자료가 없거나 표시할 주주가 없습니다.</p>;

  // Every edge is a direct holding between its two companies. The root
  // percentages are path aggregates and belong in the detail list, not labels.
  const edges = graph.edges;
  const incomingByTarget = new Map<string, typeof edges>();
  const outgoingBySource = new Map<string, typeof edges>();
  for (const edge of edges) {
    const incoming = incomingByTarget.get(edge.to) ?? [];
    incoming.push(edge);
    incomingByTarget.set(edge.to, incoming);
    const outgoing = outgoingBySource.get(edge.from) ?? [];
    outgoing.push(edge);
    outgoingBySource.set(edge.from, outgoing);
  }
  const usedNodeIds = new Set<string>();
  for (const edge of edges) { usedNodeIds.add(edge.from); usedNodeIds.add(edge.to); }
  const nodes = graph.nodes.filter((node) => usedNodeIds.has(node.id));
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const markerId = `ownership-arrow-${graph.root_kind ?? "graph"}-${graph.nodes[0]?.id.replace(/[^a-zA-Z0-9_-]/g, "-") ?? "root"}`;
  const userRoot = graph.root_kind === "user" ? nodes.find((node) => node.kind === "user") : undefined;
  const organizationRoot = nodes.find((node) => node.kind === "company" && incomingByTarget.has(node.id));
  const root = nodes.find((node) => node.id === graph.root_id) ?? userRoot ?? organizationRoot;
  const levels = new Map<string, number>();
  if (root) {
    levels.set(root.id, 0);
    const queue = [root.id];
    for (let index = 0; index < queue.length; index += 1) {
      const current = queue[index];
      const adjacent = userRoot ? outgoingBySource.get(current) : incomingByTarget.get(current);
      for (const edge of adjacent ?? []) {
        const next = userRoot ? edge.to : edge.from;
        if (levels.has(next)) continue;
        levels.set(next, levels.get(current)! + 1);
        queue.push(next);
      }
    }
  }

  const maxLevel = Math.max(0, ...levels.values());
  // Dense maps default to a dominant-control spanning tree. The full network
  // stays available on demand; dense maps use radial placement around the root.
  const complex = nodes.length >= 6 && edges.length >= nodes.length + 4;
  const dominantEdgeKeys = new Set<string>();
  if (complex) {
    for (const node of nodes) {
      const level = levels.get(node.id);
      if (level === undefined || level === 0) continue;
      const candidates = userRoot ? incomingByTarget.get(node.id) ?? [] : outgoingBySource.get(node.id) ?? [];
      const parentLevel = level - 1;
      const parentCandidates = candidates.filter((edge) =>
        levels.get(userRoot ? edge.from : edge.to) === parentLevel);
      parentCandidates.sort((a, b) =>
        Math.max(Number(b.voting_percent) || 0, Number(b.equity_percent) || 0)
        - Math.max(Number(a.voting_percent) || 0, Number(a.equity_percent) || 0));
      const parent = parentCandidates[0];
      if (parent) dominantEdgeKeys.add(`${parent.from}\u0000${parent.to}`);
    }
  }
  const simplified = complex && !showAllRelationships;
  const edgesToRender = simplified ? edges.filter((edge) => dominantEdgeKeys.has(`${edge.from}\u0000${edge.to}`)) : edges;
  const dominantParentByNode = new Map<string, string>();
  for (const edge of edges) {
    if (!dominantEdgeKeys.has(`${edge.from}\u0000${edge.to}`)) continue;
    dominantParentByNode.set(userRoot ? edge.to : edge.from, userRoot ? edge.from : edge.to);
  }
  const visibleNodeIds = new Set<string>(root ? [root.id] : []);
  for (const edge of edgesToRender) { visibleNodeIds.add(edge.from); visibleNodeIds.add(edge.to); }
  const nodesToRender = simplified ? nodes.filter((node) => visibleNodeIds.has(node.id)) : nodes;
  const radial = Boolean(root && nodesToRender.length >= RADIAL_LAYOUT_MIN_NODES
    && edgesToRender.length >= RADIAL_LAYOUT_MIN_EDGES);
  const forceHierarchy = Boolean(!radial && !userRoot && root && complex);
  const bundled = Boolean(!radial && !userRoot && root && complex);
  const layerGap = forceHierarchy ? 88 : NODE_GAP;
  const layoutMaxLevel = Math.max(0, ...nodesToRender.map((node) => levels.get(node.id) ?? 1));
  const grouped = new Map<number, string[]>();
  for (const node of nodesToRender) {
    const logicalLevel = levels.get(node.id) ?? 1;
    const visualLevel = radial || userRoot || forceHierarchy
      ? logicalLevel : (simplified ? layoutMaxLevel : maxLevel) - logicalLevel;
    const group = grouped.get(visualLevel) ?? [];
    group.push(node.id);
    grouped.set(visualLevel, group);
  }
  const intrinsicWidth = Math.max(CARD_WIDTH, ...[...grouped.values()].map((items) => items.length * CARD_WIDTH + Math.max(0, items.length - 1) * layerGap));
  const maxGroupedLevel = Math.max(0, ...grouped.keys());
  const ringRadii = new Map<number, number>();
  let maxRadius = 0;
  if (radial) {
    for (let level = 1; level <= maxGroupedLevel; level += 1) {
      const count = grouped.get(level)?.length ?? 0;
      const radiusForSpacing = count * (CARD_WIDTH + 120) / (2 * Math.PI);
      maxRadius = Math.max(LEVEL_GAP * level, maxRadius + CARD_WIDTH + NODE_GAP, radiusForSpacing);
      ringRadii.set(level, maxRadius);
    }
  }
  const radialDiameter = radial ? maxRadius * 2 + CARD_WIDTH + 80 : 0;
  const width = fullScreen ? Math.max(radial ? radialDiameter : intrinsicWidth, viewportWidth) : (radial ? radialDiameter : intrinsicWidth);
  const positions = new Map<string, { x: number; y: number }>();
  const radialCenter = { x: width / 2, y: maxRadius + CARD_HEIGHT / 2 + 40 };
  const hierarchyRootCenterY = maxGroupedLevel * LEVEL_GAP + CARD_HEIGHT / 2 + 40;
  let height = forceHierarchy ? hierarchyRootCenterY * 2 : 40 + maxGroupedLevel * LEVEL_GAP + CARD_HEIGHT;
  if (radial) {
    height = radialDiameter;
    positions.set(root!.id, { x: radialCenter.x - CARD_WIDTH / 2, y: radialCenter.y - CARD_HEIGHT / 2 });
    for (const [level, ids] of grouped) {
      if (level === 0) continue;
      const radius = ringRadii.get(level)!;
      ids.forEach((id, index) => {
        const angle = -Math.PI / 2 + index * (Math.PI * 2 / ids.length);
        positions.set(id, { x: radialCenter.x + Math.cos(angle) * radius - CARD_WIDTH / 2,
          y: radialCenter.y + Math.sin(angle) * radius - CARD_HEIGHT / 2 });
      });
    }
  } else {
    for (const [level, ids] of grouped) {
      const rowWidth = ids.length * CARD_WIDTH + Math.max(0, ids.length - 1) * layerGap;
      const startX = (width - rowWidth) / 2;
      const y = forceHierarchy
        ? hierarchyRootCenterY - level * LEVEL_GAP - CARD_HEIGHT / 2
        : 20 + level * LEVEL_GAP;
      ids.forEach((id, index) => positions.set(id, { x: startX + index * (CARD_WIDTH + layerGap), y }));
    }
    if (forceHierarchy && root) {
      const rootPosition = positions.get(root.id);
      if (rootPosition) rootPosition.x = width / 2 - CARD_WIDTH / 2;
      // Layered force relaxation: each company attracts its strongest parent
      // toward the same horizontal lane, while fixed spacing pushes cards apart.
      const centerX = new Map([...positions].map(([id, point]) => [id, point.x + CARD_WIDTH / 2]));
      for (let pass = 0; pass < 8; pass += 1) {
        for (let level = 1; level <= maxGroupedLevel; level += 1) {
          const ids = grouped.get(level) ?? [];
          const attracted = ids.map((id) => {
            const parentId = dominantParentByNode.get(id);
            const current = centerX.get(id) ?? width / 2;
            const parent = parentId ? centerX.get(parentId) : undefined;
            return { id, desired: parent === undefined ? current : current * .55 + parent * .45 };
          }).sort((a, b) => a.desired - b.desired || nodeById.get(a.id)!.name.localeCompare(nodeById.get(b.id)!.name, "ko"));
          if (!attracted.length) continue;
          const mean = attracted.reduce((sum, item) => sum + item.desired, 0) / attracted.length;
          const rowWidth = attracted.length * CARD_WIDTH + (attracted.length - 1) * layerGap;
          const first = mean - rowWidth / 2 + CARD_WIDTH / 2;
          attracted.forEach((item, index) => centerX.set(item.id, first + index * (CARD_WIDTH + layerGap)));
        }
      }
      for (const [id, x] of centerX) {
        const point = positions.get(id);
        if (point) point.x = x - CARD_WIDTH / 2;
      }
    }
  }
  const edgeBundles = new Map<string, { targetId: string; edges: typeof edges }>();
  if (bundled) {
    for (const edge of edgesToRender) {
      if (edge.cycle || levels.get(edge.from) === levels.get(edge.to)) continue;
      const key = `${edge.to}:${levels.get(edge.from)}:${levels.get(edge.to)}`;
      const bundle = edgeBundles.get(key) ?? { targetId: edge.to, edges: [] };
      bundle.edges.push(edge);
      edgeBundles.set(key, bundle);
    }
  }
  const bundledEdgeKeys = new Set<string>();
  const edgeBundlePaths = [...edgeBundles.values()].flatMap((bundle) => {
    if (bundle.edges.length < 2) return [];
    bundle.edges.forEach((edge) => bundledEdgeKeys.add(`${edge.from}\u0000${edge.to}`));
    const target = positions.get(bundle.targetId);
    const sources = [...new Set(bundle.edges.map((edge) => edge.from))]
      .map((id) => ({ id, point: positions.get(id) }))
      .filter((item): item is { id: string; point: { x: number; y: number } } => Boolean(item.point));
    if (!target || !sources.length) return [];
    const sourceCenterY = sources.reduce((sum, item) => sum + item.point.y + CARD_HEIGHT / 2, 0) / sources.length;
    const targetCenterY = target.y + CARD_HEIGHT / 2;
    const sourcesAboveTarget = sourceCenterY < targetCenterY;
    const busY = (sourceCenterY + targetCenterY) / 2;
    const targetX = target.x + CARD_WIDTH / 2;
    const sourceXs = sources.map((item) => item.point.x + CARD_WIDTH / 2);
    const left = Math.min(targetX, ...sourceXs), right = Math.max(targetX, ...sourceXs);
    const branches = sources.map((item) => {
      const sourceX = item.point.x + CARD_WIDTH / 2;
      const sourceY = sourcesAboveTarget ? item.point.y + CARD_HEIGHT : item.point.y;
      return `M ${sourceX} ${sourceY} L ${sourceX} ${busY}`;
    });
    const targetY = sourcesAboveTarget ? target.y : target.y + CARD_HEIGHT;
    return [{ branches, bus: `M ${left} ${busY} L ${right} ${busY}`,
      trunk: `M ${targetX} ${busY} L ${targetX} ${targetY}` }];
  });

  return <figure className={`ownership-diagram${fullScreen ? " ownership-diagram-full" : ""}${radial ? " ownership-diagram-radial" : ""}`} aria-label={userRoot ? "개인 보유 기업 지배구조" : "기업 지분 및 의결권 구조"}>
    {graph.truncated && <p className="hint">표시 한도 때문에 일부 소수 지분이나 하위 기업이 생략됐습니다.</p>}
    {complex && <div className="ownership-layout-controls" role="group" aria-label="조직도 관계 표시">
      <button type="button" aria-pressed={simplified} onClick={() => setShowAllRelationships(false)}>주요 지배 경로</button>
      <button type="button" aria-pressed={showAllRelationships} onClick={() => setShowAllRelationships(true)}>전체 지분 관계 · {edges.length}</button>
    </div>}
    {simplified && <p className="ownership-layout-hint">각 법인의 가장 강한 의결권 경로를 표시합니다. 다른 관계는 전체 지분 관계에서 확인할 수 있습니다.</p>}
    {bundled && !simplified && <p className="ownership-layout-hint">같은 법인으로 향하는 주주 관계는 공통 연결선으로 묶어 표시합니다. 카드에는 대표 관계만 표시합니다.</p>}
    <div className="ownership-zoom-controls" role="group" aria-label="조직도 확대·축소">
      <button type="button" aria-label="조직도 축소" disabled={zoom <= 0.1} onClick={() => setZoom((value) => Math.max(0.1, Math.round((value - 0.1) * 10) / 10))}>−</button>
      <output aria-live="polite">{Math.round(zoom * 100)}%</output>
      <button type="button" aria-label="조직도 확대" disabled={zoom >= 1.6} onClick={() => setZoom((value) => Math.min(1.6, Math.round((value + 0.1) * 10) / 10))}>+</button>
      <button type="button" className="ownership-zoom-reset" onClick={() => setZoom(1)}>초기화</button>
    </div>
    <div ref={scrollRef} className="ownership-diagram-scroll" role="group" aria-label="소유 관계 도식. 가로 스크롤로 전체 구조를 볼 수 있습니다.">
      <div className="ownership-diagram-zoom-layer" style={{ width: Math.max(viewportWidth, width * zoom), height: height * zoom }}>
      <div className="ownership-diagram-canvas" style={{ width, height, transform: `translateX(${Math.max(0, (Math.max(viewportWidth, width * zoom) - width * zoom) / 2)}px) scale(${zoom})` }}>
        <svg className="ownership-diagram-edges" width={width} height={height} aria-hidden="true">
          <defs><marker id={markerId} markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L8,4 L0,8 z" /></marker></defs>
          {edgesToRender.map((edge, index) => {
            const from = positions.get(edge.from);
            const to = positions.get(edge.to);
            if (!from || !to) return null;
            if (bundledEdgeKeys.has(`${edge.from}\u0000${edge.to}`)) return null;
            if (radial) {
              const fromCenter = { x: from.x + CARD_WIDTH / 2, y: from.y + CARD_HEIGHT / 2 };
              const toCenter = { x: to.x + CARD_WIDTH / 2, y: to.y + CARD_HEIGHT / 2 };
              const dx = toCenter.x - fromCenter.x, dy = toCenter.y - fromCenter.y;
              const boundaryScale = 1 / Math.max(Math.abs(dx) / (CARD_WIDTH / 2), Math.abs(dy) / (CARD_HEIGHT / 2), 1e-6);
              const start = { x: fromCenter.x + dx * boundaryScale, y: fromCenter.y + dy * boundaryScale };
              const end = { x: toCenter.x - dx * boundaryScale, y: toCenter.y - dy * boundaryScale };
              const fromRadius = Math.hypot(fromCenter.x - radialCenter.x, fromCenter.y - radialCenter.y);
              const toRadius = Math.hypot(toCenter.x - radialCenter.x, toCenter.y - radialCenter.y);
              const angleFrom = Math.atan2(fromCenter.y - radialCenter.y, fromCenter.x - radialCenter.x);
              const angleTo = Math.atan2(toCenter.y - radialCenter.y, toCenter.x - radialCenter.x);
              const deltaAngle = ((angleTo - angleFrom + Math.PI * 3) % (Math.PI * 2)) - Math.PI;
              const controlAngle = fromRadius < 1 ? angleTo : angleFrom + deltaAngle / 2;
              const controlRadius = Math.abs(fromRadius - toRadius) < 1
                ? Math.max(fromRadius, toRadius) + 120 : (fromRadius + toRadius) / 2;
              const control = { x: radialCenter.x + Math.cos(controlAngle) * controlRadius,
                y: radialCenter.y + Math.sin(controlAngle) * controlRadius };
              const path = `M ${start.x} ${start.y} Q ${control.x} ${control.y} ${end.x} ${end.y}`;
              return <g key={`${edge.from}-${edge.to}-${edge.depth}-${index}`} className={edge.cycle ? "ownership-edge cycle" : "ownership-edge"}>
                <path d={path} markerEnd={`url(#${markerId})`} />
              </g>;
            }
            const sameLevel = from.y === to.y;
            const forward = from.x < to.x;
            const x1 = from.x + CARD_WIDTH / 2;
            const y1 = from.y + CARD_HEIGHT;
            const x2 = to.x + CARD_WIDTH / 2;
            const y2 = to.y;
            const midY = (y1 + y2) / 2;
            const startX = sameLevel ? (forward ? from.x + CARD_WIDTH : from.x) : x1;
            const endX = sameLevel ? (forward ? to.x : to.x + CARD_WIDTH) : x2;
            const sameRowY = from.y + CARD_HEIGHT / 2;
            const path = sameLevel
              ? `M ${startX} ${sameRowY} C ${startX + (endX - startX) * .25} ${sameRowY - 42}, ${startX + (endX - startX) * .75} ${sameRowY - 42}, ${endX} ${sameRowY}`
              : `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`;
            const edgeClass = edge.cycle ? "ownership-edge cycle" : bundled && sameLevel ? "ownership-edge cross" : "ownership-edge";
            return <g key={`${edge.from}-${edge.to}-${edge.depth}-${index}`} className={edgeClass}>
              <path d={path} markerEnd={`url(#${markerId})`} />
            </g>;
          })}
          {edgeBundlePaths.map((bundle, index) => <g key={`bundle-${index}`} className="ownership-edge-bundle">
            {bundle.branches.map((path, branchIndex) => <path key={branchIndex} d={path} />)}
            <path d={bundle.bus} />
            <path className="ownership-edge-bundle-trunk" d={bundle.trunk} markerEnd={`url(#${markerId})`} />
          </g>)}
        </svg>
        {[...grouped.entries()].sort(([a], [b]) => a - b).flatMap(([level, ids]) => ids.map((id) => {
          const node = nodeById.get(id)!;
          const point = positions.get(id)!;
          const connections = outgoingBySource.get(id) ?? [];
          const ownerships = [...(incomingByTarget.get(id) ?? [])].sort((a, b) =>
            Math.max(Number(b.voting_percent) || 0, Number(b.equity_percent) || 0)
            - Math.max(Number(a.voting_percent) || 0, Number(a.equity_percent) || 0));
          return <article data-node-id={id} className={`ownership-node ${node.kind === "user" ? "person" : "company"}${!userRoot && node.id === root?.id ? " root" : ""}`} key={id} style={{ left: point.x, top: point.y }}>
            <span className="ownership-node-kind">{node.kind === "company" ? "법인" : "개인"} · {level + 1}단계</span>
            <strong>{node.name}</strong>
            {ownerships.length ? <div className="ownership-node-ownerships">{ownerships.slice(0, 3).map((edge) => <small key={`${edge.from}-${edge.to}`}>
              {nodeById.get(edge.from)?.name ?? edge.from} · 경제 {percent(edge.equity_percent)} · 의결 {percent(edge.voting_percent)}{edge.cycle ? " · 순환 보유" : ""}
            </small>)}{ownerships.length > 3 && <small>외 {ownerships.length - 3}개 보유 관계</small>}</div> : <small>{connections.length ? `${connections.length}개 보유 관계` : node.code}</small>}
          </article>;
        }))}
      </div>
      </div>
    </div>
    {!!graph.beneficial_owners?.length && <details className="help ownership-beneficial">
      <summary>경로 말단 보유자 합산 (참고치)</summary>
      <p className="hint">최대 5단계 단순 경로 기여의 합입니다. 순환 출자에 대한 최종 지배 판정은 아닙니다.</p>
      <ul>{graph.beneficial_owners.map((owner) => <li key={owner.id}>{owner.name} · 경제 {percent(owner.root_equity_percent)} · 의결 {percent(owner.root_voting_percent)}</li>)}</ul>
    </details>}
  </figure>;
}
