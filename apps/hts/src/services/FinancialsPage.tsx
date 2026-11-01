import { minorMoney, moneyCompare, moneyAdd, type Money } from "../../../shared/money";
import { useEffect, useState } from "react";
import "./FinancialsPage.css";
import type { FormEvent } from "react";
import { ActionFields, initialValues, number } from "../commandKit";
import type { ServiceProps } from "./ServiceKit";

type Report = {
  code: string; name: string; currency: string; places: number; period: string;
  balances: Partial<Record<string, Money>>; opening: Partial<Record<string, Money>>; changes: Partial<Record<string, Money>>;
  summary: Partial<Record<string, Money>>; previous: Partial<Record<string, Money>>; income: Partial<Record<string, Money>>;
  cash_flows: Partial<Record<string, Money>>; capex: Money; fcf: Money; eps: Money | null;
  ratios: Record<string, { value: number | null; unit: string }>; checks: Record<string, boolean | null>; closed: boolean; notes: string[];
};

const ACCOUNTS: Record<string, string> = {
  cash: "현금", restricted_cash: "사용제한 현금", receivables: "매출채권", inventory: "재고자산", short_investments: "단기투자자산",
  investments: "장기투자자산", ppe: "유형자산", depreciation_accumulated: "감가상각누계액", intangibles: "무형자산",
  amortization_accumulated: "상각누계액", treasury_receivable: "국고 미수금", payables: "매입채무", accrued_expenses: "미지급비용",
  short_debt: "단기차입금", dividend_payable: "미지급배당금", long_debt: "장기차입금", els_liability: "ELS 부채", bonds: "사채",
  bond_discount: "사채할인발행차금", common_capital: "보통주 자본금", preferred_capital: "우선주 자본금", retained: "이익잉여금", capital_surplus: "자본잉여금", treasury_stock: "자기주식", opening_adjustment: "도입 전 미확인 잔액", adjustment: "기타 자본조정",
};
const SUMMARY: [string, string][] = [["current_assets", "유동자산"], ["noncurrent_assets", "비유동자산"], ["assets", "자산 총계"],
  ["current_liabilities", "유동부채"], ["noncurrent_liabilities", "비유동부채"], ["liabilities", "부채 총계"], ["equity", "자본 총계"]];
const INCOME: [string, string][] = [["revenue", "매출액"], ["gross", "매출총이익"], ["operating", "영업이익"], ["ebitda", "EBITDA"],
  ["pretax", "법인세차감전이익"], ["tax", "법인세"], ["profit", "당기순이익"]];
const FLOWS: [string, string][] = [["opening", "원장 도입 현금"], ["operating", "영업활동"], ["investing", "투자활동"], ["financing", "재무활동"], ["adjustment", "조정"], ["none", "미분류"]];

