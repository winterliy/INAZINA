import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../shared/api";
import { OwnershipGraph, type OwnershipGraphData } from "../../shared/OwnershipGraph";

type GraphNode = OwnershipGraphData["nodes"][number];
type TreeNode = GraphNode & { children: TreeNode[] };
type OrganizationOption = { organization_code: string; name: string; industry: string };

function PersonLabel({ name }: { name: string }) {
  return <span className="ownership-tree-person"><svg className="ownership-tree-person-icon" aria-hidden="true" viewBox="0 0 16 16"><circle cx="8" cy="5" r="2.5" /><path d="M3 14c.4-3 2.1-4.5 5-4.5s4.6 1.5 5 4.5" /></svg>{name}</span>;
}

function ownershipTree(graph?: OwnershipGraphData) {
  if (!graph) return [] as TreeNode[];
  const nodes = new Map<string, TreeNode>(graph.nodes.map((node) => [node.id, { ...node, children: [] as TreeNode[] }] as const));
  const children = new Set<string>();
  const outgoing = new Map<string, { node: TreeNode; voting: number; equity: number }[]>();
  for (const edge of graph.edges) {
    const parent = nodes.get(edge.from);
    const child = nodes.get(edge.to);
    if (!parent || !child || parent.id === child.id) continue;
    const relations = outgoing.get(parent.id) ?? [];
    if (!relations.some((item) => item.node.id === child.id)) {
      relations.push({ node: child, voting: Number(edge.voting_percent) || 0, equity: Number(edge.equity_percent) || 0 });
      outgoing.set(parent.id, relations);
    }
    children.add(child.id);
  }
  for (const relations of outgoing.values()) relations.sort((a, b) =>
    b.voting - a.voting || b.equity - a.equity || a.node.name.localeCompare(b.node.name, "ko"));

  // Cross-holdings make the graph a network, not a tree. Rendering every simple
  // path duplicates shared companies exponentially. Build a spanning forest so
  // each company appears once in the sidebar while the diagram keeps every edge.
  const visited = new Set<string>();
  const append = (node: TreeNode) => {
    if (visited.has(node.id)) return;
    visited.add(node.id);
    for (const relation of outgoing.get(node.id) ?? []) {
      if (visited.has(relation.node.id)) continue;
      node.children.push(relation.node);
      append(relation.node);
    }
  };
  const roots = [...nodes.values()].filter((node) => !children.has(node.id)).sort((a, b) => a.name.localeCompare(b.name, "ko"));
  for (const node of roots) append(node);
  for (const node of [...nodes.values()].sort((a, b) => a.name.localeCompare(b.name, "ko"))) {
    if (!visited.has(node.id)) { roots.push(node); append(node); }
  }
  return roots;
}

function TreeItem({ node, selected, onSelect, ancestors = new Set<string>() }: {
  node: TreeNode; selected: string; onSelect: (code: string) => void; ancestors?: Set<string>;
}) {
  const cycle = ancestors.has(node.id);
  const nextAncestors = new Set(ancestors).add(node.id);
  const select = () => { if (node.kind === "company") onSelect(node.code); };
  const title = node.name || node.code;
  if (!node.children.length || cycle) return <li className="ownership-tree-leaf-row">
    {node.kind === "company"
      ? <button type="button" className={selected === node.code ? "ownership-tree-company active" : "ownership-tree-company"} onClick={select} title={`${title} (${node.code})`}><span aria-hidden="true">▣</span>{title}</button>
      : <PersonLabel name={title} />}
  </li>;
  return <li>
    <details className="ownership-tree-folder" open={node.kind === "user"}>
      <summary>
        {node.kind === "company"
          ? <><span className="ownership-tree-icon" aria-hidden="true">▰</span><button type="button" className={selected === node.code ? "ownership-tree-company active" : "ownership-tree-company"} onClick={(event) => { event.preventDefault(); event.stopPropagation(); select(); }} title={`${title} (${node.code})`}>{title}</button></>
          : <PersonLabel name={title} />}
      </summary>
      <ul>{node.children.map((child) => <TreeItem key={child.id} node={child} selected={selected} onSelect={onSelect} ancestors={nextAncestors} />)}</ul>
    </details>
  </li>;
}

