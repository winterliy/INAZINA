import { useCallback, useEffect, useMemo, useState } from "react";
import { formatMoney } from "../../shared/money";
import { api } from "../../shared/api";
import { Card, Field, type Ctx } from "./common";
import "./OrganizationPage.css";

type Size = "sme" | "mid_market" | "large" | null;
type Issuance = { issuance_id: number; legacy_product_code: string; class_name: string; share_class: string; voting_rights_per_share: string; economic_interest_per_share: string; total_shares: string; share_price: string; status: string; dividend_mode: string; dividend_per_share: string; dividend_percent: string; dividend_priority: number; dividend_cycle_seconds: number; dividend_is_active: number; cumulative_dividend: number; liquidation_value_per_share: string; dividend_funding: string };
type Org = { organization_code: string; name: string; industry: string; share_price: string | number; purpose: string; ownership_type: string; public_enterprise_type: string; size_override: Size; auto_size: Size; pending_size: Size; size_effective_epoch: number | null; display_size: Size; metrics: { revenue: string | null; assets: string | null; employees: number | null }; issuances: Issuance[] };
type Group = { group_id: number; name: string; representative_organization_code: string | null; description: string; members: string[] };
export type OwnershipGraphData = { nodes: { id: string; kind: string; code: string; name: string }[]; edges: { from: string; to: string; equity_percent: string; voting_percent: string; root_equity_percent: string; root_voting_percent: string; depth: number; direct?: boolean; cycle?: boolean }[]; beneficial_owners?: { id: string; name: string; root_equity_percent: string; root_voting_percent: string }[]; truncated?: boolean; root_kind?: "user" | "organization" };
type Graph = OwnershipGraphData;
type Directory = { organizations: Org[]; groups: Group[]; graph?: Graph; sizeSettings: { announcement_seconds: number; rules: { mode?: string; thresholds?: Record<string, [string, string]> } } };

const SIZE: Record<Exclude<Size, null>, string> = { sme: "중소기업", mid_market: "중견기업", large: "대기업" };
const PURPOSE: Record<string, string> = { for_profit: "영리", nonprofit: "비영리" };
const PUBLIC: Record<string, string> = { none: "민간·기타", public_corporation: "공기업", market_based: "시장형", quasi_market_based: "준시장형", other_public: "기타 공공기관" };
const OWNERSHIP: Record<string, string> = { private: "민간 소유", national: "국가 소유", local: "지방자치단체 소유", mixed: "혼합 소유", other: "기타" };
const num = (value: string | null | undefined) => value === null || value === undefined ? "—" : formatMoney(value);

