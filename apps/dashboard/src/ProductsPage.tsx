import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { FundPanel } from "./FundPanel";
import { Card, Field, changed, date, number, plain, show, text, type Ctx } from "./common";

type KindField = { key: string; label: string; optional: boolean; text: boolean };
type AdvancedField = { key: string; label: string; type: string };
type Kind = { kind: string; token: string; label: string; group: "market" | "finance"; family: string; fields: KindField[]; advanced: AdvancedField[]; example: Record<string, unknown>; editable: boolean };
type Product = { code: string; name: string; kind: string; status: string; exchangeId: string | null; currency: string | null; value: number | null; drProgram?: { underlyingCode: string; receiptType: string; depositaryBank: string; custodian: string; underlyingPerReceipt: number; receiptSupply: number; depositedUnderlyingQuantity: number; custodiedUnderlyingQuantity: number; outstandingReceiptQuantity: number; programStatus: string; underlyingPrice?: number; underlyingCurrency?: string; referenceValue?: number; referencePremiumPercent?: number; referenceDeviationAlert?: boolean } };
type DrMovement = { movement_id: number; user_id: string; dr_code: string; underlying_code: string; movement_type: "issue" | "redeem"; dr_quantity: number; underlying_quantity: number; underlying_average_cost_text: string; underlying_currency: string; holder_underlying_average_cost_text: string; holder_underlying_currency: string; recorded_epoch: number };
type DrCustodyEvent = { event_id: number; reference_key: string; event_sequence: number; event_type: "instruction_received" | "custody_verified" | "settled"; instruction_status: "requested" | "verified" | "settled" | "rejected" | "cancelled" | null; dr_code: string; underlying_code: string; user_id: string; movement_type: "issue" | "redeem"; dr_quantity: number; underlying_quantity: number; recorded_epoch: number };
type DrDeviationSample = { sample_id: number; dr_code: string; sampled_epoch: number; dr_price: number | null; reference_value: number | null; deviation_percent: number | null; state: "normal" | "premium" | "discount" | "unavailable" };
type DrDeviationEvent = { event_id: number; dr_code: string; from_state: string; to_state: DrDeviationSample["state"]; recorded_epoch: number; deviation_percent: number | null };
type DrVotingMeeting = { meetingId: number; underlyingCode: string; title: string; recordEpoch: number; createdEpoch: number; opensEpoch: number | null; closesEpoch: number | null; quorumPercent: number; agendaCount: number };
type DrVotingResult = { agendaItemId: number; title: string; choices: Record<string, { votingPower: number; ballots: number }>; eligibleVotingPower: number; turnoutVotingPower: number; turnoutPercent: number; votingOpensEpoch: number | null; votingClosesEpoch: number | null; closed: boolean; quorumPercent: number; quorumMet: boolean | null };
type Detail = { code: string; kind: string; status: string; values: Record<string, unknown>; row: Record<string, unknown> };
type Editor = { mode: "create" | "edit"; kind: string; code: string; values: Record<string, string>; advanced: Record<string, string>; original: Detail | null };

// Ratios an administrator tunes on a live product (saved through the same 상품수정 path).
const RATIOS: Record<string, [string, string, string][]> = {
  etf: [["leverage_multiplier", "레버리지 배율", "−10~10 · 음수는 인버스 (−1 = 인버스, 2 = 2배)"], ["rebalance_cycle_seconds", "리밸런싱 주기 (초)", "86400 = 하루"]],
  etn: [["leverage_multiplier", "레버리지 배율", "−10~10 · 음수는 인버스"], ["rebalance_cycle_seconds", "리밸런싱 주기 (초)", "86400 = 하루"]],
  future: [["initial_margin", "초기 증거금률", "0~1 · 0.2 = 20%"], ["maintenance_margin", "유지 증거금률", "0~1 · 초기 증거금률 이하"]],
  option: [["implied_volatility", "내재변동성", "0~5 · 0.6 = 60%"]],
};