function LoadingGraph() {
  return <div className="ownership-loading" role="status" aria-live="polite">
    <span className="ownership-spinner" aria-hidden="true" />
    <strong>지분·의결권 조직도를 불러오는 중…</strong>
    <div className="ownership-skeleton" aria-hidden="true"><i /><i /><i /></div>
  </div>;
}

export function OrganizationOwnershipWindow({ route }: { route: string }) {
  const [routeType = "", initialCode = "", initialDetail = ""] = route.split("/").filter(Boolean);
  const personal = routeType === "personal";
  const [graph, setGraph] = useState<OwnershipGraphData>();
  const [organizations, setOrganizations] = useState<OrganizationOption[]>([]);
  const [selected, setSelected] = useState(initialCode);
  const [focusedNodeId, setFocusedNodeId] = useState(initialCode ? `company:${initialCode}` : "");
  const [focusRequest, setFocusRequest] = useState(0);
  const [includeMinor, setIncludeMinor] = useState(initialDetail === "minor");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [organizationSearch, setOrganizationSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestId = useRef(0);
  const tree = useMemo(() => ownershipTree(graph), [graph]);
  const organizationGroups = useMemo(() => {
    const groups = new Map<string, OrganizationOption[]>();
    for (const organization of organizations) {
      const industry = organization.industry || "산업 미지정";
      groups.set(industry, [...(groups.get(industry) ?? []), organization]);
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b, "ko"));
  }, [organizations]);
  const filteredOrganizations = useMemo(() => {
    const term = organizationSearch.trim().toLocaleLowerCase("ko");
    return term ? organizations.filter((organization) => `${organization.name} ${organization.organization_code} ${organization.industry}`.toLocaleLowerCase("ko").includes(term)) : [];
  }, [organizationSearch, organizations]);

  useEffect(() => {
    document.title = personal ? "보유 기업 지배구조 · INAZINA" : "기업 지분·의결권 조직도 · INAZINA";
    return () => { document.title = "INAZINA Dashboard"; };
  }, [personal]);

  const loadOrganization = useCallback(async (code: string, showMinor: boolean, updateAddress = true) => {
    const id = ++requestId.current;
    setSelected(code);
    setFocusedNodeId(`company:${code}`);
    setFocusRequest((value) => value + 1);
    setIncludeMinor(showMinor);
    setLoading(true);
    setError("");
    setGraph(undefined);
    if (updateAddress) history.replaceState(null, "", `${location.pathname}${location.search}#ownership/organization/${encodeURIComponent(code)}${showMinor ? "/minor" : ""}`);
    try {
      const suffix = showMinor ? "?includeMinor=1" : "";
      const result = await api<{ graph?: OwnershipGraphData }>(`/api/admin/organizations/${encodeURIComponent(code)}${suffix}`);
      if (id === requestId.current) {
        if (!result.graph) throw new Error("선택한 법인의 지분 자료가 없습니다.");
        setGraph(result.graph);
      }
    } catch (cause) {
      if (id === requestId.current) setError(cause instanceof Error ? cause.message : "조직도를 불러오지 못했습니다.");
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    if (personal) {
      const id = ++requestId.current;
      setLoading(true);
      setError("");
      void api<{ graph: OwnershipGraphData }>("/api/me/ownership-graph")
        .then(({ graph: next }) => { if (active && id === requestId.current) setGraph(next); })
        .catch((cause) => { if (active && id === requestId.current) setError(cause instanceof Error ? cause.message : "조직도를 불러오지 못했습니다."); })
        .finally(() => { if (active && id === requestId.current) setLoading(false); });
      return () => { active = false; requestId.current += 1; };
    }
    void api<{ organizations: OrganizationOption[] }>("/api/admin/organizations?options=1")
      .then((result) => {
        if (!active) return;
        const available = result.organizations ?? [];
        setOrganizations(available);
        if (!initialCode && available[0]) void loadOrganization(available[0].organization_code, false);
        else if (!initialCode) {
          setError("표시할 법인이 없습니다.");
          setLoading(false);
        }
      })
      .catch(() => {
        if (active && !initialCode) {
          setError("법인 목록을 불러오지 못했습니다.");
          setLoading(false);
        }
      });
    if (initialCode) void loadOrganization(initialCode, initialDetail === "minor", false);
    return () => { active = false; requestId.current += 1; };
  }, [initialCode, initialDetail, loadOrganization, personal]);

  return <main className="ownership-window ownership-window-admin">
    <header>
      <div><p className="eyebrow">INAZINA · 지배구조</p><h1>{personal ? "보유 기업 지배구조" : "기업 지분·의결권 조직도"}</h1>
        <p className="sub">{personal ? "보유 주식의 의결권과 지배하는 자회사 관계를 표시합니다." : selected ? `${graph?.nodes.find((node) => node.kind === "company" && node.code === selected)?.name ?? selected} 기준으로 주주 관계를 표시합니다.` : "화면을 넓게 사용해 소유 관계와 의결권 경로를 확인합니다."}</p>
      </div>
      <button className="soft" type="button" onClick={() => window.close()}>창 닫기</button>
    </header>
    <div className="ownership-toolbar">
      {!personal && <button className="soft ownership-sidebar-toggle" type="button" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen((open) => !open)}>{sidebarOpen ? "사이드바 닫기" : "법인 트리 열기"}</button>}
      {!personal && <label className="check ownership-minor-toggle"><input type="checkbox" checked={includeMinor} disabled={loading} onChange={(event) => { if (selected) void loadOrganization(selected, event.target.checked); }} />1% 미만 지분도 표시</label>}
      {graph?.truncated && <small>표시 한도로 일부 소수 지분 또는 상위 주주 경로가 생략됐습니다.</small>}
    </div>
    <div className={!personal && sidebarOpen ? "ownership-workspace" : "ownership-workspace sidebar-collapsed"}>
      {!personal && sidebarOpen && <aside className="ownership-sidebar" aria-label="법인 지분 관계 트리">
        <h2>법인 선택</h2>
        <p>법인을 선택하면 해당 법인 기준으로 조직도를 다시 펼칩니다.</p>
        <label className="ownership-search"><span>전체 법인 검색</span><input type="search" value={organizationSearch} onChange={(event) => setOrganizationSearch(event.target.value)} placeholder="이름 또는 코드" /></label>
        {organizationSearch.trim() ? <ul className="ownership-tree ownership-search-results">{filteredOrganizations.map((organization) => <li key={organization.organization_code}><button type="button" className={selected === organization.organization_code ? "ownership-tree-company active" : "ownership-tree-company"} onClick={() => void loadOrganization(organization.organization_code, includeMinor)}>{organization.name} <small>{organization.organization_code}</small></button></li>)}{!filteredOrganizations.length && <li className="empty">검색된 법인이 없습니다.</li>}</ul> : <>
          <details className="ownership-tree-section" open>
            <summary>현재 지분 관계 경로</summary>
            {loading && !tree.length ? <div className="ownership-tree-loading"><i /><i /><i /></div> : tree.length
              ? <ul className="ownership-tree">{tree.map((node) => <TreeItem key={node.id} node={node} selected={focusedNodeId.replace(/^company:/, "")} onSelect={(code) => { setFocusedNodeId(`company:${code}`); setFocusRequest((value) => value + 1); }} />)}</ul>
              : <p className="empty">표시할 주주 관계가 없습니다.</p>}
          </details>
          <details className="ownership-tree-section">
            <summary>전체 법인</summary>
            <ul className="ownership-tree">{organizationGroups.map(([industry, members]) => <li key={industry}>
              <details className="ownership-tree-folder">
                <summary><span className="ownership-tree-icon" aria-hidden="true">▰</span><span className="ownership-tree-person">{industry} ({members.length})</span></summary>
                <ul>{members.map((organization) => <li className="ownership-tree-leaf-row" key={organization.organization_code}><button type="button" className={selected === organization.organization_code ? "ownership-tree-company active" : "ownership-tree-company"} onClick={() => void loadOrganization(organization.organization_code, includeMinor)} title={`${organization.name} (${organization.organization_code})`}><span aria-hidden="true">▣</span>{organization.name}</button></li>)}</ul>
              </details>
            </li>)}</ul>
          </details>
        </>}
      </aside>}
      <section className="ownership-graph-panel" aria-label={personal ? "내 보유 기업 지배구조" : "선택한 법인의 지분 조직도"}>
        {loading ? <LoadingGraph /> : error ? <div className="ownership-load-error" role="alert"><p>{error}</p><button className="soft" type="button" onClick={() => personal ? window.location.reload() : selected && void loadOrganization(selected, includeMinor, false)}>다시 불러오기</button></div> : <OwnershipGraph graph={graph} fullScreen focusedNodeId={focusedNodeId} focusRequest={focusRequest} />}
      </section>
    </div>
  </main>;
}