export function OrganizationPage({ ctx }: { ctx: Ctx }) {
  const { say } = ctx;
  const [data, setData] = useState<Directory | null>(null);
  const [selected, setSelected] = useState("");
  const [groupName, setGroupName] = useState("");
  const [announcementSeconds, setAnnouncementSeconds] = useState("0");
  const [thresholds, setThresholds] = useState<Record<string, [string, string]>>({});
  const [ruleMode, setRuleMode] = useState("any");
  const [includeMinor, setIncludeMinor] = useState(false);
  const [saving, setSaving] = useState(false);
  const load = useCallback(async (code: string, showMinor = false) => {
    const query = code && showMinor ? "?includeMinor=1" : "";
    const next = await api<Directory>(`/api/admin/organizations${code ? `/${encodeURIComponent(code)}` : ""}${query}`);
    setData(next);
    setAnnouncementSeconds(String(next.sizeSettings.announcement_seconds));
    setThresholds(Object.fromEntries(Object.entries(next.sizeSettings.rules.thresholds ?? {}).map(([key, limits]) => [key, [String(limits[0]), String(limits[1])] as [string, string]])));
    setRuleMode(next.sizeSettings.rules.mode ?? "any");
  }, []);
  useEffect(() => { void load("").catch((error) => say(error instanceof Error ? error.message : "기업 정보를 불러오지 못했습니다.", true)); }, [load, say]);

  const organization = useMemo(() => data?.organizations.find((item) => item.organization_code === selected), [data, selected]);
  const saveAction = async (payload: Record<string, unknown>, success: string) => {
    setSaving(true);
    try {
      await api("/api/admin/organizations", { method: "POST", body: JSON.stringify(payload) });
      say(success);
      await load(selected, includeMinor);
      return true;
    } catch (error) {
      say(error instanceof Error ? error.message : "조직 설정을 저장하지 못했습니다.", true);
      return false;
    } finally { setSaving(false); }
  };
  const retireIssuance = async (issue: Issuance) => {
    if (!window.confirm(`${issue.class_name} (${issue.legacy_product_code}) 주식 종류를 폐지할까요? 보유자·미체결 주문·계약·미지급 배당이 없어야 하며, 폐지 후 이 거래 코드는 다시 사용할 수 없습니다.`)) return;
    setSaving(true);
    try {
      const result = await api<string>(`/api/admin/companies/${encodeURIComponent(issue.legacy_product_code)}/share-actions`, {
        method: "POST", body: JSON.stringify({ action: "retire_issuance" }),
      });
      say(result, result.startsWith("❌"));
      await load(selected, includeMinor);
    } catch (error) {
      say(error instanceof Error ? error.message : "주식 종류를 폐지하지 못했습니다.", true);
    } finally { setSaving(false); }
  };
  const issueShares = async (issue: Issuance, payload: { mode: "paid" | "bonus"; quantity: string; issuePrice?: string }) => {
    const modeLabel = payload.mode === "paid" ? "유상증자" : "무상증자";
    if (!window.confirm(`${issue.class_name} (${issue.legacy_product_code}) ${modeLabel} ${payload.quantity}주를 진행할까요? 다른 주식 종류의 수량과 가격은 변경하지 않습니다.`)) return;
    setSaving(true);
    try {
      const result = await api<string>(`/api/admin/companies/${encodeURIComponent(issue.legacy_product_code)}/share-actions`, {
        method: "POST", body: JSON.stringify({ action: "issue_shares", ...payload }),
      });
      say(result, result.startsWith("❌"));
      await load(selected, includeMinor);
    } catch (error) {
      say(error instanceof Error ? error.message : "신주를 발행하지 못했습니다.", true);
    } finally { setSaving(false); }
  };

  return <>
    <Card title="기업 목록" caption="주식 발행과 법인 프로필을 분리하는 단계적 모델입니다. 현재 거래 코드와 보유 데이터는 유지됩니다.">
      <div className="scroll-x tall"><table><thead><tr><th>기업</th><th>영리성</th><th>공공 분류</th><th>자동 규모</th><th>표시 규모</th><th>주식 종류</th></tr></thead><tbody>
        {data?.organizations.map((item) => <tr key={item.organization_code} className={selected === item.organization_code ? "selected" : "clickable"} onClick={() => { setSelected(item.organization_code); void load(item.organization_code, includeMinor); }}>
          <td><strong>{item.name || item.organization_code}</strong><small className="mono">{item.organization_code} · {item.industry || "산업 미지정"}</small></td>
          <td>{PURPOSE[item.purpose] ?? item.purpose}</td><td>{PUBLIC[item.public_enterprise_type] ?? item.public_enterprise_type}</td>
          <td>{item.auto_size ? SIZE[item.auto_size] : "자료 부족"}</td><td>{item.display_size ? SIZE[item.display_size] : "자료 부족"}</td>
          <td>{item.issuances.length}</td>
        </tr>)}
      </tbody></table>{!data?.organizations.length && <p className="empty">기업 정보가 없습니다.</p>}</div>
    </Card>

    <div className="organization-layout">
      <Card title="기업집단" caption="집단 소속과 지분 지배관계는 별도로 관리됩니다.">
        <div className="organization-form-row"><Field label="새 기업집단"><input value={groupName} onChange={(event) => setGroupName(event.target.value)} maxLength={120} /></Field>
          <button type="button" disabled={saving || !groupName.trim()} onClick={() => void saveAction({ action: "group_create", name: groupName, representative: selected || null }, "기업집단을 만들었습니다.").then((ok) => { if (ok) setGroupName(""); })}>집단 만들기</button></div>
        <div>{data?.groups.map((group) => <section className="organization-inset" key={group.group_id}>
          <strong>{group.name}</strong><small>대표 기업: {group.representative_organization_code || "미지정"}</small>
          <div className="chips">{group.members.map((code) => <button className="chip" type="button" key={code} onClick={() => { setSelected(code); void load(code, includeMinor); }}>{code} ×</button>)}</div>
          {selected && !group.members.includes(selected) && <button className="soft" type="button" disabled={saving} onClick={() => void saveAction({ action: "group_member", group_id: group.group_id, organization_code: selected, active: true }, `${selected} 기업을 집단에 추가했습니다.`)}>선택 기업 추가</button>}
          {selected && group.members.includes(selected) && <button className="soft" type="button" disabled={saving} onClick={() => void saveAction({ action: "group_member", group_id: group.group_id, organization_code: selected, active: false }, `${selected} 기업을 집단에서 제외했습니다.`)}>선택 기업 제외</button>}
        </section>)}</div>
      </Card>
      <Card title="규모 분류 공시" caption="자동 판정에는 기준 통화 환산액을 저장하지 않고 요청 시점 환율로 계산합니다. 공시기간 0초는 즉시 적용입니다.">
        <div className="form-grid">
          <Field label="변경 공시기간 (초)" hint="0은 즉시 적용"><input type="number" min="0" step="1" value={announcementSeconds} onChange={(event) => setAnnouncementSeconds(event.target.value)} /></Field>
          <Field label="복수 기준 결합"><select value={ruleMode} onChange={(event) => setRuleMode(event.target.value)}><option value="any">기준 하나라도 초과하면 큰 구간</option><option value="all">모든 기준이 초과해야 큰 구간</option></select></Field>
          {(["revenue", "assets", "employees"] as const).map((key) => <div className="span-wide" key={key}><p className="sub-head">{key === "revenue" ? "연간 매출 (기준 통화)" : key === "assets" ? "자산 (기준 통화)" : "종업원 수"}</p><div className="organization-form-row">
            <Field label="중소기업 상한"><input inputMode="decimal" value={thresholds[key]?.[0] ?? ""} onChange={(event) => setThresholds((current) => ({ ...current, [key]: [event.target.value, current[key]?.[1] ?? ""] }))} /></Field>
            <Field label="중견기업 상한"><input inputMode="decimal" value={thresholds[key]?.[1] ?? ""} onChange={(event) => setThresholds((current) => ({ ...current, [key]: [current[key]?.[0] ?? "", event.target.value] }))} /></Field>
          </div></div>)}
        </div>
        <p className="hint">기준은 제품 내 자동 분류 규칙이며 법정 기준이라고 표시하지 않습니다. 빈 지표는 판정에서 제외됩니다.</p>
        <button type="button" disabled={saving || !/^\d+$/.test(announcementSeconds)} onClick={() => {
          const configured = Object.fromEntries(Object.entries(thresholds).filter(([, limits]) => limits[0].trim() && limits[1].trim()));
          void saveAction({ action: "size_settings", announcement_seconds: Number(announcementSeconds), rules: { mode: ruleMode, thresholds: configured } }, "기업 규모 분류 설정을 저장했습니다.");
        }}>분류 설정 저장</button>
      </Card>
    </div>

    {organization && <>
      <div className="organization-layout">
        <Card title="기업 분류" caption="법적 형태는 현재 주식회사로 유지합니다. 비영리법인 동작은 배당·잔여재산 제한을 구현한 뒤 활성화합니다.">
          <ClassificationEditor key={organization.organization_code} organization={organization} saving={saving} onSave={(ownership_type, public_enterprise_type) => void saveAction({ action: "classification", organization_code: organization.organization_code, ownership_type, public_enterprise_type }, "기업 분류를 저장했습니다.")} />
        </Card>
        <Card title={`${organization.name || organization.organization_code} · 규모`} caption={`자동 판정: ${organization.auto_size ? SIZE[organization.auto_size] : "자료 부족"} · 현재 표시: ${organization.display_size ? SIZE[organization.display_size] : "자료 부족"}`}>
          <div className="spec-strip"><Metric label="연간 매출 · 기준 통화" value={num(organization.metrics.revenue)} /><Metric label="자산 · 기준 통화" value={num(organization.metrics.assets)} /><Metric label="종업원 수" value={organization.metrics.employees === null ? "—" : new Intl.NumberFormat("ko-KR").format(organization.metrics.employees)} /></div>
          {organization.pending_size && <p className="notice">공시 예정: {SIZE[organization.pending_size]} · {organization.size_effective_epoch ? new Date(organization.size_effective_epoch * 1000).toLocaleString("ko-KR") : ""}</p>}
          <div className="organization-form-row"><Field label="관리자 지정 규모"><select key={organization.organization_code} id="organization-size-override" defaultValue={organization.size_override ?? ""}><option value="">자동 판정 사용</option><option value="sme">중소기업</option><option value="mid_market">중견기업</option><option value="large">대기업</option></select></Field>
            <button type="button" disabled={saving} onClick={() => { const size = (document.getElementById("organization-size-override") as HTMLSelectElement).value || null; void saveAction({ action: "size_override", organization_code: organization.organization_code, size, reason: "관리자 대시보드 지정" }, "기업 규모 지정을 저장했습니다."); }}>규모 적용</button></div>
        </Card>
        <Card title="주식 발행 종류" caption="새 종류는 별도 거래 코드로 추가되며 기존 보통주 가격·수량은 변경하지 않습니다. 분배·매입 절차는 별도 기업행동으로 처리해야 합니다.">
          {organization.issuances.map((issue) => <IssuanceEditor key={issue.legacy_product_code} issue={issue} saving={saving} onIssue={(payload) => void issueShares(issue, payload)} onRetire={() => void retireIssuance(issue)} onSave={(rights) => saveAction({ action: "issuance_rights", issuance_id: issue.issuance_id, legacy_product_code: issue.legacy_product_code, class_name: rights.class_name, voting_rights_per_share: rights.voting_rights_per_share, economic_interest_per_share: rights.economic_interest_per_share, rights: rights.dividend }, `${rights.class_name} 권리를 저장했습니다.`)} />)}
          <NewIssuanceForm key={organization.organization_code} organization={organization} saving={saving} onCreate={(payload) => void saveAction({ action: "issuance_create", ...payload }, `${payload.class_name} 주식 종류를 추가했습니다.`)} />
        </Card>
      </div>
      <Card title="지분·의결권 구조" caption="위쪽으로 직접 주주와 법인 주주의 보유 관계를 펼칩니다. 선에는 경제적 지분과 의결권 비율을 각각 표시합니다.">
        <label className="check"><input type="checkbox" checked={includeMinor} onChange={(event) => { const checked = event.target.checked; setIncludeMinor(checked); void load(selected, checked); }} />1% 미만 지분도 표시</label>
        <OwnershipGraph graph={data?.graph} />
      </Card>
    </>}
  </>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <section className="tile"><p className="tile-label">{label}</p><p className="tile-value">{value}</p></section>;
}

