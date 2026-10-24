import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Field, number, plain, show, type Ctx } from "./common";
import { CompanyLedger } from "./CompanyLedger";

type Holding = { asset_code: string; quantity: number };
type Company = { company_code: string; name: string; exchange_id: string; total_shares: number; share_price: number; cash: number; locked: number; account: string | null; holdings: Holding[]; accounts: number; loans: number; parents: { code: string; percent: number }[] };
type CommandField = { key: string; label: string; kind: string; optional: boolean; hint: string };
type Command = { name: string; group: string; description: string; guide: string; fields: CommandField[] };
type Stock = { name: string; code: string; qty: number; value: number; price: number };
type Account = { number: string; product_name: string; balance: number };
type Assets = { cash: number; grand_total: number; loan_total: number; net_worth: number; stocks: Stock[]; accounts: Account[] };
type Detail = { account: string; webLogin: boolean; loginId: string; assets: Assets };
type Member = { code: string; name: string; parent: string; ownership: number; currency: string; goodwill: number; nci: number; depth?: number };
type Consolidated = { subsidiaries: Member[]; summary: { assets: number; liabilities: number; equity: number; parent_equity: number; noncontrolling_interest: number; goodwill: number }; income: Record<string, number>; profit_attributable: { parent: number; noncontrolling: number }; eliminations: { dividends: number; transfers: number; cross_holdings: number }; notes: string[] } | null;
type Report = { period: string; currency: string; places: number; summary: { assets: number; liabilities: number; equity: number }; income: Record<string, number>; changes: Record<string, number>; checks: { balance_sheet: boolean; live_cash: boolean | null }; consolidated: Consolidated };
type Result = { message: string; success: boolean };

