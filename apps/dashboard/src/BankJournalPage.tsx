import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Card, Field, type Ctx } from "./common";

type Trial = { currency_code: string; account_code: string; debit_units: number; credit_units: number; balance_units: number; debit_amount: string; credit_amount: string; balance_amount: string; decimal_places: number };
type Unbalanced = { entry_id: number; issuer: string; event_type: string; reference_key: string; currency_code: string; line_count: number; debit_units: number; credit_units: number; debit_amount: string; credit_amount: string };
type Reconciliation = { reference_key: string; currency_code: string; ledger_amount: number; journal_amount: number | null; difference: number | null; ledger_amount_text: string; journal_amount_text: string | null; difference_text: string | null; status: string };
type DepositReconciliation = { currency_code: string; account_balance: string; journal_balance: string; difference: string; status: string };
type LoanReconciliation = DepositReconciliation;
type DepositInsuranceClaim = { user_id: string; account_number: string; currency_code: string; covered_amount_text: string; uninsured_amount_text: string; resolved_epoch: number; decimal_places: number };
type MaturityGap = { issuer: string; asOfEpoch: number; method: string; ladders: { currency: string; decimalPlaces: number; buckets: { bucket: string; inflows: number; outflows: number; net_gap: number; cumulative_gap: number }[]; depositRunoffStress: { runoffPercent: number; assumedRunoff: number; stressedOneDayOutflow: number; oneDayLoanInflows: number; oneDayGap: number }[] }[] };
type LiquidityStress = { issuer: string; asOfEpoch: number; horizonDays: number; currency: string; decimalPlaces: number; availableLiquidityProxy: number; eligibleInflows: number; depositorConcentration: { largestDepositorExposure: number; uninsuredDepositExposure: number; uninsuredDepositSharePercent: number; largestDepositorSharePercent: number; topFiveDepositorSharePercent: number }; concentrationScenarios: { group: "largest_depositor" | "top_five_depositors"; depositOutflow: number; eligibleInflows: number; netOutflow: number; availableLiquidityProxy: number; coveragePercent: number | null }[]; scenarios: { runoffPercent: number; depositOutflow: number; eligibleInflows: number; netOutflow: number; availableLiquidityProxy: number; coveragePercent: number | null }[]; method: string };
type RateStress = { issuer: string; asOfEpoch: number; horizonDays: number; scenarios: { currency: string; decimalPlaces: number; assetBalanceYears: number; liabilityBalanceYears: number; shocks: { basisPoints: number; assetInterestChange: number; liabilityInterestChange: number; netInterestIncomeChange: number }[] }[]; method: string };
type FxStress = { issuer: string; baseCurrency: string; decimalPlaces: number; asOfEpoch: number; positions: { currency: string; baseCurrency: string; assetsInBase: number; liabilitiesInBase: number; netOpenPositionInBase: number; fxRate: number; shocks: { currencyChangePercent: number; capitalImpact: number }[] }[]; method: string };
type CreditStress = { issuer: string; currency: string; decimalPlaces: number; performingExposureCount: number; performingEad: number; estimatedFullDefaultLoss: number; existingAllowance: number; scenarios: { defaultSharePercent: number; defaultedEad: number; grossLoss: number; existingAllowanceApplied: number; incrementalProvision: number; equityAfterProvision: number; capitalConsumedPercent: number | null }[]; method: string };
type Prudential = { issuer: string; currency: string; decimalPlaces: number; status: string; effectiveEquity: number; riskWeightedAssets: number; capitalRatioPercent: number | null; targetCapitalRatioPercent: number; leverageExposure: number; leverageRatioPercent: number | null; minimumLeverageRatioPercent: number; exposureCount: number; cashCollateralCovered: number; weightedComponents: Record<string, number>; method: string };
type Report = { issuer: string; currency: string | null; trialBalance: Trial[]; unbalancedEntries: Unbalanced[]; resultReconciliation: Reconciliation[]; depositReconciliation: DepositReconciliation[]; loanReconciliation: LoanReconciliation[]; eclAllowanceReconciliation: LoanReconciliation[]; depositInsuranceClaims: DepositInsuranceClaim[]; maturityGap: MaturityGap; liquidityStress: LiquidityStress; rateStress: RateStress; fxStress: FxStress; prudential: Prudential; creditStress: CreditStress };

