import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, errorText, type Ctx, confirmAction } from "./common";
import "./alm.css";

type Rule = { rule_id: number; nationality: string; customer_type: string; product_type: string; effective_epoch: number; max_rate_bps: number | null;
  apr_display: "required" | "optional" | null; tax_bps: number | null; note: string; created_by: string; created_epoch: number };
type Overview = { rules: Rule[]; countries: { nationality: string; legal_max_rate: number }[] };

const CUSTOMER: [string, string][] = [["*", "모든 고객"], ["individual", "개인"], ["corporate", "법인"]];
const PRODUCT: [string, string][] = [["*", "모든 상품"], ["loan", "대출"], ["deposit", "예금"]];
const label = (table: [string, string][], key: string) => table.find(([id]) => id === key)?.[1] ?? key;
const percent = (bps: number | null) => bps === null ? "—" : `${(bps / 100).toFixed(2)}%`;
const day = (epoch: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium" }).format(epoch * 1000);
const APR: Record<string, string> = { required: "필수", optional: "선택" };

type Insurance = { currencyCode: string; decimalPlaces: number; limitMinorUnits: number; depositorCount: number; accountCount: number; totalDepositsMinorUnits: number;
  coveredMinorUnits: number; uninsuredMinorUnits: number; premiumBps: number; assessedPremiumMinorUnits: number; allocations: Record<string, number>; shortfalls: Record<string, number>; unallocatedMinorUnits: number };
const CLAIMS: [string, string, number][] = [["insurer_subrogation", "보험 대위(보험금을 낸 만큼)", 1], ["uninsured_depositors", "비보험 예금자", 2], ["general_creditors", "일반 채권", 3]];

type FundReport = { fund: Record<string, { premiumsMinorUnits: number; payoutsMinorUnits: number; balanceMinorUnits: number; shortfallMinorUnits: number; decimalPlaces: number }>;
  problems: unknown[]; events: { event_id: number; kind: "premium" | "payout"; issuer: string; currency_code: string; amount_minor_units: number; reference: string; created_epoch: number }[] };

/** 예금보험기금: ledger of premiums collected from banks and payouts recorded from resolved claims (admin-triggered only). */
function FundCard({ ctx, bankId }: { ctx: Ctx; bankId: string }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<FundReport | null>(null);
  const [premium, setPremium] = useState("0.5");
  const load = useCallback(() => { api<FundReport>("/api/admin/deposit-insurance-fund").then(setData).catch((cause) => say(errorText(cause, "기금 정보를 불러오지 못했습니다."), true)); }, []);
  useEffect(load, []);
  const units = (value: number, places: number) => (value / 10 ** places).toLocaleString("ko-KR", { minimumFractionDigits: places, maximumFractionDigits: places });
  const post = (body: Record<string, unknown>, fallback: string, done: string) =>
    run(() => api<{ success: boolean }>("/api/admin/deposit-insurance-fund", { method: "POST", body: JSON.stringify({ bankId, ...body }) }), fallback, () => { say(done); load(); });
  function collect() {
    const bps = Math.round(Number(premium) * 100);
    if (!bankId || !Number.isInteger(bps) || bps < 1 || bps > 10000) { say("보험료율은 0.01~100% 범위로 입력해주세요.", true); return; }
    if (!window.confirm(`${bankId} 은행에 보험 대상 예금의 ${premium}%를 보험료로 징수할까요? 은행 분개에 보험료 비용이 기록되고 기금에 보험료가 쌓입니다.`)) return;
    void post({ action: "collect_premium", premiumBps: bps, eventKey: `ui-${crypto.randomUUID()}` }, "보험료를 징수하지 못했습니다.", "보험료를 징수했습니다.");
  }
  function record() {
    if (!window.confirm(`${bankId} 은행의 정리 청구 가운데 아직 기금에 기록하지 않은 보험금 지급을 기금 원장에 기록할까요?`)) return;
    void post({ action: "record_payouts" }, "지급을 기록하지 못했습니다.", "지급을 기록했습니다.");
  }
  if (!data) return <p className="cap">불러오는 중…</p>;
  const currencies = Object.entries(data.fund);
  return <Card title="예금보험기금" caption="보험료를 모아 한도 안에서 지급합니다. 기능은 관리자가 실행할 때만 작동하며 자동으로 징수·지급하지 않습니다.">
    {currencies.length === 0 ? <p className="cap">기금 거래가 없습니다.</p> : <div className="alm-tiles">{currencies.map(([code, f]) => <div key={code}>
      <span>기금 잔액 · {code}</span><b>{units(f.balanceMinorUnits, f.decimalPlaces)}</b><span>{f.shortfallMinorUnits > 0 ? `부족 ${units(f.shortfallMinorUnits, f.decimalPlaces)}` : `보험료 ${units(f.premiumsMinorUnits, f.decimalPlaces)} · 지급 ${units(f.payoutsMinorUnits, f.decimalPlaces)}`}</span></div>)}</div>}
    {data.problems.length > 0 && <p><span className="tag danger">대사 불일치 {data.problems.length}건</span></p>}
    <div className="scroll-x"><table><thead><tr><th>#</th><th>종류</th><th>은행</th><th>통화</th><th className="alm-num">금액(최소단위)</th><th>비고</th><th>시각</th></tr></thead><tbody>
      {data.events.length === 0 ? <tr><td colSpan={7} className="cap">기록이 없습니다.</td></tr> : data.events.map((e) => <tr key={e.event_id}><td>{e.event_id}</td><td>{e.kind === "premium" ? "보험료" : "지급"}</td><td>{e.issuer}</td><td>{e.currency_code}</td>
        <td className="alm-num">{e.amount_minor_units.toLocaleString("ko-KR")}</td><td>{e.reference}</td><td>{day(e.created_epoch)}</td></tr>)}
    </tbody></table></div>
    <div className="alm-edit">
      <input inputMode="decimal" aria-label="보험료율(%)" placeholder="보험료율 %" value={premium} onChange={(event) => setPremium(event.target.value)} />
      <button type="button" className="btn primary" disabled={busy || !bankId} onClick={collect}>선택한 은행에 보험료 징수</button>
      <button type="button" className="outline" disabled={busy || !bankId} onClick={record}>정리 청구 지급 기록</button>
    </div>
  </Card>;
}

/** 예금보험 · 정리 미리보기: read-only coverage, assessed premium and priority allocation of one bank. */
function InsurancePreview({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [banks, setBanks] = useState<{ id: string; name?: string }[]>([]);
  const [form, setForm] = useState({ bank: "", premium: "0.5", proceeds: "0", general: "0" });
  const [result, setResult] = useState<Insurance | null>(null);
  useEffect(() => {
    api<{ banks: { id: string; name?: string }[] }>("/api/admin/banks").then((data) => { setBanks(data.banks); if (data.banks[0]) setForm((f) => ({ ...f, bank: data.banks[0].id })); })
      .catch((cause) => say(errorText(cause, "은행 목록을 불러오지 못했습니다."), true));
  }, []);
  const units = (value: number, places: number) => (value / 10 ** places).toLocaleString("ko-KR", { minimumFractionDigits: places, maximumFractionDigits: places });
  function load() {
    const premium = Math.round(Number(form.premium) * 100);
    if (!form.bank || !Number.isInteger(premium) || premium < 0 || premium > 10000 || !/^\d{1,15}$/.test(form.proceeds) || !/^\d{1,15}$/.test(form.general)) { say("은행, 보험료율(0~100%), 정수 최소단위 금액을 확인해주세요.", true); return; }
    void run(() => api<Insurance>(`/api/admin/deposit-insurance?bank_id=${encodeURIComponent(form.bank)}&premium_bps=${premium}&proceeds=${form.proceeds}&general_claims=${form.general}`), "미리보기를 불러오지 못했습니다.", setResult);
  }
  const places = result?.decimalPlaces ?? 0;
  return <><Card title="예금보험 · 정리 배분 미리보기" caption="은행의 보험 한도 안 예금과 가정한 회수금의 우선순위 배분을 계산합니다. 실제 전기는 하지 않으며 보험기금 원장은 아직 없어 기금 잔액은 표시하지 않습니다.">
    <div className="alm-edit">
      <select aria-label="은행" value={form.bank} onChange={(event) => setForm({ ...form, bank: event.target.value })}>{banks.map((bank) => <option key={bank.id} value={bank.id}>{bank.name ? `${bank.name} (${bank.id})` : bank.id}</option>)}</select>
      <input inputMode="decimal" aria-label="보험료율(%)" placeholder="보험료율 %" value={form.premium} onChange={(event) => setForm({ ...form, premium: event.target.value })} />
      <input inputMode="numeric" aria-label="회수금(최소단위)" placeholder="회수금(최소단위)" value={form.proceeds} onChange={(event) => setForm({ ...form, proceeds: event.target.value })} />
      <input inputMode="numeric" aria-label="일반 채권 청구(최소단위)" placeholder="일반 채권(최소단위)" value={form.general} onChange={(event) => setForm({ ...form, general: event.target.value })} />
      <button type="button" className="btn primary" disabled={busy || !form.bank} onClick={load}>미리보기</button>
    </div>
    {result && <>
      <div className="alm-tiles"><div><span>보험 한도(1인)</span><b>{units(result.limitMinorUnits, places)}</b></div><div><span>보험 대상 예금</span><b>{units(result.coveredMinorUnits, places)}</b></div>
        <div><span>비보험 예금</span><b>{units(result.uninsuredMinorUnits, places)}</b></div><div><span>예상 보험료(가정)</span><b>{units(result.assessedPremiumMinorUnits, places)}</b></div>
        <div><span>예금자 · 계좌</span><b>{result.depositorCount} · {result.accountCount}</b></div></div>
      <div className="scroll-x"><table><thead><tr><th>순위</th><th>채권자</th><th className="alm-num">청구</th><th className="alm-num">배분</th><th className="alm-num">부족</th></tr></thead><tbody>
        {CLAIMS.map(([key, name, rank]) => <tr key={key}><td>{rank}</td><td>{name}</td>
          <td className="alm-num">{units(result.allocations[key] + result.shortfalls[key], places)}</td><td className="alm-num">{units(result.allocations[key], places)}</td><td className="alm-num">{units(result.shortfalls[key], places)}</td></tr>)}
        <tr className="alm-total"><td /><td>남은 회수금</td><td /><td className="alm-num">{units(result.unallocatedMinorUnits, places)}</td><td /></tr>
      </tbody></table></div>
      <p className="alm-note">통화 {result.currencyCode}. 금액은 은행 기준통화 최소단위로 계산한 미리보기입니다.</p></>}
  </Card><FundCard ctx={ctx} bankId={form.bank} /></>;
}

type ScheduleRow = { product_type: "loan" | "deposit"; product_code: string; schedule_json: string; enabled: number; updated_by: string; updated_epoch: number };
const EXAMPLE = JSON.stringify({ segments: [{ from_day: 0, kind: "fixed", rate: "12" }, { from_day: 365, kind: "floating", spread: "1" }], floor: "2", cap: "20", max_change_per_reset: "1", rounding_step: "0.25" }, null, 1);

/** 상품별 금리 일정: optional mixed/stepped schedules, off unless enabled here; only contracts signed afterwards follow them. */
function ScheduleCard({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [rows, setRows] = useState<ScheduleRow[] | null>(null);
  const [form, setForm] = useState({ type: "loan", code: "", json: EXAMPLE });
  const load = useCallback(() => { api<{ schedules: ScheduleRow[] }>("/api/admin/product-rate-schedule").then((d) => setRows(d.schedules)).catch((cause) => say(errorText(cause, "금리 일정을 불러오지 못했습니다."), true)); }, []);
  useEffect(load, []);
  const summary = (json: string) => { try { const d = JSON.parse(json) as { segments: { from_day: number; kind: string; rate?: string; spread?: string }[]; floor: string | null; cap: string | null };
    return `${d.segments.map((x) => `${x.from_day}일~ ${x.kind === "fixed" ? `고정 ${x.rate}%` : `변동 지표+${x.spread}`}`).join(" → ")} · 하한/상한 ${d.floor ?? "—"} / ${d.cap ?? "—"}`; } catch { return "—"; } };
  function save(code: string, type: string, json: string, enabled: boolean) {
    let schedule: unknown;
    try { schedule = JSON.parse(json); } catch { say("금리 일정 JSON 형식을 확인해주세요.", true); return; }
    if (!code.trim()) { say("상품 코드를 입력해주세요.", true); return; }
    const text = enabled ? `${code} 상품에 금리 일정을 켭니다. 이후 새로 체결하는 ${type === "loan" ? "대출" : "예금"} 계약부터 이 일정이 적용되고 기존 계약은 바뀌지 않습니다. 계속할까요?` : `${code} 상품의 금리 일정을 끕니다. 이미 체결한 계약은 자기 일정을 유지합니다. 계속할까요?`;
    if (!window.confirm(text)) return;
    void run(() => api<{ success: boolean }>("/api/admin/product-rate-schedule", { method: "POST", body: JSON.stringify({ action: "set", productType: type, productCode: code.trim(), schedule, enabled }) }),
      "금리 일정을 저장하지 못했습니다.", () => { say(enabled ? "금리 일정을 켰습니다." : "금리 일정을 껐습니다."); load(); });
  }
  if (!rows) return <p className="cap">불러오는 중…</p>;
  return <Card title="상품별 금리 일정" caption="고정→변동 전환, 하한·상한, 1회 변동폭 제한. 기본은 꺼짐이며 켠 뒤 새로 체결하는 계약에만 적용됩니다.">
    <div className="scroll-x"><table><thead><tr><th>상품</th><th>종류</th><th>일정</th><th>상태</th><th>수정</th><th /></tr></thead><tbody>
      {rows.length === 0 ? <tr><td colSpan={6} className="cap">설정된 금리 일정이 없습니다. 모든 상품은 기존 금리 방식을 따릅니다.</td></tr> : rows.map((row) => <tr key={`${row.product_type}${row.product_code}`}>
        <td>{row.product_code}</td><td>{row.product_type === "loan" ? "대출" : "예금"}</td><td>{summary(row.schedule_json)}</td>
        <td>{row.enabled ? <span className="tag ok">켜짐</span> : <span className="tag warn">꺼짐</span>}</td><td>{row.updated_by} · {day(row.updated_epoch)}</td>
        <td><button type="button" className="outline" disabled={busy} onClick={() => save(row.product_code, row.product_type, row.schedule_json, !row.enabled)}>{row.enabled ? "끄기" : "켜기"}</button></td></tr>)}
    </tbody></table></div>
    <div className="alm-edit">
      <select aria-label="종류" value={form.type} onChange={(event) => setForm({ ...form, type: event.target.value })}><option value="loan">대출</option><option value="deposit">예금</option></select>
      <input aria-label="상품 코드" placeholder="상품 코드" value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} />
      <textarea aria-label="금리 일정(JSON)" rows={8} cols={46} value={form.json} onChange={(event) => setForm({ ...form, json: event.target.value })} />
      <button type="button" className="btn primary" disabled={busy} onClick={() => save(form.code, form.type, form.json, true)}>일정 저장하고 켜기</button>
    </div>
    <p className="alm-note">일정 구간은 계약 체결일 기준 일수로 시작하며, 변동 구간은 상품의 지표금리에 스프레드를 더합니다. 최고금리 규칙과 법정최고이자가 항상 상한으로 적용됩니다.</p>
  </Card>;
}

/** 금리 · 한도 · 정리: dated country rules (maximum rate, APR display, tax). Rules apply by contract date and are never edited, only superseded. */
export function RatesPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<Overview | null>(null);
  const [form, setForm] = useState({ nationality: "", customer: "*", product: "*", date: "", maxRate: "", apr: "", tax: "", note: "" });

  const load = useCallback(() => {
    api<Overview>("/api/admin/rate-rules").then(setData).catch((cause) => say(errorText(cause, "국가별 규칙을 불러오지 못했습니다."), true));
  }, []);
  useEffect(load, []);

  const bps = (text: string) => text.trim() === "" ? null : Math.round(Number(text) * 100);

  async function submit() {
    const max = bps(form.maxRate), tax = bps(form.tax);
    const valid = (value: number | null) => value === null || (Number.isInteger(value) && value >= 0 && value <= 10000);
    if (!form.nationality || !form.date) { say("국가와 적용일을 입력해주세요.", true); return; }
    if (!valid(max) || !valid(tax) || (form.maxRate && !/^\d{1,3}(\.\d{1,2})?$/.test(form.maxRate)) || (form.tax && !/^\d{1,3}(\.\d{1,2})?$/.test(form.tax))) { say("최고금리와 세율은 0~100%(소수 둘째 자리까지)로 입력해주세요.", true); return; }
    if (max === null && !form.apr && tax === null) { say("최고금리, APR 표시, 세율 중 하나 이상을 정해주세요.", true); return; }
    const epoch = Math.floor(new Date(`${form.date}T00:00:00`).getTime() / 1000);
    if (!Number.isFinite(epoch) || epoch < 0) { say("적용일을 확인해주세요.", true); return; }
    if (!await confirmAction(`${form.nationality} · ${label(CUSTOMER, form.customer)} · ${label(PRODUCT, form.product)} 규칙을 ${form.date}부터 적용할까요? 이 날짜 이후에 체결하는 계약에만 적용되며, 등록한 규칙은 고치거나 지울 수 없습니다.`)) return;
    void run(() => api<{ success: boolean; ruleId: number }>("/api/admin/rate-rules", { method: "POST", body: JSON.stringify({
      action: "add_rule", nationality: form.nationality, customerType: form.customer, productType: form.product, effectiveEpoch: epoch,
      maxRateBps: max, aprDisplay: form.apr || null, taxBps: tax, note: form.note.trim() }) }), "규칙을 저장하지 못했습니다.", (response) => {
      say(`규칙 #${response.ruleId}을 등록했습니다.`); setForm({ ...form, maxRate: "", apr: "", tax: "", note: "" }); load();
    });
  }

  if (!data) return <p className="cap">불러오는 중…</p>;
  return <>
    <ScheduleCard ctx={ctx} />
    <Card title="국가별 규칙" caption="최고금리, APR 표시, 세금. 계약일 기준 규칙이 적용됩니다. 규칙이 없으면 국가의 법정최고이자가 그대로 적용됩니다.">
      <div className="scroll-x"><table><thead><tr><th>국가</th><th>고객</th><th>상품</th><th className="alm-num">최고금리</th><th>APR 표시</th><th className="alm-num">세율</th><th>적용일</th><th>메모</th><th>등록</th></tr></thead><tbody>
        {data.rules.length === 0 ? <tr><td colSpan={9} className="cap">등록된 규칙이 없습니다. 국가별 법정최고이자({data.countries.map((c) => `${c.nationality} ${c.legal_max_rate}%`).join(" · ") || "—"})가 적용됩니다.</td></tr> : data.rules.map((row) => <tr key={row.rule_id}>
          <td>{row.nationality}</td><td>{label(CUSTOMER, row.customer_type)}</td><td>{label(PRODUCT, row.product_type)}</td><td className="alm-num">{percent(row.max_rate_bps)}</td>
          <td>{row.apr_display ? APR[row.apr_display] : "—"}</td><td className="alm-num">{percent(row.tax_bps)}</td><td>{day(row.effective_epoch)}</td><td>{row.note || "—"}</td><td>{row.created_by}</td></tr>)}
      </tbody></table></div>
      <p className="alm-note">현재 실제 계약에 반영되는 값은 최고금리(신규 대출의 금리·산입비용 상한)뿐입니다. APR 표시와 세율은 기록만 하며 아직 계산에 쓰지 않습니다.</p>
    </Card>
    <Card title="규칙 추가" caption="같은 국가·고객·상품·적용일에는 한 번만 등록할 수 있습니다. 바꾸려면 더 늦은 적용일로 새 규칙을 등록합니다.">
      <div className="alm-edit">
        <select aria-label="국가" value={form.nationality} onChange={(event) => setForm({ ...form, nationality: event.target.value })}><option value="">국가 선택</option>{data.countries.map((c) => <option key={c.nationality} value={c.nationality}>{c.nationality}</option>)}</select>
        <select aria-label="고객 유형" value={form.customer} onChange={(event) => setForm({ ...form, customer: event.target.value })}>{CUSTOMER.map(([id, text]) => <option key={id} value={id}>{text}</option>)}</select>
        <select aria-label="상품 유형" value={form.product} onChange={(event) => setForm({ ...form, product: event.target.value })}>{PRODUCT.map(([id, text]) => <option key={id} value={id}>{text}</option>)}</select>
        <input type="date" aria-label="적용일" value={form.date} onChange={(event) => setForm({ ...form, date: event.target.value })} />
        <input inputMode="decimal" aria-label="최고금리(%)" placeholder="최고금리 %" value={form.maxRate} onChange={(event) => setForm({ ...form, maxRate: event.target.value })} />
        <select aria-label="APR 표시" value={form.apr} onChange={(event) => setForm({ ...form, apr: event.target.value })}><option value="">APR 표시 (정하지 않음)</option><option value="required">필수</option><option value="optional">선택</option></select>
        <input inputMode="decimal" aria-label="세율(%)" placeholder="세율 %" value={form.tax} onChange={(event) => setForm({ ...form, tax: event.target.value })} />
        <input aria-label="메모" placeholder="메모(선택)" maxLength={300} value={form.note} onChange={(event) => setForm({ ...form, note: event.target.value })} />
        <button type="button" className="btn primary" disabled={busy} onClick={submit}>규칙 등록</button>
      </div>
    </Card>
    <InsurancePreview ctx={ctx} />
  </>;
}
