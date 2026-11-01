import { formatQuantity, moneyAdd, moneyCompare, minorMoney, moneyMultiply, moneyDivideFloor, type Money } from "../../shared/money";
import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Donut, ShareholderPie, VotingRightsPie, topSlices, type Shareholders } from "./Donut";
import { Card, Field, number, plain, show, type Ctx, BigValue } from "./common";
import { CompanyLedger } from "./CompanyLedger";

type Holding = { asset_code: string; quantity: number };
type Company = { company_code: string; name: string; exchange_id: string; total_shares: number; listed_shares: number | null; share_price: Money; capital: Money; par_value_minor_units: string | null; quote_currency: string; share_decimal_places: number; currency: string; base_currency?: string; cash: Money; cash_base?: Money; locked: Money; account: string | null; holdings: Holding[]; accounts: number; loans: Money; parents: { code: string; percent: number }[]; largest_shareholder?: { id: string; name: string; shares: Money } | null; max_voting_holder?: { id: string; name: string; percent: string } | null; ultimate_controller?: { id: string; kind: "user" | "company"; name: string; shares: string; percent: string } | null };
type CommandField = { key: string; label: string; kind: string; optional: boolean; hint: string };
type Command = { name: string; group: string; description: string; guide: string; fields: CommandField[] };
type Stock = { name: string; code: string; qty: number; value: Money; price: Money };
type Account = { number: string; product_name: string; balance: Money };
type Assets = { cash: Money; grand_total: Money; loan_total: Money; net_worth: Money; stocks: Stock[]; accounts: Account[] };
type Detail = { account: string; webLogin: boolean; loginId: string; assets: Assets; shareholders: Shareholders };

const UNLISTED = "3DSG";

/**
 * 상장하기 / 비상장 전환하기: list the company on an exchange with the share of it offered
 * (listing again, after a delisting, is the same step), or take it off the market.
 * Keyed by the company's listing so a saved change starts from the new terms.
 */
function ListingControl({ ctx, company, onDone }: { ctx: Ctx; company: Company; onDone: () => void }) {
  const { busy, run, say } = ctx;
  const current = company.exchange_id || UNLISTED;
  const listedNow = current !== UNLISTED;
  const currentPercent = company.listed_shares == null ? 100 : Math.round(company.listed_shares / company.total_shares * 10000) / 100;
  const [mode, setMode] = useState<"list" | "unlist">("list");
  const [exchanges, setExchanges] = useState<string[]>([]);
  const [target, setTarget] = useState(listedNow ? current : "");
  const [percent, setPercent] = useState(String(currentPercent));
  useEffect(() => {
    api<{ exchanges: { exchange_id: string }[] }>("/api/admin/exchanges").then((result) => {
      const ids = result.exchanges.map((item) => item.exchange_id).filter((id) => id !== "CURRENCY" && id !== UNLISTED);
      setExchanges(ids);
      setTarget((value) => value || ids[0] || "");
    }).catch(() => undefined);
  }, []);
  const ratio = Number(percent);
  const listed = Math.floor(company.total_shares * ratio / 100);
  const ratioValid = ratio <= 100 && listed >= 1;
  const changed = !listedNow || target !== current || ratio !== currentPercent;

  function save(exchange: string, listedShares: number | null, question: string) {
    if (!window.confirm(question)) return;
    void run(async () => {
      const detail = await api<{ kind: string }>(`/api/admin/products/${encodeURIComponent(company.company_code)}`);
      return api<{ message: string; success: boolean }>("/api/admin/products", { method: "POST", body: JSON.stringify({ operation: "edit", kind: detail.kind, code: company.company_code, fields: { exchange_id: exchange }, advanced: { listed_shares: listedShares } }) });
    }, "상장 상태를 바꾸지 못했습니다.", (result) => { say(plain(result.message), !result.success); if (result.success) onDone(); });
  }
  const list = () => save(target, ratio === 100 ? null : listed,
    `${company.name} (${company.company_code})를 ${target}에 전체 ${number(company.total_shares)}주 중 ${ratio}% (${number(listed)}주) 상장할까요?\n시장에 풀리는 주식은 상장 주식 수까지입니다(이미 보유 중인 주식 포함).`);
  const unlist = () => save(UNLISTED, null,
    `${company.name} (${company.company_code})를 비상장으로 전환할까요?\n장외시장 3DSG로 옮겨 계속 매매됩니다. 주주의 보유 주식은 그대로 남습니다.`);

  return <div className="listing-control">
    <div className="segment-tabs" role="radiogroup" aria-label="상장 상태">
      {([["list", "상장하기"], ["unlist", "비상장 전환하기"]] as const).map(([key, label]) => <button key={key} type="button" role="radio" aria-checked={mode === key} className={mode === key ? "active" : ""} onClick={() => setMode(key)}>{label}</button>)}
    </div>
    {mode === "list" ? <>
      <Field label="상장 거래소" hint={listedNow ? `지금 ${current} · ${currentPercent}% 상장` : "지금 비상장 · 장외시장 3DSG"}>
        <select value={target} onChange={(event) => setTarget(event.target.value)}>{exchanges.map((id) => <option key={id} value={id}>{id}</option>)}</select>
      </Field>
      <Field label="상장 비율 (%)" hint={ratioValid ? `${number(listed)}주 / ${number(company.total_shares)}주` : "0 초과 100 이하"}><input type="number" min="0.01" max="100" step="0.01" value={percent} onChange={(event) => setPercent(event.target.value)} /></Field>
      <button className="btn primary" type="button" disabled={busy || !target || !ratioValid || !changed} onClick={list}>{listedNow ? "상장 조건 저장" : "상장하기"}</button>
    </> : <>
      <p className="hint">{listedNow ? `${current}에서 상장을 폐지하고 장외시장 3DSG로 옮깁니다. 다시 상장하려면 상장하기를 고르세요.` : "이미 비상장입니다."}</p>
      <button className="soft danger" type="button" disabled={busy || !listedNow} onClick={unlist}>비상장 전환</button>
    </>}
  </div>;
}

