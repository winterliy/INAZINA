import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, date, errorText, type Ctx } from "./common";

type Amount = string | number;
type Limit = { source_type: string; currency_code: string; decimal_places: number; limit_minor_units: Amount; outstanding_minor_units: Amount; available_minor_units: Amount; updated_by: string; updated_epoch: number };
type Draw = { funding_id: number; reference_key: string; source_type: string; counterparty: string; currency_code: string; decimal_places: number; principal_minor_units: Amount; rate_bps: number;
  collateral_minor_units: Amount; haircut_bps: number; start_epoch: number; maturity_epoch: number; status: "active" | "repaid"; interest_minor_units: Amount; repaid_epoch: number | null; overdue: boolean };
type Profile = Record<string, { decimal_places: number; outstanding_minor_units: Amount; weighted_average_rate_bps: Amount; maturity_ladder: { bucket: string; outstanding_minor_units: Amount }[] }>;
type Overview = { issuer: string; as_of_epoch: number; limits: Limit[]; draws: Draw[]; overdue_count: number; profile: { currencies: Profile } };
type Result = { success: boolean; result: { fundingId?: number; repaid?: unknown[]; failed?: { funding_id: number; error: string }[] } };

const SOURCES: [string, string][] = [["bank_bond", "은행채"], ["interbank", "은행간"], ["repo", "RP(환매조건부)"], ["central_bank", "중앙은행"]];
const SOURCE_LABEL = Object.fromEntries(SOURCES);
const BUCKET: Record<string, string> = { "0-7d": "1주 이내", "8-30d": "1개월", "31-90d": "3개월", "91-365d": "1년", "365d+": "1년 초과" };

/** Minor units as an exact decimal text (BigInt, so a 20-digit balance does not lose its last digits). */
function units(value: Amount, places: number) {
  const raw = BigInt(typeof value === "number" ? Math.trunc(value) : String(value || "0"));
  const abs = raw < 0n ? -raw : raw, scale = 10n ** BigInt(places);
  const fraction = places ? `.${(abs % scale).toString().padStart(places, "0")}` : "";
  return `${raw < 0n ? "−" : ""}${(abs / scale).toLocaleString("ko-KR")}${fraction}`;
}
const percent = (bps: number) => `${(bps / 100).toFixed(2)}%`;
const newReference = () => `ui-${crypto.randomUUID()}`;

