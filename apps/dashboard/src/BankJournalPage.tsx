import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Card, Field, number, type Ctx } from "./common";

type Trial = { currency_code: string; account_code: string; debit_units: number; credit_units: number; balance_units: number; debit_amount: string; credit_amount: string; balance_amount: string; decimal_places: number; entry_count?: number };
type Unbalanced = { entry_id: number; issuer: string; event_type: string; reference_key: string; currency_code: string; line_count: number; debit_units: number; credit_units: number; debit_amount: string; credit_amount: string };
type Reconciliation = { reference_key: string; currency_code: string; ledger_amount: number; journal_amount: number | null; difference: number | null; ledger_amount_text: string; journal_amount_text: string | null; difference_text: string | null; status: string };
type DepositReconciliation = { currency_code: string; account_balance: string; journal_balance: string; difference: string; status: string };
type LoanReconciliation = DepositReconciliation;
type LoanScheduleFinding = { schedule_id: number; loan_id: number; currency_code: string; amount_paid_minor_units: number; allocated_minor_units: number; due_minor_units: number; status: string };
type ChargeoffMemoReconciliation = { currency_code: string; subledger_units: number; event_units: number; difference_units: number; decimal_places: number; status: string };
type DepositInsuranceClaim = { user_id: string; account_number: string; currency_code: string; covered_amount_text: string; uninsured_amount_text: string; payout_currency: string | null; payout_amount_text?: string; resolved_epoch: number; decimal_places: number; payout_decimal_places?: number };
type DepositInsuranceReconciliation = { currency_code: string; account_code: string; claim_amount: string; journal_amount: string; difference: string; status: string };
type MaturityGap = { issuer: string; asOfEpoch: number; method: string; ladders: { currency: string; decimalPlaces: number; buckets: { bucket: string; inflows: number; outflows: number; net_gap: number; cumulative_gap: number }[]; depositRunoffStress: { runoffPercent: number; assumedRunoff: number; stressedOneDayOutflow: number; oneDayLoanInflows: number; oneDayGap: number }[] }[] };
type LiquidityStress = { issuer: string; asOfEpoch: number; horizonDays: number; currency: string; decimalPlaces: number; availableLiquidityProxy: number; eligibleInflows: number; depositorConcentration: { largestDepositorExposure: number; uninsuredDepositExposure: number; uninsuredDepositSharePercent: number; largestDepositorSharePercent: number; topFiveDepositorSharePercent: number }; concentrationScenarios: { group: "largest_depositor" | "top_five_depositors"; depositOutflow: number; eligibleInflows: number; netOutflow: number; availableLiquidityProxy: number; coveragePercent: number | null }[]; scenarios: { runoffPercent: number; depositOutflow: number; eligibleInflows: number; netOutflow: number; availableLiquidityProxy: number; coveragePercent: number | null }[]; method: string };
type RateStress = { issuer: string; asOfEpoch: number; horizonDays: number; scenarios: { currency: string; decimalPlaces: number; assetBalanceYears: number; liabilityBalanceYears: number; shocks: { basisPoints: number; assetInterestChange: number; liabilityInterestChange: number; netInterestIncomeChange: number }[] }[]; method: string };
type FixedRateEveStress = { issuer: string; asOfEpoch: number; scenarios: { currency: string; decimalPlaces: number; fixedLoans: number; fixedDeposits: number; loansWithoutMaturity: number; assetDurationAmountYears: number; liabilityDurationAmountYears: number; netDurationGapAmountYears: number; netConvexityGapAmountYears2: number; shocks: { basisPoints: number; estimatedEveChange: number }[] }[]; method: string };
type FxStress = { issuer: string; baseCurrency: string; decimalPlaces: number; asOfEpoch: number; positions: { currency: string; baseCurrency: string; assetsInBase: number; liabilitiesInBase: number; netOpenPositionInBase: number; fxRate: number; shocks: { currencyChangePercent: number; capitalImpact: number }[] }[]; method: string };
type CreditStress = { issuer: string; currency: string; decimalPlaces: number; performingExposureCount: number; performingEad: number; estimatedFullDefaultLoss: number; existingAllowance: number; scenarios: { defaultSharePercent: number; defaultedEad: number; grossLoss: number; existingAllowanceApplied: number; incrementalProvision: number; equityAfterProvision: number; capitalConsumedPercent: number | null }[]; method: string };
type CorrelatedCreditStress = { issuer: string; currency: string; decimalPlaces: number; confidence: number; assetCorrelation: number; exposureCount: number; performingEad: number; independentExpectedLoss: number; correlatedTailLoss: number; connectedPartyCorrelation: number; connectedPartyBorrowerCount: number; connectedPartyExposureCount: number; connectedPartyEad: number; connectedPartyJointTailLoss: number; method: string };
type BorrowerConcentrationStress = { issuer: string; currency: string; decimalPlaces: number; borrowerCount: number; totalLoanExposure: number; largestBorrowerSharePercent: number; topFiveExposureSharePercent: number; borrowers: { borrowerId: string; loanCount: number; exposure: number; exposureSharePercent: number; grossLoss: number; existingAllowanceApplied: number; incrementalProvision: number; equityAfterProvision: number }[]; method: string };
type Prudential = { issuer: string; currency: string; decimalPlaces: number; status: string; effectiveEquity: number; riskWeightedAssets: number; capitalRatioPercent: number | null; targetCapitalRatioPercent: number; leverageExposure: number; leverageRatioPercent: number | null; minimumLeverageRatioPercent: number; exposureCount: number; cashCollateralCovered: number; weightedComponents: Record<string, number>; method: string };
type CommonJournalReconciliation = { ledger_scope: string; native_entries?: number; native_balances?: number; wallet_entries?: number; common_entries: number; missing_entries: number; balance_gaps?: number; status: string; execution_gaps?: unknown[]; common_journal_gaps?: unknown[]; wallet_link_gaps?: unknown[]; primary_settlement_gaps?: unknown[]; holding_movement_gaps?: unknown[]; gaps?: unknown[] };
type AmountSnapshotAudit = { status: string; findingCount: number; findings: { source_table?: string; source_key?: string; amount_field?: string; currency_code?: string; status: string }[] };
type FinancialCloseControl = { status: string; coverage: string[]; institution_count: number; findingCount: number; findings: Record<string, unknown>[]; amount_field_coverage: { status: string; registered_count: number; exempt_count: number; unclassified: { source_table?: string; amount_field?: string; status: string }[] } };
type Report = { issuer: string; currency: string | null; trialBalance: Trial[]; commonTrialBalance: Trial[]; unbalancedEntries: Unbalanced[]; resultReconciliation: Reconciliation[]; depositReconciliation: DepositReconciliation[]; loanReconciliation: LoanReconciliation[]; loanScheduleReconciliation: LoanScheduleFinding[]; eclAllowanceReconciliation: LoanReconciliation[]; chargeoffMemoReconciliation: ChargeoffMemoReconciliation[]; depositInsuranceReconciliation: DepositInsuranceReconciliation[]; depositInsuranceClaims: DepositInsuranceClaim[]; commonJournalReconciliation: CommonJournalReconciliation[]; financialCloseControl: FinancialCloseControl; commonJournalUnbalancedEntries: { entry_id: number; ledger_scope: string; entity_type: string; entity_id: string; currency_code: string }[]; amountSnapshotAudit: AmountSnapshotAudit; maturityGap: MaturityGap; liquidityStress: LiquidityStress; rateStress: RateStress; fixedRateEveStress: FixedRateEveStress; fxStress: FxStress; prudential: Prudential; creditStress: CreditStress; correlatedCreditStress: CorrelatedCreditStress; borrowerConcentrationStress: BorrowerConcentrationStress };