type ShareResolution = { resolution_id: number; ratio: number; decision: "approved" | "rejected"; meeting_reference: string; articles_reference: string; par_value_minor_units: string; recorded_by: string; executed: boolean };

function ShareSplitControl({ ctx, company, onDone }: { ctx: Ctx; company: Company; onDone: () => Promise<void> }) {
  const [mode, setMode] = useState(company.par_value_minor_units === null ? "stock_split" : "par_split");
  const [ratio, setRatio] = useState(String(Math.max(2, Math.round(Number(company.share_price) / 100000))));
  const [oldRatio, setOldRatio] = useState("1");
  const [newRatio, setNewRatio] = useState("2");
  const [resolutions, setResolutions] = useState<ShareResolution[]>([]);
  const [resolutionId, setResolutionId] = useState("");
  const [error, setError] = useState("");
  const [proposalRatio, setProposalRatio] = useState("10");
  const [decision, setDecision] = useState("approved");
  const [meeting, setMeeting] = useState("");
  const [articles, setArticles] = useState("");
  const [parValue, setParValue] = useState("");
  const path = `/api/admin/companies/${encodeURIComponent(company.company_code)}/share-actions`;
  const load = useCallback(async () => {
    const result = await api<{ resolutions: ShareResolution[] }>(path);
    setResolutions(result.resolutions);
    setError("");
  }, [path]);
  useEffect(() => { load().catch((cause) => setError(cause instanceof Error ? cause.message : "의결 기록을 불러오지 못했습니다.")); }, [load]);
  const selected = resolutions.find((item) => String(item.resolution_id) === resolutionId);
  const value = Number(mode === "par_split" ? selected?.ratio ?? "" : ratio);
  const ordinary = mode === "ratio_split";
  const oldUnits = ordinary ? Number(oldRatio) : mode === "reverse_split" ? value : 1;
  const newUnits = ordinary ? Number(newRatio) : mode === "reverse_split" ? 1 : value;
  const scale = 10 ** company.share_decimal_places;
  const currency = company.quote_currency || company.currency;
  const ratioValid = [oldUnits, newUnits].every((n) => Number.isSafeInteger(n) && n >= 1) && oldUnits !== newUnits;
  const shareNumerator = ratioValid ? BigInt(company.total_shares) * BigInt(newUnits) : 0n;
  const shares = ratioValid && shareNumerator % BigInt(oldUnits) === 0n ? Number(shareNumerator / BigInt(oldUnits)) : NaN;
  const valid = ratioValid && Number.isSafeInteger(shares) && shares > 0 && moneyCompare(moneyMultiply(company.share_price, scale * oldUnits), newUnits) >= 0 && (mode !== "par_split" || Boolean(selected && !selected.executed && selected.decision === "approved")) && (!ordinary || company.par_value_minor_units === null);
  const price = ratioValid ? moneyDivideFloor(moneyMultiply(company.share_price, oldUnits), newUnits, company.share_decimal_places) : "0";
  const labels: Record<string, string> = { par_split: "정식 액면분할", stock_split: "주식분할", reverse_split: "액면병합", ratio_split: "일반비율분할" };

  return <Card title="정식 액면분할 · 주식분할 · 액면병합 · 일반비율분할" caption="주총은 의결 결과를 남기고, 관리자가 별도로 집행합니다. 의결 등록만으로 주식 수가 바뀌지는 않습니다.">
    <p className="hint">자본금 {number(company.capital)} {currency} · 현재 액면가 {company.par_value_minor_units === null ? "미등록" : `${number(minorMoney(company.par_value_minor_units, Math.log10(scale)))} ${currency}`} · 미체결·예약 주문과 연결된 파생상품은 먼저 정리해주세요.</p>
    {error && <p role="alert">{error}</p>}
    <form className="form-grid" onSubmit={(event) => {
      event.preventDefault();
      if (!valid || !window.confirm(`${company.name} (${company.company_code}) ${labels[mode]} 관리자 집행\n${oldUnits}주 → ${newUnits}주\n주가 ${number(company.share_price)} → ${number(price)} ${currency}\n발행주식 ${number(company.total_shares)} → ${number(shares)}주\n자본금은 유지합니다.${mode === "par_split" ? `\n가결 의결 #${resolutionId}를 집행하고 액면가도 조정합니다.` : ""}`)) return;
      void ctx.run(() => api<Result>(path, { method: "POST", body: JSON.stringify({ action: "execute", mode, ratio: String(value), ...(ordinary ? { oldShares: oldRatio, newShares: newRatio } : {}), ...(mode === "par_split" ? { resolutionId } : {}) }) }),
        "주식 수를 조정하지 못했습니다.", async (result) => { ctx.say(plain(result.message), !result.success); if (result.success) await onDone(); });
    }}>
      <Field label="집행 방식"><select value={mode} onChange={(event) => setMode(event.target.value)}><option value="par_split">정식 액면분할</option><option value="stock_split">주식분할</option><option value="reverse_split">액면병합</option><option value="ratio_split">일반비율분할 (기존주:신주)</option></select></Field>
      {mode === "par_split" ? <Field label="집행할 가결 의결" hint="법인·비율·자본금이 의결 당시 조건과 같아야 합니다."><select required value={resolutionId} onChange={(event) => setResolutionId(event.target.value)}><option value="">가결 의결 선택</option>{resolutions.filter((item) => item.decision === "approved" && !item.executed).map((item) => <option key={item.resolution_id} value={item.resolution_id}>#{item.resolution_id} · 1주 → {item.ratio}주 · {item.meeting_reference}</option>)}</select></Field>
        : ordinary ? <>
          <Field label="기존 주식 수" hint="기존주:신주 비율입니다. 예: 3:2는 기존 3주 → 신주 2주입니다."><input required type="number" min="1" step="1" value={oldRatio} onChange={(event) => setOldRatio(event.target.value)} /></Field>
          <Field label="변경 주식 수"><input required type="number" min="1" step="1" value={newRatio} onChange={(event) => setNewRatio(event.target.value)} /></Field>
          <p className="hint span-wide">n:1 병합과 1:n 분할, 일반 정수 비율을 지원합니다. 모든 발행·상장·보유 수량이 정수 주식으로 변환되어야 하며 단주를 버리지 않습니다. 액면가 등록 법인은 정식 액면분할을 사용해주세요.</p>
        </> : <Field label="비율" hint={mode !== "reverse_split" ? "1주를 몇 주로 나눌지 입력합니다." : "몇 주를 1주로 합칠지 입력합니다. 단주가 생기는 병합은 실행하지 않습니다."}><input required type="number" min="2" step="1" value={ratio} onChange={(event) => setRatio(event.target.value)} /></Field>}
      <p className="hint span-wide" aria-live="polite">{valid ? `예상 주가 ${number(price)} ${currency} · 발행주식 ${number(shares)}주 · 자본금 유지` : mode === "par_split" ? "아래에서 의결 결과를 등록한 뒤 가결 의결을 선택해주세요." : "서로 다른 양의 정수 비율을 입력해주세요. 변경 후 주가는 최소단위 이상, 발행주식은 정수여야 합니다. 액면가 등록 법인은 정식 액면분할을 사용해주세요."}</p>
      <button className="btn primary" disabled={ctx.busy || !valid}>{labels[mode]} 관리자 집행</button>
    </form>
    {mode === "par_split" && <details className="help"><summary>주총 의결 결과 등록</summary>
      <p className="hint">주총에서 이미 의결한 결과를 관리자가 의사록·정관 변경 문서와 함께 등록합니다. 이 화면은 주주 투표나 주식분할 집행을 대신하지 않습니다.</p>
      <form className="form-grid" onSubmit={(event) => {
        event.preventDefault();
        void ctx.run(() => api<Result>(path, { method: "POST", body: JSON.stringify({ action: "record_resolution", ratio: proposalRatio, decision, meetingReference: meeting, articlesReference: articles, parValue }) }),
          "의결 결과를 등록하지 못했습니다.", async (result) => { ctx.say(plain(result.message), !result.success); if (result.success) { setMeeting(""); setArticles(""); await load(); } });
      }}>
        <Field label="의결 결과"><select value={decision} onChange={(event) => setDecision(event.target.value)}><option value="approved">가결</option><option value="rejected">부결</option></select></Field>
        <Field label="의결한 분할 비율" hint="1주 → N주"><input required type="number" min="2" step="1" value={proposalRatio} onChange={(event) => setProposalRatio(event.target.value)} /></Field>
        <Field label={`현재 액면가 (${currency})`} hint="현재 발행주식 수 × 액면가 = 자본금"><input required inputMode="decimal" value={parValue} onChange={(event) => setParValue(event.target.value)} /></Field>
        <Field label="특별결의 의사록 참조"><input required maxLength={1000} value={meeting} onChange={(event) => setMeeting(event.target.value)} placeholder="문서 번호 또는 의사록 링크" /></Field>
        <Field label="정관 변경 문서 참조"><input required maxLength={1000} value={articles} onChange={(event) => setArticles(event.target.value)} placeholder="문서 번호 또는 정관 링크" /></Field>
        <button className="btn primary" disabled={ctx.busy}>의결 결과만 등록</button>
      </form>
    </details>}
    <h3 className="sub-head">주총 의결 · 집행 기록</h3>
    <div className="scroll-x"><table><thead><tr><th>의결</th><th>결과</th><th>비율</th><th>의사록</th><th>집행</th></tr></thead><tbody>{resolutions.map((item) => <tr key={item.resolution_id}><td>#{item.resolution_id}</td><td>{item.decision === "approved" ? "가결" : "부결"}</td><td>1주 → {item.ratio}주</td><td>{item.meeting_reference}</td><td>{item.executed ? "관리자 집행 완료" : item.decision === "approved" ? "집행 대기" : "집행 불가"}</td></tr>)}</tbody></table>{!resolutions.length && !error && <p className="empty">등록된 의결이 없습니다.</p>}</div>
  </Card>;
}

/** What the company owns: its holdings by value. */

/** What the company owns: its holdings by value. */
function HoldingsPie({ stocks }: { stocks: Stock[] }) {
  const items = [...stocks].sort((a, b) => moneyCompare(b.value, a.value)).map((stock) => ({ label: `${stock.code} ${stock.name}`, value: stock.value }));
  const total = items.reduce<Money>((sum, item) => moneyAdd(sum, item.value), "0");
  return <Donut slices={topSlices(items, "기타 종목")} total={total} center={number(total)} unit="평가액" label="보유 자산 구성 원그래프" />;
}

type Member = { code: string; name: string; parent: string; ownership: number; currency: string; goodwill: Money; nci: number; depth?: number };
type Consolidated = { subsidiaries: Member[]; summary: { assets: Money; liabilities: Money; equity: Money; parent_equity: Money; noncontrolling_interest: Money; goodwill: Money }; income: Record<string, Money>; profit_attributable: { parent: Money; noncontrolling: Money }; eliminations: { dividends: Money; transfers: Money; cross_holdings: Money }; notes: string[] } | null;
type Report = { period: string; currency: string; places: number; summary: { assets: Money; liabilities: Money; equity: Money }; income: Record<string, Money>; changes: Record<string, Money>; checks: { balance_sheet: boolean; live_cash: boolean | null }; consolidated: Consolidated };
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
  useEffect(() => { window.scrollTo({ top: 0 }); }, [selected]);

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
    if (!window.confirm(`${company.name} 법인 계정의 웹·나루 비밀번호를 설정할까요?\n로그인 ID는 ${company.company_code}입니다.`)) return;
    void run(() => api<{ account: { message: string } }>("/api/admin/companies/login", { method: "POST", body: JSON.stringify({ companyId: company.company_code, password }) }),
      "법인 로그인을 설정하지 못했습니다.", async (result) => { say(plain(result.account.message)); setPassword(""); await open(company.company_code); });
  }

  function loadReport(next = period) {
    if (!company) return;
    void run(() => api<{ report: Report; periods: string[] }>(`/api/admin/companies/${encodeURIComponent(company.company_code)}/financial${next ? `?period=${encodeURIComponent(next)}` : ""}`),
      "재무제표를 불러오지 못했습니다.", (result) => { setReport(result); setPeriod(result.report.period); });
  }

  const money = (value: Money) => report ? `${number(minorMoney(value, report.report.places))} ${report.report.currency}` : "—";

  return <>
    {company && <>
      <div className="subpage-bar">
        <button className="soft" type="button" onClick={() => { setSelected(""); setDetail(null); }}>← 법인 목록으로 돌아가기</button>
        <p><b>{company.name}</b> <span className="mono">{company.company_code} · {company.exchange_id === UNLISTED ? "3DSG (장외)" : company.exchange_id}</span></p>
      </div>
      <Card title="개요 · 지분" caption={<>계정 <span className="mono">{detail?.account ?? company.account ?? "첫 실행 때 개설"}</span> · 현금·잠금 예치금은 법인 국적 통화로 실시간 환산됩니다 · 거래는 재무제표에 자동 기록됩니다</>}
        actions={<button className="soft" type="button" onClick={() => { setSelected(""); setDetail(null); }}>닫기</button>}>
      <div className="stats">
        <div><small>현금</small><b>{number(company.cash)} {company.currency}</b></div>
        <div><small>잠금 예치금</small><b>{number(company.locked)} {company.currency}</b></div>
        <div><small>총자산</small><b>{detail ? number(detail.assets.grand_total) : "…"}</b></div>
        <div><small>대출</small><b>{number(company.loans)}</b></div>
        <div><small>순자산</small><b>{detail ? number(detail.assets.net_worth) : "…"}</b></div>
      </div>
      <ListingControl key={`${company.company_code}:${company.exchange_id}:${company.listed_shares}`} ctx={ctx} company={company} onDone={() => void load()} />
      {company.parents.length > 0 && <p className="hint">모회사(직접 의결권 과반): {company.parents.map((parent) => `${parent.code} ${Number(parent.percent).toFixed(1)}%`).join(", ")}</p>}
      <div className="pie-pair">
        <section><h3 className="sub-head">주주 구성</h3>{detail?.shareholders ? <ShareholderPie data={detail.shareholders} /> : <p className="empty">불러오는 중…</p>}</section>
        <section><h3 className="sub-head">보유 자산 구성</h3>{detail?.assets.stocks.length ? <HoldingsPie stocks={detail.assets.stocks} /> : <p className="empty">{detail ? "보유 자산이 없습니다." : "불러오는 중…"}</p>}</section>
        <section className="voting-distribution"><h3 className="sub-head">의결권 구성</h3>{detail?.shareholders ? <VotingRightsPie data={detail.shareholders} /> : <p className="empty">불러오는 중…</p>}</section>
        <section className="voting-distribution"><h3 className="sub-head">실질 의결권 구성</h3>{detail?.shareholders ? <VotingRightsPie data={detail.shareholders} effective /> : <p className="empty">불러오는 중…</p>}</section>
      </div>
      </Card>
      <Card title="보유 자산 · 통장" caption="법인 자본 계정이 가진 종목과 통장입니다.">
      <div className="scroll-x"><table><thead><tr><th>종목</th><th>이름</th><th className="r">수량</th><th className="r">평가액</th><th /></tr></thead><tbody>
        {(detail?.assets.stocks ?? []).map((stock) => <tr key={stock.code}><td className="mono">{stock.code}</td><td>{stock.name}</td><td className="r mono">{number(stock.qty)}</td><td className="r mono">{number(stock.value)}</td><td>{stock.code === company.company_code ? <span className="tag">자기주식</span> : companies.some((item) => item.company_code === stock.code && item.parents.some((parent) => parent.code === company.company_code && parent.percent > 50)) ? <span className="tag ok">자회사</span> : null}</td></tr>)}
      </tbody></table>{detail && !detail.assets.stocks.length && <p className="empty">보유 자산이 없습니다.</p>}</div>
      <h3 className="sub-head">통장</h3>
      <div className="scroll-x"><table><thead><tr><th>계좌</th><th>상품</th><th className="r">잔액</th></tr></thead><tbody>
        {(detail?.assets.accounts ?? []).map((account) => <tr key={account.number}><td className="mono">{account.number}</td><td>{account.product_name}</td><td className="r mono">{number(account.balance)}</td></tr>)}
      </tbody></table>{detail && !detail.assets.accounts.length && <p className="empty">통장이 없습니다.</p>}</div>
      </Card>
      <Card title="재무제표 · 연결재무제표" caption="원장에서 계산한 월별 재무제표입니다. 과반 지분 자회사가 있으면 연결재무제표도 보여 줍니다.">
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
            {report.report.consolidated.subsidiaries.map((member) => <tr key={member.code}><td>{"└ ".repeat(member.depth ?? 1)}{member.name} <span className="mono">({member.code})</span></td><td className="mono">{member.parent}</td><td className="r">{Number(member.ownership).toFixed(1)}%</td><td>{member.currency}</td><td className="r mono">{money(member.goodwill)}</td><td className="r mono">{money(member.nci)}</td></tr>)}
          </tbody></table></div>
          <p className="hint">내부거래 제거 · 그룹 내 배당 {money(report.report.consolidated.eliminations.dividends)} · 그룹 내 송금 {money(report.report.consolidated.eliminations.transfers)} · 상호주 {money(report.report.consolidated.eliminations.cross_holdings)}</p>
          <details className="help"><summary>연결 기준</summary><ul>{report.report.consolidated.notes.map((note) => <li key={note}>{note}</li>)}</ul></details>
        </> : <p className="hint">과반 지분을 가진 자회사가 없어 연결재무제표가 없습니다.</p>}
      </>}

      </Card>
      <ShareSplitControl key={`${company.company_code}:${company.share_price}:${company.total_shares}`} ctx={ctx} company={company} onDone={async () => { await load(); await open(company.company_code); }} />
      <CompanyLedger key={company.company_code} code={company.company_code} ctx={ctx} onChanged={() => { if (report) loadReport(); }} />

      <Card title="법인 계정으로 실행" caption="법인 명의로 매수·매도·송금·대출을 실행합니다. Discord의 $법인 또는 $login과 같은 검증을 거칩니다.">
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
      </Card>
      <Card title="웹·나루 로그인" caption="법인 계정으로 나루에 로그인할 비밀번호를 정합니다.">
      <p className="hint">{detail?.webLogin ? <>로그인 ID <span className="mono">{detail.loginId}</span>로 나루에 로그인할 수 있습니다. 비밀번호를 다시 설정하면 기존 로그인은 만료됩니다.</> : "아직 로그인이 없습니다. 비밀번호를 정하면 법인 코드를 ID로 나루에 로그인해 이 계정으로 거래합니다."}</p>
      <form className="form-grid" onSubmit={(event) => { event.preventDefault(); saveLogin(); }}>
        <Field label="비밀번호" hint="12자 이상 · 영문과 숫자 포함"><input type="password" value={password} minLength={12} required onChange={(event) => setPassword(event.target.value)} /></Field>
        <button className="btn primary" disabled={busy || !password}>{detail?.webLogin ? "비밀번호 재설정" : "로그인 만들기"}</button>
      </form>

      </Card>
    </>}

    {!company && <><div className="tile-row">
      <section className="tile"><p className="tile-label">법인</p><p className="tile-value">{number(companies.length)}</p></section>
      <section className="tile"><p className="tile-label">장외 3DSG</p><p className="tile-value">{number(companies.filter((item) => item.exchange_id === UNLISTED).length)}</p></section>
      <section className="tile"><p className="tile-label">법인 현금 합계 · {companies[0]?.base_currency ?? ""}</p><BigValue value={number(companies.reduce<Money>((sum, item) => moneyAdd(sum, item.cash_base ?? item.cash), "0"))} /></section>
    </div>
    <Card title="법인 자본" caption="법인은 사람이 아니라 로그인하지 않지만, 자본은 다른 계정과 똑같이 주식·상품 매수매도, 통장, 대출, 송금을 할 수 있습니다. 자기 회사 주식을 사면 자기주식(배당 없음), 다른 회사 지분을 과반 사면 자회사가 됩니다. Discord에서는 $법인 또는 $login [법인ID]를 씁니다.">
      <div className="scroll-x tall"><table><thead><tr><th>법인</th><th>이름</th><th>거래소</th><th className="r">현금</th><th className="r">보유 종목</th><th className="r">통장</th><th className="r">대출</th><th>모회사</th><th>최대주주</th><th>최대 의결권 보유자</th><th>실질 지배자</th></tr></thead><tbody>
        {visible.map((item) => <tr key={item.company_code} className={item.company_code === selected ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => void open(item.company_code).catch((cause) => say(cause instanceof Error ? cause.message : "법인을 열지 못했습니다.", true))} onKeyDown={(event) => { if (event.key === "Enter") void open(item.company_code); }}>
          <td className="mono">{item.company_code}</td><td>{item.name}</td><td>{item.exchange_id === UNLISTED ? <span className="tag">3DSG 장외</span> : show(item.exchange_id)}</td>
          <td className="r mono">{number(item.cash)} {item.currency}</td><td className="r">{number(item.holdings.length)}</td><td className="r">{number(item.accounts)}</td><td className="r mono">{number(item.loans)}</td>
          <td>{item.parents.length ? item.parents.map((parent) => `${parent.code} 의결 ${Number(parent.percent).toFixed(1)}%`).join(", ") : "—"}</td>
          <td>{item.largest_shareholder ? <>{item.largest_shareholder.name}<br /><span className="mono">{formatQuantity(item.largest_shareholder.shares)}주</span></> : "—"}</td>
          <td>{item.max_voting_holder ? <>{item.max_voting_holder.name}<br /><span className="mono">{Number(item.max_voting_holder.percent).toFixed(1)}% 의결권</span></> : "—"}</td>
          <td>{item.ultimate_controller ? <>{item.ultimate_controller.name}<br /><span className="mono">{formatQuantity(item.ultimate_controller.shares)}주 · {Number(item.ultimate_controller.percent).toFixed(1)}% 지배력</span></> : "—"}</td>
        </tr>)}
      </tbody></table>{!visible.length && <p className="empty">거래 중인 법인이 없습니다.</p>}</div>
    </Card></>}
  </>;
}