/** 기업 재무제표 on its own page: summary figures first, then each statement as a readable table. */
export function FinancialsPage({ kit }: ServiceProps) {
  const command = kit.commands.find((item) => item.name === "재무");
  const [values, setValues] = useState<Record<string, string>>({});
  const [report, setReport] = useState<Report | null>(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  // A link like /financials?code=SELOB opens straight on that company.
  const preset = new URLSearchParams(location.search).get("code") ?? "";
  useEffect(() => {
    if (!command) return;
    const start = { ...initialValues(command), ...(preset ? { product: preset } : {}) };
    setValues(start);
    if (preset) void lookup(start);
  }, [command]);
  if (!command) return <p className="svc-empty">재무제표 조회를 쓸 수 없습니다.</p>;

  async function lookup(input: Record<string, string>) {
    setLoading(true);
    let result;
    try { result = await kit.execute(command!, input, false); } finally { setLoading(false); }
    if (!result) return;
    if (result.success && result.data && typeof result.data === "object") { setReport(result.data as Report); setMessage(""); }
    else { setReport(null); setMessage(result.message); }
  }
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); void lookup(values); }

  const difference = (value: Money | null | undefined, previous: Money | null | undefined) => value == null || previous == null ? null : moneyAdd(value, previous, true);
  const money = (value: Money | null | undefined) => value === null || value === undefined ? "—" : number(minorMoney(value, report?.places ?? 0), report?.places ?? 0);
  return <div className="fin-page" aria-busy={loading}>
    <form className="svc-card fin-search" onSubmit={submit}>
      <ActionFields command={command} values={values} onChange={setValues} sources={kit.sources} />
      <button className="svc-submit" disabled={kit.busy || loading}>{kit.busy || loading ? "조회 중…" : "재무제표 보기"}</button>
    </form>
    {message && <p className="account-error" role="alert">{message}</p>}
    {!report && !message && <p className="fin-empty" role="status">회사 코드를 선택하면 재무상태·손익·현금흐름을 함께 확인할 수 있습니다.</p>}
    {report && <article className="fin-report" aria-label={`${report.name} 재무제표`}>
      <header className="fin-head">
        <div><h2>{report.name} <span>{report.code}</span></h2><p>{report.period} · {report.currency} · {report.closed ? "마감된 기간" : "진행 중인 기간"}</p></div>
        <div className="fin-checks">{Object.entries(report.checks).map(([key, ok]) => <span key={key} className={ok === false ? "bad" : "ok"}>{{ balance_sheet: "대차", cash_flow: "현금흐름", live_cash: "실제 현금" }[key] ?? key} {ok === null ? "—" : ok ? "일치" : "불일치"}</span>)}</div>
      </header>
      <section className="fin-figures" aria-label="요약">
        {[["자산", report.summary.assets], ["부채", report.summary.liabilities], ["자본", report.summary.equity], ["당기순이익", report.income.profit], ["잉여현금흐름", report.fcf]].map(([label, value]) =>
          <div key={label as string}><small>{label}</small><b>{money(value as Money)}</b></div>)}
      </section>
      <div className="fin-grid">
        <section className="fin-table"><h3>재무상태표</h3><p className="fin-unit">표시 통화 {report.currency} · 차변 + / 대변 −</p><table><thead><tr><th>계정</th><th>기초</th><th>증감</th><th>기말</th></tr></thead><tbody>
          {Object.keys(ACCOUNTS).filter((key) => report.balances[key] || report.opening[key]).map((key) => <tr key={key}><th>{ACCOUNTS[key] ?? key}</th><td>{money(report.opening[key])}</td><td className={moneyCompare(report.changes[key] ?? 0, 0) > 0 ? "up" : moneyCompare(report.changes[key] ?? 0, 0) < 0 ? "down" : undefined}>{money(report.changes[key])}</td><td>{money(report.balances[key])}</td></tr>)}
          {SUMMARY.map(([key, label]) => <tr key={key} className="fin-total"><th>{label}</th><td>{money(report.previous[key])}</td><td>{money(difference(report.summary[key], report.previous[key]))}</td><td>{money(report.summary[key])}</td></tr>)}
        </tbody></table></section>
        <div className="fin-side">
          <section className="fin-table"><h3>손익계산서</h3><table><tbody>{INCOME.map(([key, label]) => <tr key={key} className={key === "profit" ? "fin-total" : undefined}><th>{label}</th><td>{money(report.income[key])}</td></tr>)}
            <tr><th>주당순이익(EPS)</th><td>{report.eps == null ? "—" : number(minorMoney(report.eps, report.places), Math.max(2, report.places))}</td></tr></tbody></table></section>
          <section className="fin-table"><h3>현금흐름표</h3><table><tbody>{FLOWS.map(([key, label]) => <tr key={key}><th>{label}</th><td>{money(report.cash_flows[key])}</td></tr>)}
            <tr><th>설비투자(CAPEX)</th><td>{money(report.capex)}</td></tr><tr className="fin-total"><th>잉여현금흐름</th><td>{money(report.fcf)}</td></tr></tbody></table></section>
        </div>
      </div>
      <section className="fin-ratios"><h3>주요 비율</h3><dl>{Object.entries(report.ratios).map(([label, ratio]) => <div key={label}><dt>{label}</dt><dd>{ratio.value === null ? "—" : `${number(ratio.value, 2)} ${ratio.unit}`}</dd></div>)}</dl></section>
      {report.notes.length > 0 && <ul className="fin-notes">{report.notes.map((note) => <li key={note}>{note}</li>)}</ul>}
    </article>}
  </div>;
}