/** One balance per currency: the beam tips toward the heavier side of the trial balance. */
function BalanceScales({ rows, unbalanced }: { rows: Trial[]; unbalanced: number }) {
  const currencies = [...new Set(rows.map((row) => row.currency_code))].map((code) => {
    const lines = rows.filter((row) => row.currency_code === code);
    const scale = 10 ** (lines[0]?.decimal_places ?? 0);
    const debit = lines.reduce((sum, row) => sum + row.debit_units, 0);
    const credit = lines.reduce((sum, row) => sum + row.credit_units, 0);
    return { code, debit: debit / scale, credit: credit / scale, tilt: debit === credit ? 0 : Math.max(-12, Math.min(12, (debit - credit) / Math.max(debit, credit) * 12)) };
  });
  if (!currencies.length) return null;
  return <div className="tile-row scale-tiles">
    {currencies.map((item) => <section key={item.code} className="tile scale-tile" aria-label={`${item.code} 대차 ${item.tilt ? "불일치" : "일치"}`}>
      <p className="tile-label">대차 저울 · {item.code}</p>
      <svg viewBox="0 0 240 120" aria-hidden="true">
        <path className="scale-post" d="M120 30 L120 104 M96 108 L144 108" />
        <g transform={`rotate(${-item.tilt} 120 30)`}>
          <path className="scale-beam" d="M30 30 L210 30" />
          <path className="scale-pan" d="M30 30 L14 74 L46 74 Z M210 30 L194 74 L226 74 Z" />
        </g>
        <circle className="scale-pivot" cx="120" cy="30" r="5" />
      </svg>
      <dl className="scale-sides"><div><dt>차변</dt><dd>{number(item.debit)}</dd></div><div><dt>대변</dt><dd>{number(item.credit)}</dd></div></dl>
      <p className={item.tilt ? "scale-verdict off" : "scale-verdict"}>{item.tilt ? `차이 ${number(item.debit - item.credit)}` : "대차 일치"}{unbalanced > 0 && ` · 불균형 분개 ${unbalanced}건`}</p>
    </section>)}
  </div>;
}