/** 도매조달: limits, draws and repayments of one bank's wholesale funding, and the maturity ladder. */
export function FundingPanel({ ctx, bankId, currency }: { ctx: Ctx; bankId: string; currency: string }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<Overview | null>(null);
  const [draw, setDraw] = useState({ source: "interbank", currency, amount: "", ratePercent: "", days: "", counterparty: "", collateral: "", haircutPercent: "", reference: newReference() });
  const [limit, setLimit] = useState({ source: "interbank", currency, amount: "" });

  const load = useCallback(() => {
    if (!bankId) return;
    api<Overview>(`/api/admin/funding?bank_id=${encodeURIComponent(bankId)}`).then(setData)
      .catch((cause) => say(errorText(cause, "도매조달 정보를 불러오지 못했습니다."), true));
  }, [bankId]);
  useEffect(() => { setData(null); load(); }, [bankId]);
  useEffect(() => { setDraw((current) => ({ ...current, currency })); setLimit((current) => ({ ...current, currency })); }, [currency]);

  const post = (body: Record<string, unknown>, fallback: string, done: (result: Result["result"]) => void) =>
    run(() => api<Result>("/api/admin/funding", { method: "POST", body: JSON.stringify({ bankId, ...body }) }), fallback, (response) => { done(response.result); load(); });

  function submitLimit() {
    if (!/^\d{1,15}(\.\d{1,6})?$/.test(limit.amount)) { say("한도는 0 이상의 숫자(소수 6자리까지)로 입력해주세요.", true); return; }
    if (!window.confirm(`${SOURCE_LABEL[limit.source]} · ${limit.currency} 한도를 ${limit.amount}(으)로 정할까요?`)) return;
    void post({ action: "set_limit", sourceType: limit.source, currency: limit.currency.trim().toUpperCase(), limit: limit.amount }, "한도를 저장하지 못했습니다.", () => { say("한도를 저장했습니다."); setLimit({ ...limit, amount: "" }); });
  }
  function submitDraw() {
    const rate = Number(draw.ratePercent), days = Number(draw.days), haircut = Number(draw.haircutPercent || "0");
    if (!/^\d{1,15}(\.\d{1,6})?$/.test(draw.amount) || !/^\d{1,15}(\.\d{1,6})?$/.test(draw.collateral || "0")) { say("금액과 담보는 0 이상의 숫자(소수 6자리까지)로 입력해주세요.", true); return; }
    if (!(rate >= 0 && rate <= 1000) || !Number.isInteger(days) || days < 1 || days > 36500 || !(haircut >= 0 && haircut <= 100)) { say("금리(0~1000%), 만기(1~36500일), 헤어컷(0~100%)을 확인해주세요.", true); return; }
    if (!window.confirm(`${SOURCE_LABEL[draw.source]} ${draw.amount} ${draw.currency}을(를) 연 ${rate}% · ${days}일 만기로 조달할까요? 은행 원장에 조달 부채가 기록됩니다.`)) return;
    void post({ action: "draw", sourceType: draw.source, currency: draw.currency.trim().toUpperCase(), amount: draw.amount, rateBps: Math.round(rate * 100), maturityDays: days, referenceKey: draw.reference,
      counterparty: draw.counterparty.trim(), collateral: draw.collateral || "0", haircutBps: Math.round(haircut * 100) }, "조달을 실행하지 못했습니다.",
    (result) => { say(`조달 #${result.fundingId}을 기록했습니다.`); setDraw({ ...draw, amount: "", ratePercent: "", days: "", counterparty: "", collateral: "", haircutPercent: "", reference: newReference() }); });
  }
  function repay(row: Draw) {
    if (!window.confirm(`조달 #${row.funding_id}(${units(row.principal_minor_units, row.decimal_places)} ${row.currency_code})을 지금 상환할까요? 원금과 실제 경과일 기준(ACT/365) 이자가 기록됩니다.`)) return;
    void post({ action: "repay", fundingId: row.funding_id }, "상환하지 못했습니다.", () => say(`조달 #${row.funding_id}을 상환했습니다.`));
  }
  function processDue() {
    if (!window.confirm(`만기가 지난 ${data?.overdue_count ?? 0}건을 만기순으로 상환할까요? 이 은행의 조달만 처리하며, 실패한 건은 건너뛰고 알려 드립니다.`)) return;
    void post({ action: "process_due" }, "만기 처리에 실패했습니다.", (result) => {
      const failed = result.failed ?? [];
      say(`${result.repaid?.length ?? 0}건을 상환했습니다.${failed.length ? ` 실패 ${failed.length}건: ${failed.map((row) => `#${row.funding_id} ${row.error}`).join(", ")}` : ""}`, failed.length > 0);
    });
  }

  if (!data) return <p className="cap">불러오는 중…</p>;
  const currencies = Object.entries(data.profile.currencies);
  return <>
    <div className="alm-two">
      {currencies.length === 0 ? <Card title="만기 사다리" caption="남아 있는 조달의 만기별 원금입니다."><p className="cap">남아 있는 조달이 없습니다.</p></Card> : currencies.map(([code, entry]) => {
        const largest = Math.max(1, ...entry.maturity_ladder.map((row) => Number(row.outstanding_minor_units)));
        return <Card key={code} title={`만기 사다리 · ${code}`} caption={`잔액 ${units(entry.outstanding_minor_units, entry.decimal_places)} · 가중평균 금리 ${percent(Number(entry.weighted_average_rate_bps))}`}>
          <div className="alm-gauges">{entry.maturity_ladder.map((row) => <div className="alm-gauge" key={row.bucket}>
            <div className="row"><span>{BUCKET[row.bucket] ?? row.bucket}</span><b>{units(row.outstanding_minor_units, entry.decimal_places)}</b></div>
            <div className="alm-track"><i style={{ width: `${(Number(row.outstanding_minor_units) / largest) * 100}%` }} /></div></div>)}</div>
        </Card>;
      })}
      <Card title="한도" caption="조달원·통화별로 한도 안에서만 조달할 수 있습니다. 한도는 운영자 설정이며 규제 수치가 아닙니다.">
        <div className="scroll-x"><table><thead><tr><th>조달원</th><th>통화</th><th className="alm-num">한도</th><th className="alm-num">사용</th><th className="alm-num">남은 한도</th></tr></thead><tbody>
          {data.limits.length === 0 ? <tr><td colSpan={5} className="cap">설정된 한도가 없습니다. 한도를 정하면 조달할 수 있습니다.</td></tr> : data.limits.map((row) => <tr key={`${row.source_type}${row.currency_code}`}>
            <td>{SOURCE_LABEL[row.source_type] ?? row.source_type}</td><td>{row.currency_code}</td><td className="alm-num">{units(row.limit_minor_units, row.decimal_places)}</td>
            <td className="alm-num">{units(row.outstanding_minor_units, row.decimal_places)}</td><td className="alm-num">{units(row.available_minor_units, row.decimal_places)}</td></tr>)}
        </tbody></table></div>
        <div className="alm-edit">
          <select aria-label="한도 조달원" value={limit.source} onChange={(event) => setLimit({ ...limit, source: event.target.value })}>{SOURCES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
          <input aria-label="한도 통화" placeholder="통화" value={limit.currency} onChange={(event) => setLimit({ ...limit, currency: event.target.value })} />
          <input inputMode="decimal" aria-label="한도 금액" placeholder="한도" value={limit.amount} onChange={(event) => setLimit({ ...limit, amount: event.target.value })} />
          <button type="button" className="btn primary" disabled={busy || !limit.amount} onClick={submitLimit}>한도 저장</button>
        </div>
      </Card>
    </div>

    <Card title="조달 내역" caption="최근 100건. 원금은 만기에 이자와 함께 상환합니다. 연체는 만기가 지났는데 아직 상환하지 않은 건입니다."
      actions={<button type="button" className="outline" disabled={busy || data.overdue_count === 0} onClick={processDue}>만기 도래 {data.overdue_count}건 처리</button>}>
      <div className="scroll-x"><table><thead><tr><th>#</th><th>조달원</th><th>상대방</th><th>통화</th><th className="alm-num">원금</th><th className="alm-num">금리</th><th>실행</th><th>만기</th><th>상태</th><th /></tr></thead><tbody>
        {data.draws.length === 0 ? <tr><td colSpan={10} className="cap">조달 내역이 없습니다.</td></tr> : data.draws.map((row) => <tr key={row.funding_id}>
          <td>{row.funding_id}</td><td>{SOURCE_LABEL[row.source_type] ?? row.source_type}</td><td>{row.counterparty || "—"}</td><td>{row.currency_code}</td>
          <td className="alm-num">{units(row.principal_minor_units, row.decimal_places)}</td><td className="alm-num">{percent(row.rate_bps)}</td>
          <td>{date(row.start_epoch)}</td><td>{date(row.maturity_epoch)}</td>
          <td>{row.status === "repaid" ? <span className="tag ok">상환 · 이자 {units(row.interest_minor_units, row.decimal_places)}</span> : row.overdue ? <span className="tag danger">연체</span> : <span className="tag warn">진행</span>}</td>
          <td>{row.status === "active" && <button type="button" className="outline" disabled={busy} onClick={() => repay(row)}>상환</button>}</td></tr>)}
      </tbody></table></div>
    </Card>

    <Card title="새 조달 실행" caption="은행 원장에 결제자산 차변·조달 부채 대변이 기록됩니다. RP·중앙은행 조달은 헤어컷을 뺀 담보 가치가 조달 금액 이상이어야 합니다.">
      <div className="alm-edit">
        <select aria-label="조달원" value={draw.source} onChange={(event) => setDraw({ ...draw, source: event.target.value })}>{SOURCES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
        <input aria-label="통화" placeholder="통화" value={draw.currency} onChange={(event) => setDraw({ ...draw, currency: event.target.value })} />
        <input inputMode="decimal" aria-label="금액" placeholder="금액" value={draw.amount} onChange={(event) => setDraw({ ...draw, amount: event.target.value })} />
        <input inputMode="decimal" aria-label="연 금리(%)" placeholder="연 금리 %" value={draw.ratePercent} onChange={(event) => setDraw({ ...draw, ratePercent: event.target.value })} />
        <input inputMode="numeric" aria-label="만기(일)" placeholder="만기 일수" value={draw.days} onChange={(event) => setDraw({ ...draw, days: event.target.value })} />
        <input aria-label="상대방" placeholder="상대방(선택)" value={draw.counterparty} onChange={(event) => setDraw({ ...draw, counterparty: event.target.value })} />
        <input inputMode="decimal" aria-label="담보 가치" placeholder="담보 가치(RP·중앙은행)" value={draw.collateral} onChange={(event) => setDraw({ ...draw, collateral: event.target.value })} />
        <input inputMode="decimal" aria-label="헤어컷(%)" placeholder="헤어컷 %" value={draw.haircutPercent} onChange={(event) => setDraw({ ...draw, haircutPercent: event.target.value })} />
        <button type="button" className="btn primary" disabled={busy || !draw.amount || !draw.ratePercent || !draw.days} onClick={submitDraw}>조달 실행</button>
      </div>
      <p className="alm-note">같은 화면에서 버튼을 두 번 눌러도 한 번만 실행됩니다(요청마다 고유 키를 보냅니다).</p>
    </Card>
  </>;
}
