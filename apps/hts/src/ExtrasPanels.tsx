import { groupAmount, RATE_BENCHMARK_LABELS, date, money, type DrCustodyInstruction, type DrMovement, type DrVotingAgenda, type LoanScheduleView, type ReceivedDrVotingDelegation } from "./appShared";
import { moneyAdd } from "../../shared/money";
import type { Portfolio } from "./types";

type Props = {
  portfolio: Portfolio | null;
  loanSchedules: LoanScheduleView[];
  drMovements: DrMovement[];
  drCustodyInstructions: DrCustodyInstruction[];
  receivedDrDelegations: ReceivedDrVotingDelegation[];
  drVotingAgenda: DrVotingAgenda[];
  busy: boolean;
  onCancelConversion: (item: DrCustodyInstruction) => void;
  onVote: (item: DrVotingAgenda, choice: string) => void;
};

/** Lower panels: deposit-insurance history, loan schedules, DR movements, pending conversions and voting. */
export function ExtrasPanels({ portfolio, loanSchedules, drMovements, drCustodyInstructions, receivedDrDelegations, drVotingAgenda, busy, onCancelConversion, onVote }: Props) {
  return <div className="extras">
  {(portfolio?.deposit_insurance_claims?.length ?? 0) > 0 && <section className="data-panel holdings-panel">
    <div className="panel-title"><h2>예금보험 정리 이력</h2><span>{portfolio?.deposit_insurance_claims?.length}건</span></div>
    <div className="holdings">{portfolio?.deposit_insurance_claims?.map((claim) => <div className="holding" key={claim.claim_id}>
      <span><b>{claim.issuer} · {date(claim.resolved_epoch)}</b><small>원예금 통화 기준 보험금 {money(claim.covered_amount, claim.decimal_places)} · 미보장채권 {money(claim.uninsured_amount, claim.decimal_places)} {claim.currency_code}</small>{claim.payout_currency && claim.payout_amount != null && <small>실제 지급 기록 {money(claim.payout_amount, claim.payout_decimal_places ?? 0)} {claim.payout_currency}</small>}</span>
    </div>)}</div>
    <p className="orders-hint">보험금 지급은 고객 국적 통화로 환전될 수 있습니다. 이 내역은 예금 원통화 기준의 시뮬레이터 정리 결과이며 실제 예금보험 청구가 아닙니다.</p>
  </section>}
  {loanSchedules.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>대출 상환 일정</h2><span>{loanSchedules.length}건</span></div>
    <div className="holdings">{loanSchedules.map((loan) => <div className="holding loan-schedule" key={loan.loan_id}>
      <span><b>#{loan.loan_id} · {loan.product_code} · 잔액 {money(loan.outstanding_balance, loan.decimal_places)} {loan.quote_currency}</b>
        {loan.underwriting_stress_dsr != null && <small>심사 당시 DSR {(loan.underwriting_dsr == null ? undefined : Number(loan.underwriting_dsr).toFixed(1)) ?? "—"}% · 금리 +{loan.underwriting_stress_shock_bps}bp / 외화부채 환율 +{loan.underwriting_fx_stress_shock_percent}% 스트레스 DSR {Number(loan.underwriting_stress_dsr).toFixed(1)}%</small>}
        {loan.origination_pd_12m != null && <small>취급 시 추정 12개월 부도확률(PD) {(loan.origination_pd_12m * 100).toFixed(2)}% · 현재 ECL 평가의 Stage 2 비교 기준</small>}
        {loan.current_ecl_stage != null && loan.current_ecl_pd_12m != null && <small>최근 ECL 평가 · Stage {loan.current_ecl_stage} · 12개월 PD {(loan.current_ecl_pd_12m * 100).toFixed(2)}%</small>}
        {loan.stage_cure_since_epoch != null && <small>위험 개선 관찰 시작 {date(loan.stage_cure_since_epoch)} · 90일 경과 후 Stage 2 조건이 없으면 Stage 1 재평가</small>}
        {loan.rate_type === "floating" && loan.next_repricing_epoch != null && <small>현재 연 {(loan.contract_rate == null ? undefined : Number(loan.contract_rate).toFixed(4)) ?? "—"}% · 지표 관측 기준일 {date(Math.max(0, loan.next_repricing_epoch - loan.repricing_lag_seconds))} · 다음 재산정 {date(loan.next_repricing_epoch)} · {RATE_BENCHMARK_LABELS[loan.benchmark_key || ""] || loan.benchmark_key || "기준금리"} {loan.benchmark_tenor_days}일 · 새 금리는 재산정 시 확정됩니다.</small>}
        {loan.installments.slice(0, 4).map((item) => <small key={item.schedule_id}>회차 {item.installment_number} · {date(item.due_epoch)} · 잔액 원금 {money(moneyAdd(item.principal_due, item.principal_paid, true), loan.decimal_places)} / 이자 {money(moneyAdd(item.interest_due, item.interest_paid, true), loan.decimal_places)} / 수수료 {money(moneyAdd(item.fee_due, item.fee_paid, true), loan.decimal_places)} {loan.quote_currency}</small>)}
        {!loan.installments.length && <small>예정된 미납 회차가 없습니다.</small>}
        {loan.rate_resets.map((reset, index) => <small key={`${reset.applied_epoch}-${index}`}>금리 재산정 {date(reset.applied_epoch)} · {RATE_BENCHMARK_LABELS[reset.benchmark_key] || reset.benchmark_key} {reset.benchmark_tenor_days}일 · 연 {reset.old_rate}% → {reset.new_rate}%</small>)}
      </span>
    </div>)}</div>
    <p className="orders-hint">회차별 계약상 배분 내역입니다. 회차 잔액을 넘는 초과상환 및 경과·연체이자 별도 원장은 표시하지 않습니다.</p>
  </section>}
  {drMovements.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>DR 전환 이력</h2><span>최근 {drMovements.length}건</span></div>
    <div className="holdings">{drMovements.map((item) => <div className="holding" key={item.movement_id}>
      <span><b>{item.movement_type === "issue" ? "DR 발행" : "원주 상환"} · {item.dr_code}</b><small>{date(item.recorded_epoch)} · {money(item.dr_quantity, 0)} DR ↔ {money(item.underlying_quantity, 0)}주 {item.underlying_code} · 원주 평균원가 {groupAmount(item.underlying_average_cost_text)} {item.underlying_currency}</small></span>
    </div>)}</div>
    <p className="orders-hint">게임 내 예탁 이동 기록이며 실제 수탁기관 보관 증빙은 아닙니다.</p>
  </section>}
  {drCustodyInstructions.some((item) => item.status === "requested" || item.status === "verified") && <section className="data-panel holdings-panel"><div className="panel-title"><h2>대기 중인 DR 전환</h2><span>{drCustodyInstructions.filter((item) => item.status === "requested" || item.status === "verified").length}건</span></div>
    <div className="holdings">{drCustodyInstructions.filter((item) => item.status === "requested" || item.status === "verified").map((item) => <div className="holding" key={item.reference_key}>
      <span><b>{item.movement_type === "issue" ? "DR 발행" : "원주 상환"} · {item.dr_code}</b><small>{money(item.dr_quantity, 0)} DR ↔ {money(item.underlying_quantity, 0)}주 {item.underlying_code} · 접수 {date(item.requested_epoch)} · 정산 가능 {item.settle_after_epoch ? date(item.settle_after_epoch) : "즉시"} · 수량 예약 중</small></span>
      {item.status === "requested" && <button className="cancel-button" disabled={busy} onClick={() => void onCancelConversion(item)}>요청 취소</button>}
    </div>)}</div>
    <p className="orders-hint">대기 중인 입력 수량은 매도·담보·다른 DR 전환에 사용할 수 없습니다. 정산은 내부 시뮬레이터 관리 흐름에서 처리됩니다.</p>
  </section>}
  {receivedDrDelegations.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>위임받은 DR 의결권</h2><span>{receivedDrDelegations.length}건</span></div>
    <div className="holdings">{receivedDrDelegations.map((item) => <div className="holding" key={item.delegation_id}>
      <span><b>{item.dr_code} · {item.dr_quantity} DR</b><small>{item.owner_user_id}에게서 위임 · {item.underlying_code} 의결권 · {date(item.created_epoch)}</small></span>
    </div>)}</div>
    <p className="orders-hint">위임받은 권리는 기준일 스냅샷에서 대리인의 표수에 합산됩니다.</p>
  </section>}
  {drVotingAgenda.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>주주총회 의결권</h2><span>{drVotingAgenda.length}개 안건</span></div>
    <div className="holdings">{drVotingAgenda.map((item) => <div className="holding" key={`${item.meetingId}-${item.agendaItemId}`}>
      <span><b>{item.underlyingCode} · {item.meetingTitle} — {item.agendaTitle}</b><small>기준일 의결권 {money(item.votingPower)}표 · {item.state === "open" ? `투표 마감 ${item.closesEpoch ? date(item.closesEpoch) : "—"}` : item.state === "upcoming" ? `투표 시작 ${item.opensEpoch ? date(item.opensEpoch) : "—"}` : item.state === "closed" ? "투표 종료" : "투표 일정 미설정"}{item.myChoice ? ` · 내 선택 ${item.myChoice}` : ""}</small></span>
      {item.state === "open" && !item.myChoice && <div className="actions">{item.choices.map((choice) => <button key={choice} className="cancel-button" disabled={busy} onClick={() => void onVote(item, choice)}>{choice}</button>)}</div>}
    </div>)}</div>
    <p className="orders-hint">기준일 현재 직접·DR 및 위임 권리를 합산합니다. 투표는 기간 중 안건마다 한 번 가능하고 변경·철회할 수 없습니다.</p>
  </section>}
</div>;
}