function ClassificationEditor({ organization, saving, onSave }: { organization: Org; saving: boolean; onSave: (ownership: string, publicType: string) => void }) {
  const [ownership, setOwnership] = useState(organization.ownership_type);
  const [publicType, setPublicType] = useState(organization.public_enterprise_type);
  return <div className="form-grid">
    <Field label="영리성"> <select value={organization.purpose} disabled><option value="for_profit">영리</option><option value="nonprofit">비영리 (구현 대기)</option></select></Field>
    <Field label="소유 유형"><select value={ownership} onChange={(event) => setOwnership(event.target.value)}>{Object.entries(OWNERSHIP).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
    <Field label="공공기관 분류"><select value={publicType} onChange={(event) => setPublicType(event.target.value)}>{Object.entries(PUBLIC).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
    <button type="button" className="soft" disabled={saving || (ownership === organization.ownership_type && publicType === organization.public_enterprise_type)} onClick={() => onSave(ownership, publicType)}>분류 저장</button>
  </div>;
}

function IssuanceEditor({ issue, saving, onSave, onIssue, onRetire }: { issue: Issuance; saving: boolean; onSave: (value: { class_name: string; voting_rights_per_share: string; economic_interest_per_share: string; dividend: Record<string, unknown> }) => void; onIssue: (payload: { mode: "paid" | "bonus"; quantity: string; issuePrice?: string }) => void; onRetire: () => void }) {
  const [className, setClassName] = useState(issue.class_name);
  const [votes, setVotes] = useState(issue.voting_rights_per_share);
  const [equity, setEquity] = useState(issue.economic_interest_per_share);
  const [dividendMode, setDividendMode] = useState(issue.dividend_mode);
  const [dividend, setDividend] = useState(issue.dividend_per_share);
  const [percent, setPercent] = useState(issue.dividend_percent);
  const [priority, setPriority] = useState(String(issue.dividend_priority));
  const [cycle, setCycle] = useState(String(issue.dividend_cycle_seconds));
  const [active, setActive] = useState(Boolean(issue.dividend_is_active));
  const [cumulative, setCumulative] = useState(Boolean(issue.cumulative_dividend));
  const [liquidation, setLiquidation] = useState(issue.liquidation_value_per_share);
  const [funding, setFunding] = useState(issue.dividend_funding);
  return <>
  <form className="organization-inset" onSubmit={(event) => {
    event.preventDefault();
    onSave({ class_name: className, voting_rights_per_share: votes, economic_interest_per_share: equity,
      dividend: { dividend_mode: dividendMode, dividend_per_share: dividend, dividend_percent: percent,
        dividend_priority: Number(priority), dividend_cycle_seconds: Number(cycle), dividend_is_active: active,
        cumulative_dividend: cumulative, liquidation_value_per_share: liquidation, dividend_funding: funding } });
  }}>
    <strong>{issue.class_name} · {issue.legacy_product_code}</strong>
    <small>발행 {num(issue.total_shares)}주 · 종목코드와 기존 보유·거래 기록은 유지됩니다.</small>
    <div className="form-grid section-gap">
      <Field label="주식 종류 이름"><input value={className} maxLength={120} onChange={(event) => setClassName(event.target.value)} /></Field>
      <Field label="주당 의결권"><input inputMode="decimal" value={votes} onChange={(event) => setVotes(event.target.value)} /></Field>
      <Field label="주당 경제 지분 계수"><input inputMode="decimal" value={equity} onChange={(event) => setEquity(event.target.value)} /></Field>
      <Field label="배당 방식"><select value={dividendMode} onChange={(event) => setDividendMode(event.target.value)}><option value="fixed">고정액</option><option value="percent">가격 비율</option></select></Field>
      <Field label="주당 배당액"><input inputMode="decimal" value={dividend} onChange={(event) => setDividend(event.target.value)} /></Field>
      <Field label="배당률 (%)"><input inputMode="decimal" value={percent} onChange={(event) => setPercent(event.target.value)} /></Field>
      <Field label="배당 순위"><input inputMode="numeric" value={priority} onChange={(event) => setPriority(event.target.value)} /></Field>
      <Field label="배당 주기 (초)"><input inputMode="numeric" value={cycle} onChange={(event) => setCycle(event.target.value)} /></Field>
      <Field label="청산 우선 금액/주"><input inputMode="decimal" value={liquidation} onChange={(event) => setLiquidation(event.target.value)} /></Field>
      <Field label="배당 재원"><select value={funding} onChange={(event) => setFunding(event.target.value)}><option value="treasury">국고</option><option value="issuer">법인 현금</option></select></Field>
      <Field label="배당 상태"><select value={active ? "1" : "0"} onChange={(event) => setActive(event.target.value === "1")}><option value="1">활성</option><option value="0">중지</option></select></Field>
      <label className="check"><input type="checkbox" checked={cumulative} onChange={(event) => setCumulative(event.target.checked)} />누적 배당</label>
    </div>
    <p className="hint">배당·청산 권리는 이 발행 종류에만 적용됩니다. 미지급 우선배당이 있으면 순위 변경은 제한됩니다.</p>
    <div className="actions"><button className="soft" type="submit" disabled={saving}>주식 권리 저장</button>
      {issue.share_class === "preferred" && <button className="outline" type="button" disabled={saving || issue.status !== "active"} onClick={onRetire}>이 종류 폐지</button>}</div>
  </form>
  {issue.status === "active" && <ShareCapitalEditor issue={issue} saving={saving} onIssue={onIssue} />}
  </>;
}

function ShareCapitalEditor({ issue, saving, onIssue }: { issue: Issuance; saving: boolean; onIssue: (payload: { mode: "paid" | "bonus"; quantity: string; issuePrice?: string }) => void }) {
  const [mode, setMode] = useState<"paid" | "bonus">("paid");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState(issue.share_price);
  return <form className="organization-inset" onSubmit={(event) => {
    event.preventDefault();
    onIssue({ mode, quantity, ...(mode === "paid" ? { issuePrice: price } : {}) });
  }}>
    <strong>이 종류 신주 발행</strong>
    <small>유상은 국고 매도 주문으로 판매하고, 무상은 기존 보유 비율에 따라 이 종류 주식만 배정합니다.</small>
    <div className="form-grid section-gap">
      <Field label="발행 방식"><select value={mode} onChange={(event) => setMode(event.target.value as "paid" | "bonus")}><option value="paid">유상증자</option><option value="bonus">무상증자</option></select></Field>
      <Field label="신주 수량"><input required inputMode="numeric" pattern="[0-9]+" value={quantity} onChange={(event) => setQuantity(event.target.value)} /></Field>
      {mode === "paid" && <Field label="주당 발행가"><input required inputMode="numeric" pattern="[0-9]+" value={price} onChange={(event) => setPrice(event.target.value)} /></Field>}
    </div>
    <button className="soft" type="submit" disabled={saving}>이 종류 신주 발행</button>
  </form>;
}

function NewIssuanceForm({ organization, saving, onCreate }: { organization: Org; saving: boolean; onCreate: (payload: Record<string, string>) => void }) {
  const [code, setCode] = useState("");
  const [className, setClassName] = useState("");
  const [shares, setShares] = useState("1000000");
  const [price, setPrice] = useState(String(organization.share_price || "1"));
  const [votes, setVotes] = useState("0");
  const [equity, setEquity] = useState("1");
  return <form className="organization-inset" onSubmit={(event) => {
    event.preventDefault();
    onCreate({ organization_code: organization.organization_code, legacy_product_code: code.trim().toUpperCase(),
      class_name: className.trim(), total_shares: shares, share_price: price,
      voting_rights_per_share: votes, economic_interest_per_share: equity });
  }}>
    <strong>주식 종류 추가</strong>
    <small>새 거래 코드를 등록합니다. 회사 공통 정보와 기존 주식 수량은 바뀌지 않습니다.</small>
    <div className="form-grid section-gap">
      <Field label="거래 코드"><input required maxLength={32} pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,31}" value={code} onChange={(event) => setCode(event.target.value)} /></Field>
      <Field label="종류 이름"><input required maxLength={120} value={className} onChange={(event) => setClassName(event.target.value)} placeholder="예: Class B" /></Field>
      <Field label="발행 주식 수"><input required inputMode="numeric" pattern="[0-9]+" value={shares} onChange={(event) => setShares(event.target.value)} /></Field>
      <Field label="초기 주가"><input required inputMode="numeric" pattern="[0-9]+" value={price} onChange={(event) => setPrice(event.target.value)} /></Field>
      <Field label="주당 의결권"><input required inputMode="decimal" value={votes} onChange={(event) => setVotes(event.target.value)} /></Field>
      <Field label="주당 경제 지분"><input required inputMode="decimal" value={equity} onChange={(event) => setEquity(event.target.value)} /></Field>
    </div>
    <button className="soft" type="submit" disabled={saving}>종류 추가</button>
  </form>;
}

export function OwnershipGraph({ graph }: { graph?: OwnershipGraphData }) {
  if (!graph?.nodes.length) return <p className="empty">지분 자료가 없거나 표시할 주주가 없습니다.</p>;
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const levels = new Map<string, number>();
  const userRoot = graph.root_kind === "user" ? graph.nodes.find((node) => node.kind === "user") : undefined;
  const root = userRoot ?? graph.nodes.find((node) => node.kind === "company" && graph.edges.some((edge) => edge.to === node.id));
  if (root) levels.set(root.id, 0);
  for (let depth = 0; depth < 5; depth += 1) {
    for (const edge of graph.edges) {
      if (userRoot && levels.has(edge.from) && !levels.has(edge.to)) levels.set(edge.to, (levels.get(edge.from) ?? 0) + 1);
      else if (!userRoot && levels.has(edge.to) && !levels.has(edge.from)) levels.set(edge.from, (levels.get(edge.to) ?? 0) + 1);
    }
  }
  const groups = new Map<number, string[]>();
  for (const node of graph.nodes) {
    const level = levels.get(node.id) ?? 1;
    groups.set(level, [...(groups.get(level) ?? []), node.id]);
  }
  return <div className="organization-graph" role="list" aria-label="주식 보유 관계">
    {graph.truncated && <p className="hint">표시 한도에 따라 일부 소수 지분 또는 하위 기업을 생략했습니다.</p>}
    {!!graph.beneficial_owners?.length && <details className="help"><summary>경로 말단 보유자 합산 (참고치)</summary>
      <p className="hint">최대 5단계 단순 경로 기여를 합산했습니다. 순환 출자에 대한 최종 지배 판정은 아닙니다.</p>
      <ul>{graph.beneficial_owners.map((owner) => <li key={owner.id}>{owner.name} · 경제 {Number(owner.root_equity_percent).toFixed(2)}% · 의결 {Number(owner.root_voting_percent).toFixed(2)}%</li>)}</ul>
    </details>}
    {[...groups.entries()].sort(([a], [b]) => a - b).map(([level, ids]) => <div className="organization-graph-level" key={level}>
      {ids.map((id) => <article className="organization-node" key={id} role="listitem"><strong>{nodes.get(id)?.name ?? id}</strong><small>{nodes.get(id)?.kind === "company" ? "법인" : "사용자"}</small>
        {graph.edges.filter((edge) => edge.from === id).map((edge) => <p key={`${edge.to}-${edge.depth}`}>{edge.cycle ? "순환 보유" : edge.depth === 0 ? "직접" : "경로 합산"} · 경제 {Number(edge.depth === 0 ? edge.equity_percent : edge.root_equity_percent).toFixed(2)}% · 의결 {Number(edge.depth === 0 ? edge.voting_percent : edge.root_voting_percent).toFixed(2)}%</p>)}
      </article>)}
    </div>)}
  </div>;
}
