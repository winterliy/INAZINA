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
  return <Card title="예금보험 · 정리 배분 미리보기" caption="은행의 보험 한도 안 예금과 가정한 회수금의 우선순위 배분을 계산합니다. 실제 전기는 하지 않으며 보험기금 원장은 아직 없어 기금 잔액은 표시하지 않습니다.">
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