/** `bank` fixes the bank ID (a bank's own page) and loads its books right away. */
export function BankJournalPage({ ctx, bank }: { ctx: Ctx; bank?: string }) {
  const { busy, run, say } = ctx;
  const [issuer, setIssuer] = useState(bank ?? "");
  const [currency, setCurrency] = useState("");
  const [since, setSince] = useState(() => {
    const value = new Date(Date.now() - 30 * 86400_000);
    value.setMinutes(value.getMinutes() - value.getTimezoneOffset());
    return value.toISOString().slice(0, 16);
  });
  const [report, setReport] = useState<Report | null>(null);
  const [closing, setClosing] = useState({ scope: "institution", entity: "", currency: "", start: "", end: "" });
  useEffect(() => { if (bank) fetchReport(); }, [bank]);
  function load(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    fetchReport();
  }
  function fetchReport() {
    if (!issuer.trim()) { say("은행 ID를 입력해주세요.", true); return; }
    const query = new URLSearchParams({ issuer: issuer.trim(), ...(currency.trim() ? { currency: currency.trim().toUpperCase() } : {}),
      ...(since ? { since: String(Math.floor(new Date(since).getTime() / 1000)) } : {}) });
    void run(() => api<Report>(`/api/admin/bank-journal?${query}`), "은행 원장 대사 정보를 불러오지 못했습니다.", setReport);
  }
  function closePeriod(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const start = Math.floor(new Date(closing.start).getTime() / 1000);
    const end = Math.floor(new Date(closing.end).getTime() / 1000);
    if (!closing.entity.trim() || !closing.currency.trim() || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      say("대상, 통화와 올바른 시작·종료 시각을 입력해주세요.", true); return;
    }
    if (!window.confirm(`${closing.scope}/${closing.entity} · ${closing.currency.toUpperCase()} 기간을 마감할까요? 마감 후 이 기간에 소급 전기할 수 없으며 수정은 다음 기간 조정전기로 해야 합니다.`)) return;
    void run(() => api<{ success: boolean; closingId: number }>("/api/admin/bank-journal/close", {
      method: "POST", body: JSON.stringify({ ledgerScope: closing.scope, entityId: closing.entity.trim(), currencyCode: closing.currency.trim().toUpperCase(), periodStartEpoch: start, periodEndEpoch: end }),
    }), "회계기간을 마감하지 못했습니다.", (result) => say(`회계기간 마감 #${result.closingId}을 기록했습니다.`, !result.success));
  }
  return <Card title="은행 이중분개 원장 대사" caption="조회 전용 · 시산 잔액과 불균형은 전체 이력, 손익 원장 대사는 지정 기간입니다. 금액은 각 통화 정밀도를 따르며 법정 감사의견이 아닙니다.">
    <form className="form-grid" onSubmit={load}>
      <Field label="은행 ID"><input className="mono" required maxLength={100} readOnly={Boolean(bank)} value={issuer} onChange={(event) => setIssuer(event.target.value.toUpperCase())} /></Field>
      <Field label="통화 필터" hint="선택"><input className="mono" maxLength={12} value={currency} onChange={(event) => setCurrency(event.target.value)} /></Field>
      <Field label="손익 원장 대사 시작" hint="선택 · 기본 최근 30일, 비우면 전체"><input type="datetime-local" value={since} onChange={(event) => setSince(event.target.value)} /></Field>
      <button className="btn primary" disabled={busy || !issuer.trim()}>원장 조회</button>
    </form>
    <h3 className="sub-head">공통원장 기간 마감</h3>
    <form className="form-grid" onSubmit={closePeriod}>
      <Field label="원장 범위"><select value={closing.scope} onChange={(event) => setClosing({ ...closing, scope: event.target.value })}><option value="customer">고객</option><option value="company">회사</option><option value="institution">금융기관</option></select></Field>
      <Field label="대상 ID" hint={closing.scope === "institution" ? "은행 ID" : undefined}><input required maxLength={100} value={closing.entity} onChange={(event) => setClosing({ ...closing, entity: event.target.value })} /></Field>
      <Field label="통화"><input required className="mono" maxLength={12} value={closing.currency} onChange={(event) => setClosing({ ...closing, currency: event.target.value })} /></Field>
      <Field label="기간 시작 (포함)"><input required type="datetime-local" value={closing.start} onChange={(event) => setClosing({ ...closing, start: event.target.value })} /></Field>
      <Field label="기간 종료 (미포함)"><input required type="datetime-local" value={closing.end} onChange={(event) => setClosing({ ...closing, end: event.target.value })} /></Field>
      <button className="btn primary" disabled={busy}>기간 마감</button>
    </form>
    <p className="hint">불변 스냅샷으로 기록됩니다. 같은 기간 재요청은 멱등이며, 마감 이후 소급 전기는 거부됩니다. 오류 정정은 다음 열린 기간의 조정분개로 기록하세요.</p>
    {report && <>
      <BalanceScales rows={report.trialBalance} unbalanced={report.unbalancedEntries.length} />
      <h3 className="sub-head">통합 금융원장 검사 요약</h3>
      <p className="hint">등록된 금액 필드·스키마 후보·공통원장·각 금융기관의 구현된 보조원장 검사를 함께 실행합니다. 상태 {report.financialCloseControl.status} · 발견 {report.financialCloseControl.findingCount.toLocaleString()}건 · 금액 필드 {report.financialCloseControl.amount_field_coverage.registered_count}개 · 명시 제외 {report.financialCloseControl.amount_field_coverage.exempt_count}개 · 미분류 스키마 {report.financialCloseControl.amount_field_coverage.unclassified.length}개 · 기관 {report.financialCloseControl.institution_count}곳 · 범위 {report.financialCloseControl.coverage.join(", ")}. 미연결 특수 매처와 과거 거래 재생 제한은 금액 인벤토리에 남습니다.</p>
      {report.financialCloseControl.findings.length > 0 && <div className="scroll-x"><pre className="mono">{JSON.stringify(report.financialCloseControl.findings, null, 2)}</pre></div>}
      <h3 className="sub-head">공통 원장 연결 상태</h3>
      <p className="hint">범위는 현재 연결된 고객 지갑, 회사, 금융기관 보조원장입니다. 대출·시장·상품 등 아직 공통 전기로 이관되지 않은 하위원장은 미연결로 남으므로 여기서 matched로 간주하지 않습니다.</p>
      <div className="scroll-x"><table><thead><tr><th>원장 범위</th><th className="r">원장 행/잔액</th><th className="r">공통 전기</th><th className="r">누락</th><th className="r">잔액 차이</th><th>상태</th></tr></thead><tbody>
        {report.commonJournalReconciliation.map((row) => <tr key={row.ledger_scope}><td>{row.ledger_scope}</td><td className="r">{row.native_entries ?? row.wallet_entries ?? row.native_balances ?? 0}</td><td className="r">{row.common_entries}</td><td className="r">{row.missing_entries}</td><td className="r">{row.balance_gaps ?? "—"}</td><td>{row.status}</td></tr>)}
      </tbody></table></div>
      {report.commonJournalReconciliation.filter((row) => row.ledger_scope === "market_executions").map((row) => {
        const findings = [
          ["체결량", row.execution_gaps], ["공통분개", row.common_journal_gaps],
          ["지갑 결제", row.wallet_link_gaps], ["1차 발행 수취처", row.primary_settlement_gaps],
          ["증권 보유 이동", row.holding_movement_gaps],
        ].filter(([, items]) => Array.isArray(items) && items.length > 0) as [string, unknown[]][];
        return <section key="market-execution-findings"><h4>현물 체결 상세 대사</h4>
          {findings.length ? findings.map(([label, items]) => <div className="scroll-x" key={label}><h5>{label} · {items.length}건</h5><pre className="mono">{JSON.stringify(items, null, 2)}</pre></div>) : <p className="empty">신규 표준 현물 체결의 체결량·분개·지갑·1차 발행 수취처·보유 이동 불일치가 없습니다.</p>}
        </section>;
      })}
      {report.commonJournalReconciliation.filter((row) => row.ledger_scope === "company_bond_liabilities").map((row) => <section key="company-bond-findings"><h4>회사채 발행잔액 · 회사 원장 대사</h4>
        {row.gaps?.length ? <div className="scroll-x"><pre className="mono">{JSON.stringify(row.gaps, null, 2)}</pre></div> : <p className="empty">회사 재무원장에 등록된 회사채 발행잔액이 현재 보유 잔액과 일치합니다.</p>}
        <p className="hint">기말 잔액 비교입니다. 과거 발행·쿠폰·상환의 개별 전기나 현금 지급 시점을 증명하지 않습니다.</p>
      </section>)}
      <p className="hint">공통 원장 불균형 분개: {report.commonJournalUnbalancedEntries.length}건</p>
      {report.commonJournalUnbalancedEntries.length > 0 && <div className="scroll-x"><table><thead><tr><th>분개</th><th>원장 범위</th><th>주체 유형</th><th>주체 ID</th><th>통화</th></tr></thead><tbody>
        {report.commonJournalUnbalancedEntries.map((row) => <tr key={row.entry_id}><td>#{row.entry_id}</td><td>{row.ledger_scope}</td><td>{row.entity_type}</td><td className="mono">{row.entity_id}</td><td>{row.currency_code}</td></tr>)}
      </tbody></table></div>}
      <h3 className="sub-head">공통 기관원장 시산표</h3>
      <p className="hint">선택한 금융기관의 공통 복식부기 원장을 통화별 최소단위 정수 잔액과 표시 금액으로 조회합니다. 기존 기관 전용 원장 시산표와는 별도입니다.</p>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th>계정</th><th className="r">차변</th><th className="r">대변</th><th className="r">잔액</th><th className="r">분개 수</th></tr></thead><tbody>
        {report.commonTrialBalance.map((row, i) => <tr key={`${row.currency_code}-${row.account_code}-${i}`}><td>{row.currency_code}</td><td className="mono">{row.account_code}</td><td className="r mono">{groupAmount(row.debit_amount)} {row.currency_code}</td><td className="r mono">{groupAmount(row.credit_amount)} {row.currency_code}</td><td className="r mono">{groupAmount(row.balance_amount)} {row.currency_code}</td><td className="r">{row.entry_count}</td></tr>)}
      </tbody></table>{!report.commonTrialBalance.length && <p className="empty">공통 기관원장에 조회할 분개가 없습니다.</p>}</div>
      <h3 className="sub-head">금액 최소단위 스냅샷 검사</h3>
      <p className="hint">등록된 원천 금액과 통화 최소단위 정수 스냅샷의 차이·누락 검사입니다. 아직 스냅샷이 권위 원장을 대체하지는 않습니다. 결과 {report.amountSnapshotAudit.findingCount}건 · {report.amountSnapshotAudit.status}</p>
      {report.amountSnapshotAudit.findings.length > 0 && <div className="scroll-x"><table><thead><tr><th>테이블</th><th>키</th><th>필드</th><th>통화</th><th>판정</th></tr></thead><tbody>{report.amountSnapshotAudit.findings.map((item, index) => <tr key={`${item.source_table}-${item.source_key}-${item.amount_field}-${index}`}><td>{item.source_table ?? "—"}</td><td className="mono">{item.source_key ?? "—"}</td><td>{item.amount_field ?? "—"}</td><td>{item.currency_code ?? "—"}</td><td>{item.status}</td></tr>)}</tbody></table></div>}
      <h3 className="sub-head">신용위험가중자산 · {report.prudential.issuer} · {report.prudential.currency}</h3>
      <p className="hint">기관 상태: {report.prudential.status} · 유효 자기자본 {money(report.prudential.effectiveEquity, report.prudential.decimalPlaces)} · RWA {money(report.prudential.riskWeightedAssets, report.prudential.decimalPlaces)} · RWA 기준 단순 자본비율 {report.prudential.capitalRatioPercent == null ? "—" : `${report.prudential.capitalRatioPercent.toFixed(2)}%`} / 목표 {report.prudential.targetCapitalRatioPercent.toFixed(2)}% · 단순 레버리지비율 {report.prudential.leverageRatioPercent == null ? "—" : `${report.prudential.leverageRatioPercent.toFixed(2)}%`} / 최소 {report.prudential.minimumLeverageRatioPercent.toFixed(2)}% · 대출 {report.prudential.exposureCount}건 · 현금담보 {money(report.prudential.cashCollateralCovered, report.prudential.decimalPlaces)}</p>
      <div className="scroll-x"><table><thead><tr><th>위험구간 기여</th><th className="r">가중 노출액 ({report.prudential.currency})</th></tr></thead><tbody>{Object.entries(report.prudential.weightedComponents).map(([key, value]) => <tr key={key}><td>{riskWeightLabel(key)}</td><td className="r mono">{money(value, report.prudential.decimalPlaces)}</td></tr>)}</tbody></table></div>
      <p className="hint">가정: 현금담보 0%, 자산담보 20% 할인 후 35%, 정상 무담보 75%, 고위험 무담보 100%, 부도 무담보 150%. 시뮬레이터 분류이며 Basel 승인 RWA가 아닙니다. {report.prudential.method}</p>
      <h3 className="sub-head">대량부도 손실 스트레스 · {report.creditStress.currency}</h3>
      <p className="hint">현재 정상 대출잔액 중 5/10/20/40%가 동시에 부도난다고 가정한 손실 민감도입니다. 담보 반영 LGD와 기존 ECL 충당금 일부만 반영하며, 부도확률 예측·거시 피드백·회수시점 반영은 아닙니다.</p>
      <div className="scroll-x"><table><thead><tr><th>가정 부도비중</th><th className="r">부도 EAD</th><th className="r">총 손실</th><th className="r">충당금 사용</th><th className="r">추가 충당금</th><th className="r">자기자본 소진</th><th className="r">충당 후 자기자본</th></tr></thead><tbody>
        {report.creditStress.scenarios.map((scenario) => <tr key={scenario.defaultSharePercent}><td>{scenario.defaultSharePercent}%</td><td className="r mono">{money(scenario.defaultedEad, report.creditStress.decimalPlaces)}</td><td className="r mono">{money(scenario.grossLoss, report.creditStress.decimalPlaces)}</td><td className="r mono">{money(scenario.existingAllowanceApplied, report.creditStress.decimalPlaces)}</td><td className="r mono">{money(scenario.incrementalProvision, report.creditStress.decimalPlaces)}</td><td className="r mono">{scenario.capitalConsumedPercent == null ? "—" : `${scenario.capitalConsumedPercent.toFixed(2)}%`}</td><td className="r mono">{money(scenario.equityAfterProvision, report.creditStress.decimalPlaces)}</td></tr>)}
      </tbody></table>{!report.creditStress.performingExposureCount && <p className="empty">스트레스 대상 정상 대출이 없습니다.</p>}</div>
      <h3 className="sub-head">공통 거시충격 부도 상관 스트레스</h3>
      <p className="hint">단일요인 Gaussian proxy의 {(report.correlatedCreditStress.confidence * 100).toFixed(0)}% 불리한 거시 분위수에서 동일 포트폴리오 PD가 함께 상승한다고 가정합니다. 자산상관 {report.correlatedCreditStress.assetCorrelation.toFixed(2)} · 기대손실 {money(report.correlatedCreditStress.independentExpectedLoss, report.correlatedCreditStress.decimalPlaces)} → 조건부 꼬리손실 {money(report.correlatedCreditStress.correlatedTailLoss, report.correlatedCreditStress.decimalPlaces)} {report.correlatedCreditStress.currency}. 연결차주 {report.correlatedCreditStress.connectedPartyBorrowerCount}명·노출 {money(report.correlatedCreditStress.connectedPartyEad, report.correlatedCreditStress.decimalPlaces)} · 공동충격 민감도(상관 {report.correlatedCreditStress.connectedPartyCorrelation.toFixed(2)}) 손실 {money(report.correlatedCreditStress.connectedPartyJointTailLoss, report.correlatedCreditStress.decimalPlaces)}. 후자는 여러 연결집단이 동시에 불리하다고 둔 조건부 민감도이며 확률적 VaR나 규제자본 산식이 아닙니다.</p>
      <h3 className="sub-head">차주 집중도 · 단일 차주 부도 스트레스</h3>
      <p className="hint">정상·부도 대출을 차주별로 합산합니다. 관리자가 연결차주로 등록한 집단은 같은 집중노출 그룹으로 합산하며, 보증인은 노출집단이 아니라 담보조정 LGD에 반영합니다.</p>
      <p className="hint">차주 {report.borrowerConcentrationStress.borrowerCount}명 · 대출노출 {money(report.borrowerConcentrationStress.totalLoanExposure, report.borrowerConcentrationStress.decimalPlaces)} {report.borrowerConcentrationStress.currency} · 최대 차주 {report.borrowerConcentrationStress.largestBorrowerSharePercent.toFixed(1)}% · 상위 5명 {report.borrowerConcentrationStress.topFiveExposureSharePercent.toFixed(1)}%</p>
      <div className="scroll-x"><table><thead><tr><th>차주 ID</th><th className="r">대출 수</th><th className="r">노출액</th><th className="r">포트폴리오 비중</th><th className="r">단독 부도 총손실</th><th className="r">충당금 사용</th><th className="r">추가 충당금</th><th className="r">충당 후 자기자본</th></tr></thead><tbody>
        {report.borrowerConcentrationStress.borrowers.map((row) => <tr key={row.borrowerId}><td className="mono">{row.borrowerId}</td><td className="r">{row.loanCount}</td><td className="r mono">{money(row.exposure, report.borrowerConcentrationStress.decimalPlaces)}</td><td className="r">{row.exposureSharePercent.toFixed(2)}%</td><td className="r mono">{money(row.grossLoss, report.borrowerConcentrationStress.decimalPlaces)}</td><td className="r mono">{money(row.existingAllowanceApplied, report.borrowerConcentrationStress.decimalPlaces)}</td><td className="r mono">{money(row.incrementalProvision, report.borrowerConcentrationStress.decimalPlaces)}</td><td className="r mono">{money(row.equityAfterProvision, report.borrowerConcentrationStress.decimalPlaces)}</td></tr>)}
      </tbody></table>{!report.borrowerConcentrationStress.borrowers.length && <p className="empty">집중도 스트레스 대상 대출이 없습니다.</p>}</div>
      <h3 className="sub-head">순외화포지션 스트레스 · {report.fxStress.baseCurrency}</h3>
      <p className="hint">대출자산−예금부채의 미헤지 순포지션에 외화 ±10/20% 변동을 가정합니다. 채권·파생·기타 자산부채·헤지는 빠져 있어 전체 은행 FX 위험이나 규제 한도가 아닙니다.</p>
      <div className="scroll-x"><table><thead><tr><th>외화</th><th className="r">기준환율</th><th className="r">순포지션 ({report.fxStress.baseCurrency})</th><th className="r">−20%</th><th className="r">−10%</th><th className="r">+10%</th><th className="r">+20%</th></tr></thead><tbody>
        {report.fxStress.positions.map((position) => <tr key={position.currency}><td>{position.currency}</td><td className="r mono">{position.fxRate.toLocaleString("ko-KR", { maximumFractionDigits: 6 })}</td><td className="r mono">{money(position.netOpenPositionInBase, report.fxStress.decimalPlaces)}</td>{position.shocks.map((shock) => <td key={shock.currencyChangePercent} className="r mono">{money(shock.capitalImpact, report.fxStress.decimalPlaces)}</td>)}</tr>)}
      </tbody></table>{!report.fxStress.positions.length && <p className="empty">은행 기준통화 외의 대출·예금 잔액이 없습니다.</p>}</div>
      <h3 className="sub-head">계약상 만기갭 · 통화별</h3>
      <p className="hint">계약상 만기만 단순 합산한 ALM 관측치입니다. 1d는 1일 이내, 7d·30d·90d·365d는 직전 구간 초과부터 해당 일수까지이며 누적 갭은 해당 구간까지의 합계입니다. 각 통화는 따로 표시하며 예금 행동만기, 조기인출, 장부외 현금흐름, LCR·NSFR 규제 계산은 반영하지 않습니다.</p>
      {report.maturityGap.ladders.map((ladder) => <div className="scroll-x" key={ladder.currency}><h4>{ladder.currency}</h4><table><thead><tr><th>잔존만기</th><th className="r">대출 유입</th><th className="r">예금 유출</th><th className="r">구간 갭</th><th className="r">누적 갭</th></tr></thead><tbody>
        {ladder.buckets.map((row) => <tr key={row.bucket}><td>{row.bucket}</td><td className="r mono">{money(row.inflows, ladder.decimalPlaces)}</td><td className="r mono">{money(row.outflows, ladder.decimalPlaces)}</td><td className="r mono">{money(row.net_gap, ladder.decimalPlaces)}</td><td className="r mono">{money(row.cumulative_gap, ladder.decimalPlaces)}</td></tr>)}
      </tbody></table><h5>예금 인출 스트레스 · 가정값</h5><p className="hint">총 예금의 해당 비율이 1일 내 인출된다고 가정하고 계약상 1일 내 유출과 큰 값을 사용합니다. 현금·준비금·매각가능자산을 빼지 않으므로 실제 유동성 부족 판정이 아닙니다.</p><table><thead><tr><th>총 예금 인출</th><th className="r">가정 인출액</th><th className="r">스트레스 1일 유출</th><th className="r">계약상 대출 유입</th><th className="r">1일 순갭</th></tr></thead><tbody>{ladder.depositRunoffStress.map((row) => <tr key={row.runoffPercent}><td>{row.runoffPercent}%</td><td className="r mono">{money(row.assumedRunoff, ladder.decimalPlaces)}</td><td className="r mono">{money(row.stressedOneDayOutflow, ladder.decimalPlaces)}</td><td className="r mono">{money(row.oneDayLoanInflows, ladder.decimalPlaces)}</td><td className="r mono">{money(row.oneDayGap, ladder.decimalPlaces)}</td></tr>)}</tbody></table></div>)}
      {!report.maturityGap.ladders.length && <p className="empty">만기갭을 계산할 예금·대출 잔액이 없습니다.</p>}
      <h3 className="sub-head">30일 유동성 커버리지 민감도 · {report.liquidityStress.currency}</h3>
      <p className="hint">예금+자기자본−대출을 현금·준비금 대용치로 보고, 30일 내 정상 상환예정 대출 유입의 50%를 차감합니다. 유가증권 HQLA·담보설정·규제 헤어컷·행동만기와 장부외 흐름은 빠져 있으므로 규제 LCR이나 실제 생존기간이 아닙니다.</p>
      <p className="hint">예금자 집중도 · 최대 예금자 {money(report.liquidityStress.depositorConcentration.largestDepositorExposure, report.liquidityStress.decimalPlaces)} ({report.liquidityStress.depositorConcentration.largestDepositorSharePercent.toFixed(1)}%) · 상위 5명 {report.liquidityStress.depositorConcentration.topFiveDepositorSharePercent.toFixed(1)}% · 미보장 잔액 {money(report.liquidityStress.depositorConcentration.uninsuredDepositExposure, report.liquidityStress.decimalPlaces)} ({report.liquidityStress.depositorConcentration.uninsuredDepositSharePercent.toFixed(1)}%)</p>
      <div className="scroll-x"><table><thead><tr><th>집중 인출 가정</th><th className="r">인출액</th><th className="r">인정 대출 유입</th><th className="r">순 유출</th><th className="r">유동성 대용치</th><th className="r">커버리지</th></tr></thead><tbody>{report.liquidityStress.concentrationScenarios.map((scenario) => <tr key={scenario.group}><td>{scenario.group === "largest_depositor" ? "최대 예금자 1명 전액 인출" : "상위 5명 전액 인출"}</td><td className="r mono">{money(scenario.depositOutflow, report.liquidityStress.decimalPlaces)}</td><td className="r mono">{money(scenario.eligibleInflows, report.liquidityStress.decimalPlaces)}</td><td className="r mono">{money(scenario.netOutflow, report.liquidityStress.decimalPlaces)}</td><td className="r mono">{money(scenario.availableLiquidityProxy, report.liquidityStress.decimalPlaces)}</td><td className="r mono">{scenario.coveragePercent == null ? "—" : `${scenario.coveragePercent.toFixed(1)}%`}</td></tr>)}</tbody></table></div>
      <div className="scroll-x"><table><thead><tr><th>예금 이탈 가정</th><th className="r">예금 유출</th><th className="r">인정 대출 유입</th><th className="r">순 유출</th><th className="r">유동성 대용치</th><th className="r">커버리지</th></tr></thead><tbody>{report.liquidityStress.scenarios.map((scenario) => <tr key={scenario.runoffPercent}><td>{scenario.runoffPercent}%</td><td className="r mono">{money(scenario.depositOutflow, report.liquidityStress.decimalPlaces)}</td><td className="r mono">{money(scenario.eligibleInflows, report.liquidityStress.decimalPlaces)}</td><td className="r mono">{money(scenario.netOutflow, report.liquidityStress.decimalPlaces)}</td><td className="r mono">{money(scenario.availableLiquidityProxy, report.liquidityStress.decimalPlaces)}</td><td className="r mono">{scenario.coveragePercent == null ? "—" : `${scenario.coveragePercent.toFixed(1)}%`}</td></tr>)}</tbody></table></div>
      <h3 className="sub-head">변동금리 평행충격 민감도 · 향후 1년</h3>
      <p className="hint">현재 변동금리 대출·예금 잔액이 다음 재산정 후 1년 동안 유지되고 지표금리 충격이 전액 전가된다고 가정하며, 계약별 금리하한·상한을 반영합니다. 고정금리·헤지·예금행동·원금상환·기준지표 간 차이와 법정최고금리 재산정은 미반영하므로 IRRBB 규제값이나 수익 예측이 아닙니다.</p>
      {report.rateStress.scenarios.map((scenario) => <div className="scroll-x" key={scenario.currency}><h4>{scenario.currency}</h4><table><thead><tr><th>평행 충격</th><th className="r">대출 이자수익 변화</th><th className="r">예금 이자비용 변화</th><th className="r">순이자수익 변화</th></tr></thead><tbody>
        {scenario.shocks.map((shock) => <tr key={shock.basisPoints}><td>{shock.basisPoints > 0 ? "+" : ""}{shock.basisPoints} bp</td><td className="r mono">{money(shock.assetInterestChange, scenario.decimalPlaces)}</td><td className="r mono">{money(shock.liabilityInterestChange, scenario.decimalPlaces)}</td><td className="r mono">{money(shock.netInterestIncomeChange, scenario.decimalPlaces)}</td></tr>)}
      </tbody></table></div>)}
      {!report.rateStress.scenarios.length && <p className="empty">향후 1년 내 재산정되는 변동금리 잔액이 없습니다.</p>}
      <h3 className="sub-head">고정금리 잔액 듀레이션 갭 · 평행충격 참고 민감도</h3>
      <p className="hint">고정금리 대출의 잔여 원금상환 시점과 예금 만기의 1차·제곱 금액가중 모멘트로 평행충격의 2차 효과까지 근사합니다. 실제 할인 현금흐름 컨벡서티가 아니며 쿠폰·수익률곡선·증권·헤지·행동성 예금 듀레이션을 제외하므로 경제가치 민감도 참고치일 뿐 IRRBB 규제값이 아닙니다.</p>
      {report.fixedRateEveStress.scenarios.map((scenario) => <div className="scroll-x" key={scenario.currency}><h4>{scenario.currency} · 고정 대출 {scenario.fixedLoans}건 / 정기예금 {scenario.fixedDeposits}건</h4><table><thead><tr><th>평행 충격</th><th className="r">순 듀레이션 갭(통화·년)</th><th className="r">추정 EVE 변화</th></tr></thead><tbody>
        {scenario.shocks.map((shock) => <tr key={shock.basisPoints}><td>{shock.basisPoints > 0 ? "+" : ""}{shock.basisPoints} bp</td><td className="r mono">{scenario.netDurationGapAmountYears.toFixed(2)}</td><td className="r mono">{money(shock.estimatedEveChange, scenario.decimalPlaces)} {scenario.currency}</td></tr>)}
      </tbody></table>{scenario.loansWithoutMaturity > 0 && <p className="hint">만기 정보가 없어 듀레이션 계산에서 제외된 고정금리 대출 {scenario.loansWithoutMaturity}건</p>}</div>)}
      {!report.fixedRateEveStress.scenarios.length && <p className="empty">고정금리 대출·예금 잔액이 없습니다.</p>}
      <h3 className="sub-head">시산 잔액 · {report.issuer}</h3>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th>계정</th><th className="r">차변</th><th className="r">대변</th><th className="r">잔액</th></tr></thead><tbody>
        {report.trialBalance.map((row, i) => <tr key={`${row.currency_code}-${row.account_code}-${i}`}><td>{row.currency_code}</td><td className="mono">{row.account_code}</td><td className="r mono">{groupAmount(row.debit_amount)} {row.currency_code}</td><td className="r mono">{groupAmount(row.credit_amount)} {row.currency_code}</td><td className="r mono">{groupAmount(row.balance_amount)} {row.currency_code}</td></tr>)}
      </tbody></table>{!report.trialBalance.length && <p className="empty">전기된 분개가 없습니다.</p>}</div>
      <h3 className="sub-head">불균형 분개</h3>
      <div className="scroll-x"><table><thead><tr><th>분개</th><th>유형</th><th>참조</th><th>통화</th><th className="r">차변</th><th className="r">대변</th></tr></thead><tbody>
        {report.unbalancedEntries.map((row) => <tr key={row.entry_id}><td>#{row.entry_id}</td><td>{row.event_type}</td><td className="mono">{row.reference_key}</td><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.debit_amount)}</td><td className="r mono">{groupAmount(row.credit_amount)}</td></tr>)}
      </tbody></table>{!report.unbalancedEntries.length && <p className="empty">불균형 분개가 없습니다.</p>}</div>
      <h3 className="sub-head">기존 결과 원장 대사</h3>
      <div className="scroll-x"><table><thead><tr><th>참조</th><th>통화</th><th className="r">기존 원장</th><th className="r">분개</th><th className="r">차이</th><th>상태</th></tr></thead><tbody>
        {report.resultReconciliation.map((row, i) => <tr key={`${row.reference_key}-${row.currency_code}-${i}`}><td className="mono">{row.reference_key}</td><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.ledger_amount_text)}</td><td className="r mono">{row.journal_amount_text ? groupAmount(row.journal_amount_text) : "—"}</td><td className="r mono">{row.difference_text ? groupAmount(row.difference_text) : "—"}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.resultReconciliation.length && <p className="empty">대사할 결과 원장이 없습니다.</p>}</div>
      <h3 className="sub-head">예금 부채 잔액 대사</h3>
      <p className="hint">계좌별 현재 예금 잔액을 분개장 customer_deposits_payable 잔액과 통화 최소단위로 비교합니다. 분개 연결 전 과거 잔액은 차이 또는 누락으로 표시하며 자동 수정하지 않습니다.</p>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th className="r">계좌 잔액</th><th className="r">분개장 부채</th><th className="r">차이</th><th>상태</th></tr></thead><tbody>
        {report.depositReconciliation.map((row) => <tr key={row.currency_code}><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.account_balance)}</td><td className="r mono">{groupAmount(row.journal_balance)}</td><td className="r mono">{groupAmount(row.difference)}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.depositReconciliation.length && <p className="empty">대사할 예금 잔액이 없습니다.</p>}</div>
      <h3 className="sub-head">대출채권 잔액 대사</h3>
      <p className="hint">정상·부도 대출계약의 현재 gross 잔액을 loans_receivable 분개장 계정과 통화 최소단위로 비교합니다. 손실충당금은 별도 contra-asset 계정이므로 이 대사는 충당금 차감 전 총액 기준입니다.</p>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th className="r">대출계약 잔액</th><th className="r">분개장 채권</th><th className="r">차이</th><th>상태</th></tr></thead><tbody>
        {report.loanReconciliation.map((row) => <tr key={row.currency_code}><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.account_balance)}</td><td className="r mono">{groupAmount(row.journal_balance)}</td><td className="r mono">{groupAmount(row.difference)}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.loanReconciliation.length && <p className="empty">대사할 대출채권 잔액이 없습니다.</p>}</div>
      <h3 className="sub-head">대출 상환표 배분 불변식 검사</h3>
      <p className="hint">회차별 지급액이 수수료·이자·원금 배분 합계와 일치하는지, 배분이 예정액을 넘지 않는지 통화 최소단위로 검사합니다. 현금/고객 지갑/대출채권 간 결제 대사는 별도 항목입니다.</p>
      <div className="scroll-x"><table><thead><tr><th>대출</th><th>회차</th><th>통화</th><th className="r">지급액 (최소단위)</th><th className="r">배분합계</th><th className="r">예정액</th><th>상태</th></tr></thead><tbody>
        {report.loanScheduleReconciliation.map((row) => <tr key={row.schedule_id}><td>{row.loan_id}</td><td>{row.schedule_id}</td><td>{row.currency_code}</td><td className="r mono">{row.amount_paid_minor_units}</td><td className="r mono">{row.allocated_minor_units}</td><td className="r mono">{row.due_minor_units}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.loanScheduleReconciliation.length && <p className="empty">상환표 배분 불일치가 없습니다.</p>}</div>
      <h3 className="sub-head">기대신용손실 충당금 대사</h3>
      <p className="hint">대출 계약에 저장된 ECL 충당금과 분개장 ecl_allowance contra-asset 잔액을 상품통화 최소단위로 비교합니다. 차이는 읽기 전용으로 표시하며 자동 수정하지 않습니다.</p>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th className="r">대출별 충당금</th><th className="r">분개장 충당금</th><th className="r">차이</th><th>상태</th></tr></thead><tbody>
        {report.eclAllowanceReconciliation.map((row) => <tr key={row.currency_code}><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.account_balance)}</td><td className="r mono">{groupAmount(row.journal_balance)}</td><td className="r mono">{groupAmount(row.difference)}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.eclAllowanceReconciliation.length && <p className="empty">대사할 ECL 충당금 잔액이 없습니다.</p>}</div>
      <h3 className="sub-head">상각 후 미회수 채권 메모잔액 대사</h3>
      <p className="hint">상각으로 재무상태표에서 제거된 계약상 채무잔액과 불변 상각·회수 이벤트 순액을 비교합니다. 이는 회수 가능성이나 법적 집행 여부를 보증하지 않습니다.</p>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th className="r">대출 메모잔액 (최소단위)</th><th className="r">이벤트 순액 (최소단위)</th><th className="r">차이 (최소단위)</th><th>상태</th></tr></thead><tbody>
        {report.chargeoffMemoReconciliation.map((row) => <tr key={row.currency_code}><td>{row.currency_code}</td><td className="r mono">{row.subledger_units}</td><td className="r mono">{row.event_units}</td><td className="r mono">{row.difference_units}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.chargeoffMemoReconciliation.length && <p className="empty">대사할 상각 메모잔액이 없습니다.</p>}</div>
      <h3 className="sub-head">예금보험 청구액 · 분개장 부채 대사</h3>
      <p className="hint">정리 이력의 확정 보장·미보장 청구액과 분개장 부채를 통화 최소단위로 비교합니다. 미보장 채권은 법정 배당순위나 회수액이 아니라 시뮬레이터상 청구액입니다.</p>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th>계정</th><th className="r">청구액</th><th className="r">분개장 부채</th><th className="r">차이</th><th>상태</th></tr></thead><tbody>
        {report.depositInsuranceReconciliation.map((row) => <tr key={`${row.currency_code}-${row.account_code}`}><td>{row.currency_code}</td><td className="mono">{row.account_code}</td><td className="r mono">{groupAmount(row.claim_amount)}</td><td className="r mono">{groupAmount(row.journal_amount)}</td><td className="r mono">{groupAmount(row.difference)}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.depositInsuranceReconciliation.length && <p className="empty">대사할 예금보험 청구가 없습니다.</p>}</div>
      <h3 className="sub-head">예금보험 정리 청구 · 최근 100건</h3>
      <p className="hint">실패기관 정리 때 확정된 보장·미보장 금액과 원계좌 통화입니다. 보장 지급은 수익자 자국통화로 환산될 수 있으며, 실제 예금보험기금의 법정 지급이나 회수 절차를 뜻하지 않습니다.</p>
      <div className="scroll-x"><table><thead><tr><th>정리 시각</th><th>사용자</th><th>계좌</th><th>청구 통화</th><th className="r">보장액</th><th className="r">미보장 청구</th><th>지급 통화</th><th className="r">지급액</th></tr></thead><tbody>
        {report.depositInsuranceClaims.map((row) => <tr key={row.account_number}><td>{new Date(row.resolved_epoch * 1000).toLocaleString("ko-KR")}</td><td className="mono">{row.user_id}</td><td className="mono">{row.account_number}</td><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.covered_amount_text)}</td><td className="r mono">{groupAmount(row.uninsured_amount_text)}</td><td>{row.payout_currency || "—"}</td><td className="r mono">{row.payout_amount_text ? groupAmount(row.payout_amount_text) : "—"}</td></tr>)}
      </tbody></table>{!report.depositInsuranceClaims.length && <p className="empty">정리된 예금보험 청구가 없습니다.</p>}</div>
    </>}
  </Card>;
}

function groupAmount(value: string) {
  const [whole, fraction] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}

function money(value: number, decimalPlaces: number) {
  return new Intl.NumberFormat("ko-KR", {
    minimumFractionDigits: decimalPlaces,
    maximumFractionDigits: decimalPlaces,
  }).format(value);
}

function riskWeightLabel(key: string) {
  return ({ secured_asset: "자산담보 환산(35%)", performing_unsecured: "정상 무담보(75%)", higher_risk_unsecured: "고위험 무담보(100%)", defaulted_unsecured: "부도 무담보(150%)" } as Record<string, string>)[key] ?? key;
}