export function BankJournalPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [issuer, setIssuer] = useState("");
  const [currency, setCurrency] = useState("");
  const [since, setSince] = useState(() => {
    const value = new Date(Date.now() - 30 * 86400_000);
    value.setMinutes(value.getMinutes() - value.getTimezoneOffset());
    return value.toISOString().slice(0, 16);
  });
  const [report, setReport] = useState<Report | null>(null);
  function load(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!issuer.trim()) { say("금융기관 issuer를 입력해주세요.", true); return; }
    const query = new URLSearchParams({ issuer: issuer.trim(), ...(currency.trim() ? { currency: currency.trim().toUpperCase() } : {}),
      ...(since ? { since: String(Math.floor(new Date(since).getTime() / 1000)) } : {}) });
    void run(() => api<Report>(`/api/admin/bank-journal?${query}`), "은행 원장 대사 정보를 불러오지 못했습니다.", setReport);
  }
  return <Card title="은행 이중분개 원장 대사" caption="조회 전용 · 시산 잔액과 불균형은 전체 이력, 손익 원장 대사는 지정 기간입니다. 금액은 각 통화 정밀도를 따르며 법정 감사의견이 아닙니다.">
    <form className="form-grid" onSubmit={load}>
      <Field label="금융기관 issuer"><input className="mono" required maxLength={100} value={issuer} onChange={(event) => setIssuer(event.target.value)} /></Field>
      <Field label="통화 필터" hint="선택"><input className="mono" maxLength={12} value={currency} onChange={(event) => setCurrency(event.target.value)} /></Field>
      <Field label="손익 원장 대사 시작" hint="선택 · 기본 최근 30일, 비우면 전체"><input type="datetime-local" value={since} onChange={(event) => setSince(event.target.value)} /></Field>
      <button className="btn primary" disabled={busy || !issuer.trim()}>원장 조회</button>
    </form>
    {report && <>
      <h3 className="sub-head">신용위험가중자산 · {report.prudential.issuer} · {report.prudential.currency}</h3>
      <p className="hint">기관 상태: {report.prudential.status} · 유효 자기자본 {money(report.prudential.effectiveEquity, report.prudential.decimalPlaces)} · RWA {money(report.prudential.riskWeightedAssets, report.prudential.decimalPlaces)} · RWA 기준 단순 자본비율 {report.prudential.capitalRatioPercent == null ? "—" : `${report.prudential.capitalRatioPercent.toFixed(2)}%`} / 목표 {report.prudential.targetCapitalRatioPercent.toFixed(2)}% · 단순 레버리지비율 {report.prudential.leverageRatioPercent == null ? "—" : `${report.prudential.leverageRatioPercent.toFixed(2)}%`} / 최소 {report.prudential.minimumLeverageRatioPercent.toFixed(2)}% · 대출 {report.prudential.exposureCount}건 · 현금담보 {money(report.prudential.cashCollateralCovered, report.prudential.decimalPlaces)}</p>
      <div className="scroll-x"><table><thead><tr><th>위험구간 기여</th><th className="r">가중 노출액 ({report.prudential.currency})</th></tr></thead><tbody>{Object.entries(report.prudential.weightedComponents).map(([key, value]) => <tr key={key}><td>{riskWeightLabel(key)}</td><td className="r mono">{money(value, report.prudential.decimalPlaces)}</td></tr>)}</tbody></table></div>
      <p className="hint">가정: 현금담보 0%, 자산담보 20% 할인 후 35%, 정상 무담보 75%, 고위험 무담보 100%, 부도 무담보 150%. 시뮬레이터 분류이며 Basel 승인 RWA가 아닙니다. {report.prudential.method}</p>
      <h3 className="sub-head">대량부도 손실 스트레스 · {report.creditStress.currency}</h3>
      <p className="hint">현재 정상 대출잔액 중 5/10/20/40%가 동시에 부도난다고 가정한 손실 민감도입니다. 담보 반영 LGD와 기존 ECL 충당금 일부만 반영하며, 부도확률 예측·거시 피드백·회수시점 반영은 아닙니다.</p>
      <div className="scroll-x"><table><thead><tr><th>가정 부도비중</th><th className="r">부도 EAD</th><th className="r">총 손실</th><th className="r">충당금 사용</th><th className="r">추가 충당금</th><th className="r">자기자본 소진</th><th className="r">충당 후 자기자본</th></tr></thead><tbody>
        {report.creditStress.scenarios.map((scenario) => <tr key={scenario.defaultSharePercent}><td>{scenario.defaultSharePercent}%</td><td className="r mono">{money(scenario.defaultedEad, report.creditStress.decimalPlaces)}</td><td className="r mono">{money(scenario.grossLoss, report.creditStress.decimalPlaces)}</td><td className="r mono">{money(scenario.existingAllowanceApplied, report.creditStress.decimalPlaces)}</td><td className="r mono">{money(scenario.incrementalProvision, report.creditStress.decimalPlaces)}</td><td className="r mono">{scenario.capitalConsumedPercent == null ? "—" : `${scenario.capitalConsumedPercent.toFixed(2)}%`}</td><td className="r mono">{money(scenario.equityAfterProvision, report.creditStress.decimalPlaces)}</td></tr>)}
      </tbody></table>{!report.creditStress.performingExposureCount && <p className="empty">스트레스 대상 정상 대출이 없습니다.</p>}</div>
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
      <h3 className="sub-head">기대신용손실 충당금 대사</h3>
      <p className="hint">대출 계약에 저장된 ECL 충당금과 분개장 ecl_allowance contra-asset 잔액을 상품통화 최소단위로 비교합니다. 차이는 읽기 전용으로 표시하며 자동 수정하지 않습니다.</p>
      <div className="scroll-x"><table><thead><tr><th>통화</th><th className="r">대출별 충당금</th><th className="r">분개장 충당금</th><th className="r">차이</th><th>상태</th></tr></thead><tbody>
        {report.eclAllowanceReconciliation.map((row) => <tr key={row.currency_code}><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.account_balance)}</td><td className="r mono">{groupAmount(row.journal_balance)}</td><td className="r mono">{groupAmount(row.difference)}</td><td>{row.status}</td></tr>)}
      </tbody></table>{!report.eclAllowanceReconciliation.length && <p className="empty">대사할 ECL 충당금 잔액이 없습니다.</p>}</div>
      <h3 className="sub-head">예금보험 정리 청구 · 최근 100건</h3>
      <p className="hint">실패기관 정리 때 확정된 보장·미보장 금액과 원계좌 통화입니다. 보장 지급은 수익자 자국통화로 환산될 수 있으며, 실제 예금보험기금의 법정 지급이나 회수 절차를 뜻하지 않습니다.</p>
      <div className="scroll-x"><table><thead><tr><th>정리 시각</th><th>사용자</th><th>계좌</th><th>통화</th><th className="r">보장액</th><th className="r">미보장 청구</th></tr></thead><tbody>
        {report.depositInsuranceClaims.map((row) => <tr key={row.account_number}><td>{new Date(row.resolved_epoch * 1000).toLocaleString("ko-KR")}</td><td className="mono">{row.user_id}</td><td className="mono">{row.account_number}</td><td>{row.currency_code}</td><td className="r mono">{groupAmount(row.covered_amount_text)}</td><td className="r mono">{groupAmount(row.uninsured_amount_text)}</td></tr>)}
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