export function CompaniesPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say, query } = ctx;
  const [companies, setCompanies] = useState<Company[]>([]);
  const [commands, setCommands] = useState<Command[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [command, setCommand] = useState("매수");
  const [values, setValues] = useState<Record<string, string>>({});
  const [password, setPassword] = useState("");
  const [report, setReport] = useState<{ report: Report; periods: string[] } | null>(null);
  const [period, setPeriod] = useState("");

  const load = useCallback(async () => {
    const result = await api<{ companies: Company[]; commands: Command[] }>("/api/admin/companies");
    setCompanies(result.companies);
    setCommands(result.commands);
  }, []);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "법인 자본을 불러오지 못했습니다.", true)); }, [load]);

  const open = useCallback(async (code: string) => {
    setSelected(code);
    setDetail(null);
    setReport(null);
    setDetail(await api<Detail>(`/api/admin/companies/${encodeURIComponent(code)}`));
  }, []);

  const spec = commands.find((item) => item.name === command);
  const company = companies.find((item) => item.company_code === selected);
  const visible = companies.filter((item) => !query.trim() || `${item.company_code} ${item.name}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));

  function submit() {
    if (!company || !spec) return;
    if (!window.confirm(`${company.name}(${company.company_code}) 법인 계정으로 ${command}을(를) 실행할까요?`)) return;
    void run(() => api<Result>(`/api/admin/companies/${encodeURIComponent(company.company_code)}`, { method: "POST", body: JSON.stringify({ command, values }) }),
      "법인 명령을 실행하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); setValues({}); await load(); await open(company.company_code); });
  }

  function saveLogin() {
    if (!company) return;
    if (!window.confirm(`${company.name} 법인 계정의 웹·HTS 비밀번호를 설정할까요?\n로그인 ID는 ${company.company_code}입니다.`)) return;
    void run(() => api<{ account: { message: string } }>("/api/admin/companies/login", { method: "POST", body: JSON.stringify({ companyId: company.company_code, password }) }),
      "법인 로그인을 설정하지 못했습니다.", async (result) => { say(plain(result.account.message)); setPassword(""); await open(company.company_code); });
  }

  function loadReport(next = period) {
    if (!company) return;
    void run(() => api<{ report: Report; periods: string[] }>(`/api/admin/companies/${encodeURIComponent(company.company_code)}/financial${next ? `?period=${encodeURIComponent(next)}` : ""}`),
      "재무제표를 불러오지 못했습니다.", (result) => { setReport(result); setPeriod(result.report.period); });
  }

  const money = (value: number) => report ? `${number(value / 10 ** report.report.places)} ${report.report.currency}` : "—";

  return <>
    {company && <Card title={`${company.name} (${company.company_code}) 법인 자본`} caption={<>계정 <span className="mono">{detail?.account ?? company.account ?? "첫 실행 때 개설"}</span> · 현금은 법인 현금 장부와 같은 돈입니다 · 거래는 재무제표에 자동 기록됩니다</>}
      actions={<button className="soft" type="button" onClick={() => { setSelected(""); setDetail(null); }}>닫기</button>}>
      <div className="stats">
        <div><small>현금</small><b>{number(company.cash)} 기준 통화</b></div>
        <div><small>잠금 예치금</small><b>{number(company.locked)}</b></div>
        <div><small>총자산</small><b>{detail ? number(Math.round(detail.assets.grand_total)) : "…"}</b></div>
        <div><small>대출</small><b>{number(company.loans)}</b></div>
        <div><small>순자산</small><b>{detail ? number(Math.round(detail.assets.net_worth)) : "…"}</b></div>
      </div>
      {company.parents.length > 0 && <p className="hint">모회사(지분 보유 법인): {company.parents.map((parent) => `${parent.code} ${parent.percent.toFixed(1)}%`).join(", ")}</p>}
      <h3 className="sub-head">보유 자산</h3>
      <div className="scroll-x"><table><thead><tr><th>종목</th><th>이름</th><th className="r">수량</th><th className="r">평가액</th><th /></tr></thead><tbody>
        {(detail?.assets.stocks ?? []).map((stock) => <tr key={stock.code}><td className="mono">{stock.code}</td><td>{stock.name}</td><td className="r mono">{number(stock.qty)}</td><td className="r mono">{number(Math.round(stock.value))}</td><td>{stock.code === company.company_code ? <span className="tag">자기주식</span> : companies.some((item) => item.company_code === stock.code && item.parents.some((parent) => parent.code === company.company_code && parent.percent > 50)) ? <span className="tag ok">자회사</span> : null}</td></tr>)}
      </tbody></table>{detail && !detail.assets.stocks.length && <p className="empty">보유 자산이 없습니다.</p>}</div>
      <h3 className="sub-head">통장</h3>
      <div className="scroll-x"><table><thead><tr><th>계좌</th><th>상품</th><th className="r">잔액</th></tr></thead><tbody>
        {(detail?.assets.accounts ?? []).map((account) => <tr key={account.number}><td className="mono">{account.number}</td><td>{account.product_name}</td><td className="r mono">{number(account.balance)}</td></tr>)}
      </tbody></table>{detail && !detail.assets.accounts.length && <p className="empty">통장이 없습니다.</p>}</div>
      <h3 className="sub-head">웹·HTS 로그인</h3>
      <p className="hint">{detail?.webLogin ? <>로그인 ID <span className="mono">{detail.loginId}</span>로 HTS에 로그인할 수 있습니다. 비밀번호를 다시 설정하면 기존 로그인은 만료됩니다.</> : "아직 로그인이 없습니다. 비밀번호를 정하면 법인 코드를 ID로 HTS에 로그인해 이 계정으로 거래합니다."}</p>
      <form className="form-grid" onSubmit={(event) => { event.preventDefault(); saveLogin(); }}>
        <Field label="비밀번호" hint="12자 이상 · 영문과 숫자 포함"><input type="password" value={password} minLength={12} required onChange={(event) => setPassword(event.target.value)} /></Field>
        <button className="btn primary" disabled={busy || !password}>{detail?.webLogin ? "비밀번호 재설정" : "로그인 만들기"}</button>
      </form>

      <h3 className="sub-head">재무제표 · 연결재무제표</h3>
      <div className="actions">
        <button className="btn primary small" type="button" disabled={busy} onClick={() => loadReport()}>{report ? "새로고침" : "불러오기"}</button>
        {report && <select value={period} onChange={(event) => { setPeriod(event.target.value); loadReport(event.target.value); }}>{Array.from(new Set([report.report.period, ...report.periods])).map((item) => <option key={item} value={item}>{item}</option>)}</select>}
      </div>
      {report && <>
        <div className="stats">
          <div><small>총자산</small><b>{money(report.report.summary.assets)}</b></div>
          <div><small>총부채</small><b>{money(report.report.summary.liabilities)}</b></div>
          <div><small>자기자본</small><b>{money(report.report.summary.equity)}</b></div>
          <div><small>당기순이익</small><b>{money(report.report.income.profit)}</b></div>
        </div>
        <div className="scroll-x"><table><thead><tr><th>손익 항목 ({report.report.period})</th><th className="r">금액</th></tr></thead><tbody>
          {([["매출액", -(report.report.changes.revenue ?? 0)], ["배당금·기타 영업외수익", -(report.report.changes.other_income ?? 0)], ["이자수익", -(report.report.changes.interest_income ?? 0)], ["투자자산처분이익", -(report.report.changes.investment_gain ?? 0)], ["투자자산처분손실", report.report.changes.investment_loss ?? 0], ["이자비용", report.report.changes.interest_expense ?? 0], ["연체이자비용", report.report.changes.overdue_interest ?? 0], ["영업외비용", report.report.changes.other_expense ?? 0], ["법인세비용", report.report.changes.income_tax ?? 0]] as [string, number][]).map(([label, value]) => <tr key={label}><td>{label}</td><td className="r mono">{money(value)}</td></tr>)}
        </tbody></table></div>
        <p className="hint">대차 {report.report.checks.balance_sheet ? "일치" : "불일치"} · 현금 대사 {report.report.checks.live_cash === null ? "해당 없음" : report.report.checks.live_cash ? "일치" : "불일치"}</p>
        {report.report.consolidated?.subsidiaries?.length ? <>
          <h3 className="sub-head">연결재무제표</h3>
          <div className="stats">
            <div><small>연결 총자산</small><b>{money(report.report.consolidated.summary.assets)}</b></div>
            <div><small>연결 총부채</small><b>{money(report.report.consolidated.summary.liabilities)}</b></div>
            <div><small>지배기업 지분</small><b>{money(report.report.consolidated.summary.parent_equity)}</b></div>
            <div><small>비지배지분</small><b>{money(report.report.consolidated.summary.noncontrolling_interest)}</b></div>
            <div><small>영업권</small><b>{money(report.report.consolidated.summary.goodwill)}</b></div>
            <div><small>지배기업 귀속 순이익</small><b>{money(report.report.consolidated.profit_attributable.parent)}</b></div>
          </div>
          <div className="scroll-x"><table><thead><tr><th>자회사</th><th>모회사</th><th className="r">지분</th><th>통화</th><th className="r">영업권</th><th className="r">비지배지분</th></tr></thead><tbody>
            {report.report.consolidated.subsidiaries.map((member) => <tr key={member.code}><td>{"└ ".repeat(member.depth ?? 1)}{member.name} <span className="mono">({member.code})</span></td><td className="mono">{member.parent}</td><td className="r">{member.ownership.toFixed(1)}%</td><td>{member.currency}</td><td className="r mono">{money(member.goodwill)}</td><td className="r mono">{money(member.nci)}</td></tr>)}
          </tbody></table></div>
          <p className="hint">내부거래 제거 · 그룹 내 배당 {money(report.report.consolidated.eliminations.dividends)} · 그룹 내 송금 {money(report.report.consolidated.eliminations.transfers)} · 상호주 {money(report.report.consolidated.eliminations.cross_holdings)}</p>
          <details className="help"><summary>연결 기준</summary><ul>{report.report.consolidated.notes.map((note) => <li key={note}>{note}</li>)}</ul></details>
        </> : <p className="hint">과반 지분을 가진 자회사가 없어 연결재무제표가 없습니다.</p>}
      </>}

      <CompanyLedger key={company.company_code} code={company.company_code} ctx={ctx} onChanged={() => { if (report) loadReport(); }} />

      <h3 className="sub-head">법인 계정으로 실행</h3>
      <div className="kind-grid">{commands.map((item) => <button key={item.name} type="button" className={item.name === command ? "command-chip active" : "command-chip"} onClick={() => { setCommand(item.name); setValues({}); }}><b>{item.name}</b><small>{item.group}</small></button>)}</div>
      {spec && <form className="form-grid" onSubmit={(event) => { event.preventDefault(); submit(); }}>
        <p className="hint span-wide">{spec.description} {spec.guide}</p>
        {spec.fields.map((field) => <Field key={field.key} label={`${field.label}${field.optional ? " (선택)" : ""}`} hint={field.hint}>
          {field.kind.startsWith("select:")
            ? <select value={values[field.key] ?? ""} onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}><option value="">선택</option>{field.kind.slice(7).split(",").map((option) => <option key={option} value={option}>{option}</option>)}</select>
            : <input type={field.kind === "password" ? "password" : "text"} inputMode={field.kind === "number" ? "decimal" : undefined} value={values[field.key] ?? ""} required={!field.optional} onChange={(event) => setValues({ ...values, [field.key]: event.target.value })} />}
        </Field>)}
        <button className="btn primary" disabled={busy}>{command} 실행</button>
      </form>}
    </Card>}

    <Card title="법인 자본" caption="법인은 사람이 아니라 로그인하지 않지만, 자본은 다른 계정과 똑같이 주식·상품 매수매도, 통장, 대출, 송금을 할 수 있습니다. 자기 회사 주식을 사면 자기주식(배당 없음), 다른 회사 지분을 과반 사면 자회사가 됩니다. Discord에서는 $법인 또는 $login [법인ID]를 씁니다.">
      <div className="scroll-x tall"><table><thead><tr><th>법인</th><th>이름</th><th>거래소</th><th className="r">현금</th><th className="r">보유 종목</th><th className="r">통장</th><th className="r">대출</th><th>모회사</th></tr></thead><tbody>
        {visible.map((item) => <tr key={item.company_code} className={item.company_code === selected ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => void open(item.company_code).catch((cause) => say(cause instanceof Error ? cause.message : "법인을 열지 못했습니다.", true))} onKeyDown={(event) => { if (event.key === "Enter") void open(item.company_code); }}>
          <td className="mono">{item.company_code}</td><td>{item.name}</td><td>{show(item.exchange_id)}</td>
          <td className="r mono">{number(item.cash)}</td><td className="r">{number(item.holdings.length)}</td><td className="r">{number(item.accounts)}</td><td className="r mono">{number(item.loans)}</td>
          <td>{item.parents.length ? item.parents.map((parent) => `${parent.code} ${parent.percent.toFixed(1)}%`).join(", ") : "—"}</td>
        </tr>)}
      </tbody></table>{!visible.length && <p className="empty">거래 중인 법인이 없습니다.</p>}</div>
    </Card>
  </>;
}