function RatioPanel({ editor, spec, busy, submit }: { editor: Editor; spec: Kind; busy: boolean; submit: (fields: Record<string, string>, advanced: Record<string, string>, summary: string) => void }) {
  const ratios = RATIOS[editor.kind] ?? [];
  const isFund = editor.kind === "fund";
  const current = (key: string) => text(editor.original?.values[key] ?? editor.original?.row[key]);
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(ratios.map(([key]) => [key, current(key)])));
  const parse = (raw: string) => raw.split(",").map((part) => part.trim()).filter(Boolean).map((part) => { const index = part.lastIndexOf(":"); return { asset: part.slice(0, index), weight: part.slice(index + 1) }; });
  const [rows, setRows] = useState(() => parse(current("components")));
  if (!ratios.length && !isFund) return null;
  const total = rows.reduce((sum, row) => sum + (Number(row.weight) || 0), 0);
  function apply() {
    const fields: Record<string, string> = {}; const advanced: Record<string, string> = {};
    for (const [key] of ratios) if (values[key] !== current(key)) (spec.fields.some((field) => field.key === key) ? fields : advanced)[key] = values[key];
    if (isFund) fields.components = rows.filter((row) => row.asset.trim()).map((row) => `${row.asset.trim()}:${row.weight}`).join(",");
    const summary = [...ratios.filter(([key]) => values[key] !== current(key)).map(([key, label]) => `${label}: ${current(key) || "—"} → ${values[key]}`),
      ...(isFund ? [`구성 비중: ${fields.components}`] : [])].join("\n");
    submit(fields, advanced, summary);
  }
  return <section className="ratio-panel">
    <h3 className="sub-head">비율 조정</h3>
    <p className="hint">{isFund ? "구성 종목과 비중(합계 1)을 바꾸면 현재 기준가에서 다시 출발합니다. 다른 펀드·ETF·ETN도 담을 수 있습니다(재간접)." : editor.kind === "future" ? "증거금률은 다음 정산부터 적용됩니다. 계약 승수·만기 같은 발행 조건은 바꿀 수 없습니다." : editor.kind === "option" ? "내재변동성은 이론 프리미엄 계산에 쓰입니다. 행사가·만기는 바꿀 수 없습니다." : "배율을 바꾸면 현재 가격을 기준으로 추종을 다시 시작합니다."}</p>
    {ratios.length > 0 && <div className="form-grid">{ratios.map(([key, label, hint]) => <Field key={key} label={label} hint={`${hint} · 현재 ${current(key) || "—"}`}>
      <input className="mono" inputMode="decimal" value={values[key] ?? ""} onChange={(event) => setValues({ ...values, [key]: event.target.value })} /></Field>)}</div>}
    {isFund && <div className="scroll-x"><table><thead><tr><th>구성 종목</th><th className="r">비중</th><th /></tr></thead><tbody>
      {rows.map((row, index) => <tr key={index}>
        <td><input className="mono" value={row.asset} onChange={(event) => setRows(rows.map((item, i) => i === index ? { ...item, asset: event.target.value } : item))} /></td>
        <td className="r"><input className="mono" inputMode="decimal" value={row.weight} onChange={(event) => setRows(rows.map((item, i) => i === index ? { ...item, weight: event.target.value } : item))} /></td>
        <td className="row-actions"><button type="button" className="soft small" onClick={() => setRows(rows.filter((_, i) => i !== index))}>빼기</button></td>
      </tr>)}
    </tbody></table>
      <div className="actions"><button type="button" className="soft small" onClick={() => setRows([...rows, { asset: "", weight: "0" }])}>종목 추가</button>
        <button type="button" className="soft small" disabled={!rows.length} onClick={() => setRows(rows.map((row) => ({ ...row, weight: String(Math.round(1 / rows.length * 1e6) / 1e6) })))}>균등 배분</button>
        <span className={Math.abs(total - 1) < 1e-6 ? "tag ok" : "tag warn"}>합계 {number(total)}</span></div></div>}
    <button type="button" className="btn primary" disabled={busy || (isFund && Math.abs(total - 1) > 1e-6)} onClick={apply}>비율 저장</button>
  </section>;
}

// Corporate actions and price changes stay commands; the list links to them.
const RELATED: Record<string, [string, string][]> = {
  stock: [["상품가격수정", "가격"], ["자본변경", "증자·감자"], ["액면분할병합", "분할·병합"], ["정기배당수정", "배당"], ["정기배당활성", "배당 켜기/끄기"], ["특별배당실시", "특별배당"], ["합병", "합병"], ["법인잔고수정", "법인 현금"], ["법인청산", "청산"], ["상품분위기", "분위기"], ["소수점거래관리", "소수점"]],
  preferred: [["상품가격수정", "가격"], ["정기배당수정", "배당"], ["정기배당활성", "배당 켜기/끄기"], ["특별배당실시", "특별배당"]],
  asset: [["상품가격수정", "가격"], ["정기배당수정", "배당"], ["상품분위기", "분위기"], ["소수점거래관리", "소수점"]],
  dr: [["상품가격수정", "가격"]],
  etf: [["상품가격수정", "가격"], ["소수점거래관리", "소수점"]],
  fund: [["상품가격수정", "가격"], ["정기배당수정", "배당"], ["소수점거래관리", "소수점"]],
  indexfund: [["상품가격수정", "가격"], ["소수점거래관리", "소수점"]],
  commodity: [["상품가격수정", "가격"], ["상품분위기", "분위기"], ["소수점거래관리", "소수점"]],
  future: [["상품가격수정", "가격"]],
  corporatebond: [["상품가격수정", "가격"]],
  convertiblebond: [["상품가격수정", "가격"]], warrantbond: [["상품가격수정", "가격"]],
  abs: [["상품가격수정", "가격"]], mbs: [["상품가격수정", "가격"]], abcp: [["상품가격수정", "가격"]],
  etn: [["상품가격수정", "가격"]],
  bond: [["상품가격수정", "가격"]],
  option: [["상품가격수정", "프리미엄"]],
  els: [["ELS취소", "모집 취소"]],
  loan: [["상품이율수정", "이율"]],
  currency: [["환율수정", "환율"]],
};

