import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { ActionFields, initialValues, number } from "../commandKit";
import type { ServiceProps } from "./ServiceKit";

type Report = {
  code: string; name: string; currency: string; places: number; period: string;
  balances: Record<string, number>; opening: Record<string, number>; changes: Record<string, number>;
  summary: Record<string, number>; previous: Record<string, number>; income: Record<string, number>;
  cash_flows: Record<string, number>; capex: number; fcf: number; eps: number | null;
  ratios: Record<string, number | null>; checks: Record<string, boolean | null>; closed: boolean; notes: string[];
};

const ACCOUNTS: Record<string, string> = {
  cash: "현금", restricted_cash: "사용제한 현금", receivables: "매출채권", inventory: "재고자산", short_investments: "단기투자자산",
  investments: "장기투자자산", ppe: "유형자산", depreciation_accumulated: "감가상각누계액", intangibles: "무형자산",
  amortization_accumulated: "상각누계액", treasury_receivable: "국고 미수금", payables: "매입채무", accrued_expenses: "미지급비용",
  short_debt: "단기차입금", dividend_payable: "미지급배당금", long_debt: "장기차입금", els_liability: "ELS 부채", bonds: "사채",
  bond_discount: "사채할인발행차금", common_capital: "보통주 자본금", preferred_capital: "우선주 자본금", retained_earnings: "이익잉여금",
};
const SUMMARY: [string, string][] = [["current_assets", "유동자산"], ["noncurrent_assets", "비유동자산"], ["assets", "자산 총계"],
  ["current_liabilities", "유동부채"], ["noncurrent_liabilities", "비유동부채"], ["liabilities", "부채 총계"], ["equity", "자본 총계"]];
const INCOME: [string, string][] = [["revenue", "매출액"], ["gross", "매출총이익"], ["operating", "영업이익"], ["ebitda", "EBITDA"],
  ["pretax", "법인세차감전이익"], ["tax", "법인세"], ["profit", "당기순이익"]];
const FLOWS: [string, string][] = [["opening", "기초 현금"], ["operating", "영업활동"], ["investing", "투자활동"], ["financing", "재무활동"], ["adjustment", "조정"]];

/** 기업 재무제표 on its own page: summary figures first, then each statement as a readable table. */
export function FinancialsPage({ kit }: ServiceProps) {
  const command = kit.commands.find((item) => item.name === "재무");
  const [values, setValues] = useState<Record<string, string>>({});
  const [report, setReport] = useState<Report | null>(null);
  const [message, setMessage] = useState("");
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
    const result = await kit.execute(command!, input, false);
    if (!result) return;
    if (result.success && result.data && typeof result.data === "object") { setReport(result.data as Report); setMessage(""); }
    else { setReport(null); setMessage(result.message); }
  }
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); void lookup(values); }

  const money = (value: number | null | undefined) => value === null || value === undefined ? "—" : number(value, report?.places ?? 0);
  return <div className="fin-page">
    <form className="svc-card fin-search" onSubmit={submit}>
      <ActionFields command={command} values={values} onChange={setValues} sources={kit.sources} />
      <button className="svc-submit" disabled={kit.busy}>{kit.busy ? "조회 중…" : "재무제표 보기"}</button>
    </form>
    {message && <p className="account-error" role="alert">{message}</p>}
    {report && <article className="fin-report" aria-label={`${report.name} 재무제표`}>
      <header className="fin-head">
        <div><h2>{report.name} <span>{report.code}</span></h2><p>{report.period} · {report.currency} · {report.closed ? "마감된 기간" : "진행 중인 기간"}</p></div>
        <div className="fin-checks">{Object.entries(report.checks).map(([key, ok]) => <span key={key} className={ok === false ? "bad" : "ok"}>{{ balance_sheet: "대차", cash_flow: "현금흐름", live_cash: "실제 현금" }[key] ?? key} {ok === null ? "—" : ok ? "일치" : "불일치"}</span>)}</div>
      </header>
      <section className="fin-figures" aria-label="요약">
        {[["자산", report.summary.assets], ["부채", report.summary.liabilities], ["자본", report.summary.equity], ["당기순이익", report.income.profit], ["잉여현금흐름", report.fcf]].map(([label, value]) =>
          <div key={label as string}><small>{label}</small><b>{money(value as number)}</b></div>)}
      </section>
      <div className="fin-grid">
        <section className="fin-table"><h3>재무상태표</h3><table><thead><tr><th>계정</th><th>기초</th><th>증감</th><th>기말</th></tr></thead><tbody>
          {Object.keys(report.balances).filter((key) => report.balances[key] || report.opening[key]).map((key) => <tr key={key}><th>{ACCOUNTS[key] ?? key}</th><td>{money(report.opening[key])}</td><td className={report.changes[key] > 0 ? "up" : report.changes[key] < 0 ? "down" : undefined}>{money(report.changes[key])}</td><td>{money(report.balances[key])}</td></tr>)}
          {SUMMARY.map(([key, label]) => <tr key={key} className="fin-total"><th>{label}</th><td>{money(report.previous[key])}</td><td>{money(report.summary[key] - (report.previous[key] ?? 0))}</td><td>{money(report.summary[key])}</td></tr>)}
        </tbody></table></section>
        <div className="fin-side">
          <section className="fin-table"><h3>손익계산서</h3><table><tbody>{INCOME.map(([key, label]) => <tr key={key} className={key === "profit" ? "fin-total" : undefined}><th>{label}</th><td>{money(report.income[key])}</td></tr>)}
            <tr><th>주당순이익(EPS)</th><td>{report.eps === null ? "—" : number(report.eps, 2)}</td></tr></tbody></table></section>
          <section className="fin-table"><h3>현금흐름표</h3><table><tbody>{FLOWS.map(([key, label]) => <tr key={key}><th>{label}</th><td>{money(report.cash_flows[key])}</td></tr>)}
            <tr><th>설비투자(CAPEX)</th><td>{money(report.capex)}</td></tr><tr className="fin-total"><th>잉여현금흐름</th><td>{money(report.fcf)}</td></tr></tbody></table></section>
        </div>
      </div>
      <section className="fin-ratios"><h3>주요 비율</h3><dl>{Object.entries(report.ratios).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value === null ? "—" : `${number(value, 2)}`}</dd></div>)}</dl></section>
      {report.notes.length > 0 && <ul className="fin-notes">{report.notes.map((note) => <li key={note}>{note}</li>)}</ul>}
    </article>}
  </div>;
}