export function ProductsPage({ group, ctx }: { group: "market" | "finance"; ctx: Ctx }) {
  const { busy, run, say, query, openCommand } = ctx;
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [drMovements, setDrMovements] = useState<DrMovement[]>([]);
  const [drLedgerError, setDrLedgerError] = useState(false);
  const [drCustodyEvents, setDrCustodyEvents] = useState<DrCustodyEvent[]>([]);
  const [drCustodyError, setDrCustodyError] = useState(false);
  const [drDeviationSamples, setDrDeviationSamples] = useState<DrDeviationSample[]>([]);
  const [drDeviationEvents, setDrDeviationEvents] = useState<DrDeviationEvent[]>([]);
  const [drMonitorError, setDrMonitorError] = useState(false);
  const [drVotingMeetings, setDrVotingMeetings] = useState<DrVotingMeeting[]>([]);
  const [drVotingMeetingId, setDrVotingMeetingId] = useState("");
  const [drVotingResults, setDrVotingResults] = useState<DrVotingResult[]>([]);
  const [drVotingError, setDrVotingError] = useState(false);
  const [drVotingUnderlying, setDrVotingUnderlying] = useState("");
  const [drVotingTitle, setDrVotingTitle] = useState("");
  const [drVotingOpens, setDrVotingOpens] = useState("");
  const [drVotingCloses, setDrVotingCloses] = useState("");
  const [drVotingQuorum, setDrVotingQuorum] = useState("50");
  const [drVotingAgendaTitle, setDrVotingAgendaTitle] = useState("");
  const [drVotingChoices, setDrVotingChoices] = useState("찬성,반대,기권");
  const drMonitorInitialized = useRef(false);
  const lastDrAlertEventId = useRef(0);
  const [filter, setFilter] = useState("all");
  const [editor, setEditor] = useState<Editor | null>(null);
  const [showInactive, setShowInactive] = useState(true);
  const [fundCode, setFundCode] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [catalog, list] = await Promise.all([
      api<{ kinds: Kind[] }>("/api/admin/products/catalog"),
      api<{ products: Product[] }>(`/api/admin/products?group=${group}`),
    ]);
    setKinds(catalog.kinds.filter((item) => item.group === group));
    setProducts(list.products);
    if (group === "market") {
      try {
        const voting = await api<{ meetings: DrVotingMeeting[] }>("/api/admin/dr/voting-meetings?limit=100");
        setDrVotingMeetings(voting.meetings);
        setDrVotingError(false);
        if (!drVotingMeetingId && voting.meetings[0]) setDrVotingMeetingId(String(voting.meetings[0].meetingId));
      } catch {
        setDrVotingMeetings([]);
        setDrVotingError(true);
      }
      try {
        const custody = await api<{ events: DrCustodyEvent[] }>("/api/admin/dr/custody-events?limit=100");
        setDrCustodyEvents(custody.events);
        setDrCustodyError(false);
      } catch {
        setDrCustodyEvents([]);
        setDrCustodyError(true);
      }
      try {
        const drLedger = await api<{ movements: DrMovement[] }>("/api/admin/dr/movements?limit=100");
        setDrMovements(drLedger.movements);
        setDrLedgerError(false);
      } catch {
        setDrMovements([]);
        setDrLedgerError(true);
      }
      try {
        const monitored = await api<{ samples: DrDeviationSample[]; events: DrDeviationEvent[] }>("/api/admin/dr/monitor?limit=100");
        setDrDeviationSamples(monitored.samples);
        setDrDeviationEvents(monitored.events);
        setDrMonitorError(false);
        const newestEventId = monitored.events[0]?.event_id ?? 0;
        if (drMonitorInitialized.current && newestEventId > lastDrAlertEventId.current) {
          const alerts = monitored.events.filter((event) => event.event_id > lastDrAlertEventId.current &&
            (event.to_state === "premium" || event.to_state === "discount"));
          if (alerts.length) {
            say(`DR 괴리 경보: ${alerts.map((event) => `${event.dr_code} ${drDeviationLabel(event.to_state)}`).join(" · ")}`, true);
          }
        }
        lastDrAlertEventId.current = Math.max(lastDrAlertEventId.current, newestEventId);
        drMonitorInitialized.current = true;
      } catch {
        setDrDeviationSamples([]);
        setDrDeviationEvents([]);
        setDrMonitorError(true);
      }
    } else {
      setDrMovements([]);
      setDrLedgerError(false);
      setDrCustodyEvents([]);
      setDrCustodyError(false);
      setDrDeviationSamples([]);
      setDrDeviationEvents([]);
      setDrMonitorError(false);
      setDrVotingMeetings([]);
      setDrVotingResults([]);
      setDrVotingError(false);
    }
  }, [group, say, drVotingMeetingId]);

  const selectedVotingMeeting = drVotingMeetings.find((meeting) => String(meeting.meetingId) === drVotingMeetingId);
  const refreshDrVotingResults = useCallback(async () => {
    if (group !== "market" || !drVotingMeetingId) { setDrVotingResults([]); return; }
    try {
      const response = await api<{ results: DrVotingResult[] }>(`/api/admin/dr/voting-results?meetingId=${encodeURIComponent(drVotingMeetingId)}`);
      setDrVotingResults(response.results);
    } catch {
      setDrVotingResults([]);
    }
  }, [group, drVotingMeetingId]);

  useEffect(() => { void refreshDrVotingResults(); }, [refreshDrVotingResults]);

  function submitDrVoting(path: string, payload: Record<string, unknown>, success: string) {
    void run(() => api<{ success?: boolean; result?: unknown; message?: string }>(path, { method: "POST", body: JSON.stringify(payload) }),
      "DR 의결권 정보를 저장하지 못했습니다.", async (response) => {
        if (response.success === false) { say(typeof response.result === "string" ? plain(response.result) : "요청을 처리하지 못했습니다.", true); return; }
        say(success);
        await load();
        await refreshDrVotingResults();
      });
  }

  useEffect(() => {
    setEditor(null);
    setFilter("all");
    load().catch((cause) => say(cause instanceof Error ? cause.message : "상품을 불러오지 못했습니다.", true));
  }, [load]);

  useEffect(() => {
    if (group !== "market") return;
    const timer = window.setInterval(() => {
      if (!document.hidden) load().catch(() => undefined);
    }, 60_000);
    return () => window.clearInterval(timer);
  }, [group, load]);

  const kindOf = (kind: string) => kinds.find((item) => item.kind === kind);
  // Savings and foreign-currency accounts are one family: same form, different account currency.
  const familyLabel = (kind: string) => kindOf(kind)?.family === "saving" ? "저축" : kindOf(kind)?.label ?? kind;
  const families = useMemo(() => kinds.filter((kind, index) => kinds.findIndex((other) => other.family === kind.family) === index), [kinds]);
  const visible = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return products.filter((item) => (filter === "all" || kindOf(item.kind)?.family === filter) && (showInactive || item.status === "active") &&
      (!term || `${item.code} ${item.name} ${familyLabel(item.kind)} ${item.currency ?? ""} ${item.exchangeId ?? ""}`.toLocaleLowerCase().includes(term)));
  }, [products, filter, query, showInactive, kinds]);

  function startCreate(kind: string, keep?: Record<string, string>) {
    const spec = kindOf(kind);
    if (!spec) return;
    setEditor({ mode: "create", kind, code: editor?.mode === "create" ? editor.code : "", original: null,
      values: Object.fromEntries(spec.fields.map((field) => [field.key, keep?.[field.key] ?? text(spec.example[field.key])])), advanced: {} });
  }

  async function startEdit(item: Product) {
    await run(() => api<Detail>(`/api/admin/products/${encodeURIComponent(item.code)}`), "상품 정보를 불러오지 못했습니다.", (detail) => {
      const spec = kindOf(detail.kind);
      setEditor({ mode: "edit", kind: detail.kind, code: detail.code, original: detail,
        values: Object.fromEntries((spec?.fields ?? []).map((field) => [field.key, text(detail.values[field.key])])),
        advanced: Object.fromEntries((spec?.advanced ?? []).map((field) => [field.key, text(detail.row[field.key])])) });
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editor) return;
    const spec = kindOf(editor.kind)!;
    const fields = editor.mode === "edit" ? changed(editor.values, editor.original?.values ?? {}) : editor.values;
    const advanced = editor.mode === "edit" ? changed(editor.advanced, editor.original?.row ?? {}) : Object.fromEntries(Object.entries(editor.advanced).filter(([, value]) => value !== ""));
    if (editor.mode === "edit" && !Object.keys(fields).length && !Object.keys(advanced).length) {
      say("변경된 값이 없습니다.", true);
      return;
    }
    const summary = editor.mode === "edit"
      ? [...Object.entries(fields).map(([key, value]) => `${spec.fields.find((field) => field.key === key)?.label ?? key}: ${show(editor.original?.values[key])} → ${show(value)}`),
         ...Object.entries(advanced).map(([key, value]) => `${key}: ${show(editor.original?.row[key])} → ${show(value)}`)].join("\n")
      : `${spec.label} ${editor.code}`;
    if (!window.confirm(`${editor.mode === "create" ? "상품을 생성할까요?" : `${editor.code} 상품을 수정할까요?`}\n\n${summary}`)) return;
    void run(() => api<{ message: string; success: boolean }>("/api/admin/products", {
      method: "POST", body: JSON.stringify({ operation: editor.mode, kind: editor.kind, code: editor.code.trim(), fields, advanced }),
    }), "상품을 저장하지 못했습니다.", async (result) => {
      say(plain(result.message), !result.success);
      if (result.success) { setEditor(null); await load(); }
    });
  }

  function toggle(item: Product) {
    const active = item.status !== "active";
    const label = item.kind === "els" && !active ? "모집 취소(청약금 환불)" : active ? "재개" : "거래 중지";
    if (!window.confirm(`${item.name} (${item.code})을 ${label}할까요?`)) return;
    void run(() => api<{ message: string; success: boolean }>(`/api/admin/products/${encodeURIComponent(item.code)}/status`, { method: "POST", body: JSON.stringify({ active }) }),
      "상태를 바꾸지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  }

  function terminateDr(item: Product) {
    if (!item.drProgram) return;
    if (!window.confirm(`${item.code} DR 프로그램을 영구 종료할까요? 보유·담보·주문·시뮬레이터 예탁분이 남아 있으면 서버가 거부합니다. 종료 후 재활성화할 수 없습니다.`)) return;
    void run(() => api<{ message: string; success: boolean }>(`/api/admin/products/${encodeURIComponent(item.code)}/status`, { method: "POST", body: JSON.stringify({ terminate: true }) }),
      "DR 프로그램을 종료하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  }

  function purge(item: Product) {
    if (!window.confirm(`${item.name} (${item.code})을 완전히 삭제할까요?\n\n상품 정의와 시세 기록·차트 선이 지워지고 되돌릴 수 없습니다. 주문·배당 같은 과거 기록은 남습니다. 보유자나 미체결 주문, 계좌·대출 등이 남아 있으면 서버가 거부합니다.`)) return;
    void run(() => api<{ message: string; success: boolean }>(`/api/admin/products/${encodeURIComponent(item.code)}/delete`, { method: "POST", body: "{}" }),
      "상품을 삭제하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); if (editor?.code === item.code) setEditor(null); await load(); });
  }

  const spec = editor ? kindOf(editor.kind) : undefined;
  const counts = Object.fromEntries(families.map((kind) => [kind.family, products.filter((item) => kindOf(item.kind)?.family === kind.family).length]));

  return <>
    {fundCode && <FundPanel key={fundCode} code={fundCode} ctx={ctx} close={() => setFundCode(null)} />}
    <Card title="새 상품" caption={`${families.map((kind) => familyLabel(kind.kind)).join(" · ")} — 종류를 고르면 $상품생성과 같은 입력 칸이 나옵니다.`}>
      <div className="kind-grid">{families.map((kind) => <button key={kind.family} type="button" className={editor?.mode === "create" && kindOf(editor.kind)?.family === kind.family ? "command-chip active" : "command-chip"} onClick={() => startCreate(kind.kind)}><b>{familyLabel(kind.kind)}</b><small>{kind.family === "saving" ? "saving · 국적 통화/외환" : kind.token} · {counts[kind.family] ?? 0}개</small></button>)}</div>
    </Card>

    {group === "market" && <Card title="DR 내부 수탁 처리 이력" caption="요청별 상태 집계와 접수 → 검증 → 정산 이벤트를 함께 표시합니다. 현재 전환은 한 번의 시뮬레이터 거래에서 동기 처리되며, 대기·재시도·외부 수탁 증빙은 아직 지원하지 않습니다.">
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>요청 참조</th><th>현재 상태</th><th>처리 단계</th><th>DR / 원주</th><th>사용자</th><th>방향</th><th className="r">DR 수량</th><th className="r">원주 수량</th></tr></thead><tbody>
        {drCustodyEvents.map((event) => <tr key={event.event_id}><td>{date(event.recorded_epoch)}</td><td className="mono">{event.reference_key}</td><td>{event.instruction_status === "settled" ? "정산 완료" : event.instruction_status ?? "상태 없음"}</td><td>{event.event_type === "instruction_received" ? "요청 접수" : event.event_type === "custody_verified" ? "보관 수량 검증" : "내부 결제 완료"}</td><td className="mono">{event.dr_code} · {event.underlying_code}</td><td className="mono">{event.user_id}</td><td>{event.movement_type === "issue" ? "발행" : "상환"}</td><td className="r mono">{number(event.dr_quantity)}</td><td className="r mono">{number(event.underlying_quantity)}</td></tr>)}
      </tbody></table>{drCustodyError ? <p className="empty">DR 수탁 처리 이력을 불러오지 못했습니다.</p> : !drCustodyEvents.length && <p className="empty">수탁 처리 이력이 없습니다.</p>}</div>
    </Card>}

    {group === "market" && <Card title="DR 주주총회·의결권 관리" caption="기준 시점의 현재 DR 잔고를 의결권 스냅샷으로 고정하고, 안건·투표기간·정족수를 관리합니다. 스냅샷은 과거 기준일 잔고를 재구성하는 기록이 아니며, 실제 법적 의결 절차를 대체하지 않습니다.">
      <div className="form-grid">
        <Field label="기초 원주 코드" hint="활성 원주 코드"><input className="mono" value={drVotingUnderlying} onChange={(event) => setDrVotingUnderlying(event.target.value.toUpperCase())} /></Field>
        <Field label="회의명"><input value={drVotingTitle} onChange={(event) => setDrVotingTitle(event.target.value)} /></Field>
        <button type="button" className="btn primary" disabled={busy || !drVotingUnderlying.trim() || !drVotingTitle.trim()} onClick={() => submitDrVoting("/api/admin/dr/voting-meetings", { underlyingCode: drVotingUnderlying.trim(), title: drVotingTitle.trim() }, "기준 잔고를 고정한 주주총회를 만들었습니다.")}>현재 잔고로 회의 생성</button>
      </div>
      <div className="form-grid">
        <Field label="회의 선택" hint={drVotingError ? "회의 목록을 불러오지 못했습니다." : "기록 시점 잔고 기준"}><select value={drVotingMeetingId} onChange={(event) => setDrVotingMeetingId(event.target.value)}><option value="">회의 선택</option>{drVotingMeetings.map((meeting) => <option key={meeting.meetingId} value={meeting.meetingId}>#{meeting.meetingId} · {meeting.underlyingCode} · {meeting.title}</option>)}</select></Field>
        <Field label="투표 시작 (Unix 초)"><input className="mono" inputMode="numeric" value={drVotingOpens} onChange={(event) => setDrVotingOpens(event.target.value)} /></Field>
        <Field label="투표 종료 (Unix 초)"><input className="mono" inputMode="numeric" value={drVotingCloses} onChange={(event) => setDrVotingCloses(event.target.value)} /></Field>
        <Field label="정족수 (%)"><input className="mono" inputMode="decimal" value={drVotingQuorum} onChange={(event) => setDrVotingQuorum(event.target.value)} /></Field>
        <button type="button" className="soft" disabled={busy || !drVotingMeetingId || !drVotingOpens || !drVotingCloses} onClick={() => submitDrVoting("/api/admin/dr/voting-window", { meetingId: Number(drVotingMeetingId), opensEpoch: Number(drVotingOpens), closesEpoch: Number(drVotingCloses), quorumPercent: Number(drVotingQuorum) }, "투표기간과 정족수를 설정했습니다.")}>기간·정족수 설정</button>
        <Field label="안건 제목"><input value={drVotingAgendaTitle} onChange={(event) => setDrVotingAgendaTitle(event.target.value)} /></Field>
        <Field label="선택지" hint="쉼표로 구분 · 2~8개"><input value={drVotingChoices} onChange={(event) => setDrVotingChoices(event.target.value)} /></Field>
        <button type="button" className="soft" disabled={busy || !drVotingMeetingId || !drVotingAgendaTitle.trim()} onClick={() => submitDrVoting("/api/admin/dr/voting-agenda", { meetingId: Number(drVotingMeetingId), title: drVotingAgendaTitle.trim(), choices: drVotingChoices.split(",").map((choice) => choice.trim()).filter(Boolean) }, "안건을 추가했습니다.")}>안건 추가</button>
      </div>
      {selectedVotingMeeting && <p className="hint">회의 #{selectedVotingMeeting.meetingId} · 기준 시각 {date(selectedVotingMeeting.recordEpoch)} · 안건 {selectedVotingMeeting.agendaCount}개 · {selectedVotingMeeting.opensEpoch ? `투표 ${date(selectedVotingMeeting.opensEpoch)} ~ ${date(selectedVotingMeeting.closesEpoch ?? 0)}` : "투표기간 미설정"} · 정족수 {number(selectedVotingMeeting.quorumPercent)}%</p>}
      <div className="scroll-x"><table><thead><tr><th>안건</th><th>선택지별 의결권</th><th className="r">참여율</th><th>상태</th></tr></thead><tbody>{drVotingResults.map((result) => <tr key={result.agendaItemId}><td>{result.title}</td><td>{Object.entries(result.choices).map(([choice, tally]) => `${choice} ${number(tally.votingPower)}표`).join(" · ")}</td><td className="r mono">{number(result.turnoutPercent)}%</td><td>{result.closed ? result.quorumMet ? "종료 · 정족수 충족" : "종료 · 정족수 미달" : "진행 전/중"}</td></tr>)}</tbody></table>{!drVotingResults.length && <p className="empty">선택한 회의의 안건 결과가 없습니다.</p>}</div>
    </Card>}

    {group === "market" && <Card title="DR 전환 원장" caption="최근 100건 · 조회 전용 불변 기록. 예탁 풀 단가는 수탁 원장 대사용이고, 보유자 이전원가는 상환 후 개인에게 돌아가는 세무 원가입니다. 시뮬레이터 기록이지 실제 수탁 증빙은 아닙니다.">
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>사용자</th><th>DR</th><th>유형</th><th className="r">DR 수량</th><th className="r">원주 수량</th><th className="r">예탁 풀 단가</th><th className="r">보유자 이전원가</th><th>통화</th></tr></thead><tbody>
        {drMovements.map((item) => <tr key={item.movement_id}><td>{date(item.recorded_epoch)}</td><td className="mono">{item.user_id}</td><td className="mono">{item.dr_code} · {item.underlying_code}</td><td>{item.movement_type === "issue" ? "DR 발행" : "원주 상환"}</td><td className="r mono">{number(item.dr_quantity)}</td><td className="r mono">{number(item.underlying_quantity)}</td><td className="r mono">{groupAmount(item.underlying_average_cost_text)}</td><td className="r mono">{groupAmount(item.holder_underlying_average_cost_text)}</td><td>{item.underlying_currency} / {item.holder_underlying_currency}</td></tr>)}
      </tbody></table>{drLedgerError ? <p className="empty">DR 원장을 불러오지 못했습니다. 상품 목록은 정상 조회되었습니다.</p> : !drMovements.length && <p className="empty">DR 전환 기록이 없습니다.</p>}</div>
    </Card>}

    {group === "market" && <Card title="DR 괴리 관측·경보 이력" caption="Python 데이터 서비스가 1분마다 표본 수집을 시도하고 종목별 최대 5분당 1회 저장합니다. 고평가·저평가 진입과 정상 복귀는 인접한 5분 표본 2회 연속 확인 후 관리자 웹 알림으로 표시합니다. 표에는 원시 관측 상태가 보입니다. ±10% 경보는 단순 참고가 기준이며 환율·비용·유동성은 반영하지 않습니다. 표본은 90일 보관합니다.">
      <h3 className="sub-head">최근 상태 표본</h3>
      <div className="scroll-x"><table><thead><tr><th>관측 시각</th><th>DR</th><th>상태</th><th className="r">현재가</th><th className="r">참고가</th><th className="r">괴리율</th></tr></thead><tbody>
        {drDeviationSamples.map((item) => <tr key={item.sample_id}><td>{date(item.sampled_epoch)}</td><td className="mono">{item.dr_code}</td><td>{drDeviationLabel(item.state)}</td><td className="r mono">{item.dr_price == null ? "—" : number(item.dr_price)}</td><td className="r mono">{item.reference_value == null ? "—" : number(item.reference_value)}</td><td className="r mono">{item.deviation_percent == null ? "—" : `${number(item.deviation_percent)}%`}</td></tr>)}
      </tbody></table>{drMonitorError ? <p className="empty">DR 괴리 이력을 불러오지 못했습니다.</p> : !drDeviationSamples.length && <p className="empty">관측 가능한 DR 표본이 없습니다. 활성 DR이 있으면 이 화면에서 표본 수집을 시작합니다.</p>}</div>
      <h3 className="sub-head">경보 상태 전환</h3>
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>DR</th><th>전환</th><th className="r">괴리율</th></tr></thead><tbody>
        {drDeviationEvents.map((item) => <tr key={item.event_id}><td>{date(item.recorded_epoch)}</td><td className="mono">{item.dr_code}</td><td>{drDeviationLabel(item.from_state)} → {drDeviationLabel(item.to_state)}</td><td className="r mono">{item.deviation_percent == null ? "—" : `${number(item.deviation_percent)}%`}</td></tr>)}
      </tbody></table>{!drMonitorError && !drDeviationEvents.length && <p className="empty">경보 상태 전환 이력이 없습니다.</p>}</div>
    </Card>}

    {editor && spec && <Card title={editor.mode === "create" ? `${spec.label} 생성` : `${editor.code} 수정`} caption={editor.kind === "dr" ? editor.mode === "create" ? "기초 코드는 활성 보통주여야 하고 결제 통화·상장 거래소도 이미 등록되어야 합니다. 발행량 × DR당 원주 수는 원주 발행량 한도 안에 있어야 합니다." : "기초주식·비율·통화·거래소 등 발행조건은 변경할 수 없습니다. 가격과 표시 이름만 수정할 수 있습니다." : editor.mode === "create" ? "선택 항목은 비워 둘 수 있습니다. 배당주기는 초 또는 1일·1주·1달, 배당은 5 또는 5%처럼 입력합니다." : "바꾼 값만 저장합니다. 결제 통화와 발행된 계약 조건처럼 바꿀 수 없는 값은 서버가 거부합니다."}
      actions={<button className="soft" type="button" onClick={() => setEditor(null)}>닫기</button>}>
      {spec.family === "saving" && <div className="saving-mode">
        <div className="segment-tabs" role="radiogroup" aria-label="통장 방식">{kinds.filter((kind) => kind.family === "saving").map((kind) => <button key={kind.kind} type="button" role="radio" aria-checked={editor.kind === kind.kind} className={editor.kind === kind.kind ? "active" : ""} disabled={editor.mode === "edit"} onClick={() => startCreate(kind.kind, editor.values)}>{kind.kind === "saving" ? "국적 통화 저축" : "외환통장"}</button>)}</div>
        <p className="hint">{editor.kind === "saving" ? "통장 화폐가 국적 통화인 시민이 비밀번호·예치금을 넣고 여는 저축입니다." : "통장 화폐가 국적 통화가 아닌 시민이 여는 저축입니다. 통화마다 한 사람당 하나만 열 수 있어요."} 입력 항목은 같고 통장 화폐만 다릅니다.</p>
      </div>}
      <form className="form-grid" onSubmit={save}>
        <Field label="상품 코드" hint={editor.mode === "edit" ? "변경 불가" : "공백 없이"}><input className="mono" value={editor.code} readOnly={editor.mode === "edit"} required={editor.kind !== "option"} onChange={(event) => setEditor({ ...editor, code: event.target.value })} /></Field>
        {spec.fields.map((field) => {
          const drFixed = editor.kind === "dr" && ["total_supply", "exchange_id", "underlying_code", "underlying_per_receipt", "receipt_type", "depositary_bank", "custodian", "home_exchange_id", "quote_currency"].includes(field.key);
          return <Field key={field.key} label={field.label} hint={`${field.key}${field.optional ? " · 선택" : ""}${editor.mode === "edit" && drFixed ? " · 발행조건 고정" : ""}`} wide={field.key === "components" || field.key === "underlyings"}>
            <input className={field.text ? undefined : "mono"} readOnly={editor.mode === "edit" && drFixed} required={editor.mode === "create" && !field.optional} value={editor.values[field.key] ?? ""} onChange={(event) => setEditor({ ...editor, values: { ...editor.values, [field.key]: event.target.value } })} />
          </Field>;
        })}
        <button className="btn primary" disabled={busy}>{busy ? "저장 중…" : editor.mode === "create" ? "생성" : "저장"}</button>
      </form>
      {editor.mode === "edit" && <RatioPanel key={editor.code} editor={editor} spec={spec} busy={busy} submit={(fields, advanced, summary) => {
        if (!Object.keys(fields).length && !Object.keys(advanced).length) { say("변경된 비율이 없습니다.", true); return; }
        if (!window.confirm(`${editor.code} 비율을 바꿀까요?\n\n${summary}`)) return;
        void run(() => api<{ message: string; success: boolean }>("/api/admin/products", { method: "POST", body: JSON.stringify({ operation: "edit", kind: editor.kind, code: editor.code, fields, advanced }) }),
          "비율을 저장하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); if (result.success) { setEditor(null); await load(); } });
      }} />}
      {spec.advanced.length > 0 && editor.kind !== "els" && <details className="help advanced"><summary>고급 설정 ({spec.advanced.length}개 열)</summary>
        <p className="hint">{editor.mode === "create" ? "필요한 항목만 입력하세요. 비우면 기본값을 씁니다." : "현재 값입니다. 바꾼 칸만 저장합니다."}</p>
        <div className="form-grid">{spec.advanced.map((field) => <Field key={field.key} label={field.label} hint={`${field.key} · ${field.type || "ANY"}`}>
          <input className="mono" value={editor.advanced[field.key] ?? ""} onChange={(event) => setEditor({ ...editor, advanced: { ...editor.advanced, [field.key]: event.target.value } })} />
        </Field>)}</div>
      </details>}
    </Card>}

    <Card title={group === "market" ? "종목 · 상품" : "금융 상품"} caption={`${number(visible.length)}개 표시 · 중지는 거래만 멈추고, 중지된 상품은 아무도 보유·이용하지 않으면 삭제로 완전히 지울 수 있습니다.`}
      actions={<><select aria-label="종류" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">전체 종류</option>{families.map((kind) => <option key={kind.family} value={kind.family}>{familyLabel(kind.kind)} ({counts[kind.family] ?? 0})</option>)}</select>
        <label className="check"><input type="checkbox" checked={showInactive} onChange={(event) => setShowInactive(event.target.checked)} />중지 포함</label></>}>
      <div className="scroll-x tall"><table><thead><tr><th>코드</th><th>이름</th><th>종류</th><th>거래소</th><th>통화</th><th className="r">{group === "market" ? "가격" : "이율·환율"}</th><th>상태</th><th /></tr></thead><tbody>
        {visible.map((item) => <tr key={`${item.kind}-${item.code}`}>
          <td className="mono">{item.code}</td><td>{item.name}{item.drProgram && <small className="block hint">원주 {item.drProgram.underlyingCode} · 1증서 = {number(item.drProgram.underlyingPerReceipt)}주 · 시뮬레이터 보관 {number(item.drProgram.custodiedUnderlyingQuantity)}주 / 발행 DR 기준 필요 {number(item.drProgram.outstandingReceiptQuantity * item.drProgram.underlyingPerReceipt)}주 · {drBackingShortfall(item.drProgram) > 0 ? <span className="tag warn">보관 부족 {number(drBackingShortfall(item.drProgram))}주</span> : <span className="tag ok">시뮬레이터 수량 충족</span>}{item.drProgram.referenceValue !== undefined && <><br />단순 참고가 {number(item.drProgram.referenceValue)} {item.currency} · 현재가 괴리 {number(item.drProgram.referencePremiumPercent ?? 0)}% (±10% 이상 참고 경보 · 환율·비용·유동성 미반영) {item.drProgram.referenceDeviationAlert && <span className="tag warn">괴리 주의</span>}</>}</small>}</td><td>{familyLabel(item.kind)}{item.kind === "currencyproduct" && <span className="tag inline">외환</span>}</td><td>{show(item.exchangeId)}</td><td>{show(item.currency)}</td>
          <td className="r mono">{item.value === null ? "—" : number(item.value)}</td>
          <td><span className={item.status === "active" ? "tag ok" : "tag warn"}>{item.status}</span></td>
          <td className="row-actions">
            {["fund", "indexfund"].includes(item.kind) && <button className="soft small" disabled={busy} onClick={() => { setFundCode(item.code); window.scrollTo({ top: 0, behavior: "smooth" }); }}>펀드 운용</button>}
            {(RELATED[item.kind] ?? []).length > 0 && <select className="mini-select" aria-label="관련 명령어" value="" onChange={(event) => { if (event.target.value) openCommand(event.target.value, item.code); }}><option value="">명령어…</option>{RELATED[item.kind].map(([command, label]) => <option key={command} value={command}>{label} (${command})</option>)}</select>}
            {kindOf(item.kind)?.editable !== false && <button className="soft small" disabled={busy} onClick={() => void startEdit(item)}>수정</button>}
            {!["matured", "settled", "cancelled", "expired"].includes(item.status) && item.drProgram?.programStatus !== "terminated" && <button className="soft small" disabled={busy} onClick={() => toggle(item)}>{item.status === "active" ? (item.kind === "els" ? "취소" : "중지") : "재개"}</button>}
            {item.kind === "dr" && item.drProgram && item.drProgram.programStatus !== "terminated" && <button className="soft small danger" disabled={busy} onClick={() => terminateDr(item)}>DR 종료</button>}
            {item.status !== "active" && !item.drProgram && <button className="soft small danger" disabled={busy} title="중지된 상품을 완전히 삭제" onClick={() => purge(item)}>삭제</button>}
          </td>
        </tr>)}
      </tbody></table>{!visible.length && <p className="empty">표시할 상품이 없습니다.</p>}</div>
    </Card>
  </>;
}

function groupAmount(value: string) {
  const [whole, fraction] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}

function drDeviationLabel(state: string) {
  return ({ normal: "정상 범위", premium: "10% 이상 고평가", discount: "10% 이상 저평가", unavailable: "산출 불가", unobserved: "첫 관측" } as Record<string, string>)[state] ?? state;
}

function drBackingShortfall(program: NonNullable<Product["drProgram"]>) {
  return Math.max(0, program.outstandingReceiptQuantity * program.underlyingPerReceipt - program.custodiedUnderlyingQuantity);
}
