import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, date, errorText, type Ctx } from "./common";
import "./accounting-close.css";

type Gate = { key: string; label: string; ok: boolean; detail: string };
type Residual = { currency: string; decimalPlaces: number; dust: string; recognized: number; pending: number; transferred: number; balance: number };
type Entry = { clearing_id: number; kind: "recognize" | "transfer"; currency_code: string; decimal_places: number; minor_units: number; recipient_user_id: string | null; actor: string; recorded_epoch: number };
type Position = { currency: string; decimalPlaces: number; position: number; rate: string; previousRate: string | null; pnl: number; baseline: boolean };
type RevalRun = { run_id: number; kind: "baseline" | "revalue" | "reverse"; period_key: string; currency_code: string; decimal_places: number; position_minor_units: number; rate_text: string; pnl_base_minor_units: number; actor: string; recorded_epoch: number };
type Overview = { gates: Gate[]; checked: boolean; closeReady: boolean | null; residuals: Residual[]; history: Entry[]; revaluation: { preview: Position[]; history: RevalRun[]; period: string } };
/** A stored rate is an exact fraction ("1/120"); show it with six decimals. */
const rateValue = (text: string) => { const [top, bottom] = text.split("/"); return (Number(top) / Number(bottom ?? 1)).toFixed(6); };
const RUN_LABEL = { baseline: "기준 저장", revalue: "평가", reverse: "되돌림" } as const;

/** Show a count of minor units as an amount in the currency (e.g. 120 with 2 places -> "1.20"). */
const amount = (units: number, places: number) => (units / 10 ** places).toLocaleString("ko-KR", { minimumFractionDigits: places, maximumFractionDigits: places });

/** 회계 마감: close gates and the FX rounding dust (환전 손익) with its recognize / move-to-treasury actions. */
export function AccountingClosePage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<Overview | null>(null);
  const [moving, setMoving] = useState<Record<string, string>>({});

  const load = useCallback(async () => setData(await api<Overview>("/api/admin/accounting-close")), []);
  const check = () => void run(() => api<Overview>("/api/admin/accounting-close?check=1"), "점검하지 못했습니다.", setData);
  useEffect(() => { load().catch((cause) => say(errorText(cause, "회계 마감 정보를 불러오지 못했습니다."), true)); }, [load]);

  const act = (body: Record<string, unknown>, fallback: string, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    void run(() => api<{ message: string }>("/api/admin/accounting-close", { method: "POST", body: JSON.stringify(body) }), fallback,
      (result) => { say(result.message); void load().then(() => undefined); });
  };

  if (!data) return <p className="cap">불러오는 중…</p>;
  const waiting = data.residuals.filter((row) => row.pending > 0);
  return <>
    <Card title="마감 게이트" caption="모든 항목이 통과해야 기간을 닫을 수 있습니다. 통합 원장 대사는 큰 DB에서 오래 걸려서 누를 때만 실행합니다.">
      <table className="close-gates"><tbody>
        {data.gates.map((gate) => <tr key={gate.key}><td>{gate.label}</td><td className="cap">{gate.detail}</td>
          <td><span className={`tag ${gate.ok ? "ok" : "warn"}`}>{gate.ok ? "통과" : "확인 필요"}</span></td></tr>)}
      </tbody></table>
      <p className="close-summary">
        {!data.checked ? <span className="tag warn">전체 점검 전</span> : data.closeReady ? <span className="tag ok">마감 가능</span> : <span className="tag warn">마감 전에 확인할 항목이 있습니다</span>}
        <button type="button" className="outline" disabled={busy} onClick={check}>{data.checked ? "점검 다시 실행" : "전체 점검 실행"}</button></p>
    </Card>

    <Card title="환전 반올림 잔여" caption="환전은 받는 통화의 최소 단위로 내림해서 지급하므로, 남는 조각이 시스템에 쌓입니다. 정리하면 한 단위 이상 모인 만큼이 환전 손익 계정에 기록됩니다. 이 계정은 계속 쌓이고, 잔액은 아래에서 국고로 옮길 수 있습니다."
      actions={<button type="button" className="btn primary" disabled={busy || !waiting.length}
        onClick={() => act({ action: "recognize" }, "잔여를 정리하지 못했습니다.")}>잔여 정리 분개 만들기</button>}>
      {data.residuals.length === 0 ? <p className="cap">환전 기록이 아직 없습니다.</p> : <div className="scroll-x"><table>
        <thead><tr><th>통화</th><th>쌓인 조각</th><th>정리 대기</th><th>환전 손익 잔액</th><th>국고로 이전</th><th /></tr></thead>
        <tbody>{data.residuals.map((row) => {
          const value = moving[row.currency] ?? "";
          return <tr key={row.currency}>
            <td>{row.currency}</td><td>{row.dust}</td>
            <td>{row.pending > 0 ? <span className="tag warn">{amount(row.pending, row.decimalPlaces)}</span> : <span className="tag ok">없음</span>}</td>
            <td>{amount(row.balance, row.decimalPlaces)} <span className="cap">(이전 {amount(row.transferred, row.decimalPlaces)})</span></td>
            <td><input className="close-move" inputMode="decimal" aria-label={`${row.currency} 이전 금액`} placeholder="금액" value={value} disabled={busy || row.balance <= 0}
              onChange={(event) => setMoving({ ...moving, [row.currency]: event.target.value })} /></td>
            <td><button type="button" className="outline" disabled={busy || row.balance <= 0 || !value}
              onClick={() => {
                const units = Math.round(Number(value) * 10 ** row.decimalPlaces);
                if (!Number.isFinite(units) || units <= 0) { say("이전 금액을 올바르게 입력해주세요.", true); return; }
                act({ action: "transfer", currency: row.currency, units }, "이전하지 못했습니다.",
                  `${row.currency} ${value}을(를) 환전 손익 계정에서 해당 나라 국고로 이전할까요?\n분개와 작업 기록이 남고 되돌릴 수 없습니다.`);
                setMoving({ ...moving, [row.currency]: "" });
              }}>이전</button></td></tr>;
        })}</tbody>
      </table></div>}
    </Card>

    <Card title="외환 포지션 평가" caption="환전 중간 계정에 남아 있는 통화별 포지션을 기간 말 환율로 다시 평가합니다. 이전 기간 말 환율과의 차이만큼 평가손익을 기준통화로 장부에 남기고, 다음 기간 첫날 자동으로 반대 분개로 되돌립니다. 통화의 첫 실행은 환율만 저장합니다."
      actions={<button type="button" className="btn primary" disabled={busy} onClick={() => act({ action: "revalue" }, "평가하지 못했습니다.", `${data.revaluation.period} 기간의 외환 포지션을 평가할까요?\n평가 분개가 장부에 남습니다.`)}>이번 기간 평가 실행</button>}>
      <div className="scroll-x"><table>
        <thead><tr><th>통화</th><th>포지션</th><th>이전 환율</th><th>현재 환율</th><th>평가손익 (기준통화)</th></tr></thead>
        <tbody>{data.revaluation.preview.map((row) => <tr key={row.currency}>
          <td>{row.currency}</td><td>{amount(row.position, row.decimalPlaces)}</td>
          <td>{row.previousRate ?? <span className="tag warn">기준 없음</span>}</td><td>{row.rate}</td>
          <td className={row.pnl > 0 ? "close-up" : row.pnl < 0 ? "close-down" : ""}>{row.baseline ? "첫 실행은 기준만 저장" : `${row.pnl > 0 ? "+" : ""}${row.pnl.toLocaleString("ko-KR")}`}</td></tr>)}
        </tbody></table></div>
      {data.revaluation.history.length > 0 && <div className="scroll-x close-sub"><table>
        <thead><tr><th>시각</th><th>기간</th><th>구분</th><th>통화</th><th>포지션</th><th>환율</th><th>손익</th></tr></thead>
        <tbody>{data.revaluation.history.map((run) => <tr key={run.run_id}><td>{date(run.recorded_epoch)}</td><td>{run.period_key}</td><td>{RUN_LABEL[run.kind]}</td>
          <td>{run.currency_code}</td><td>{amount(run.position_minor_units, run.decimal_places)}</td><td>{rateValue(run.rate_text)}</td>
          <td>{run.pnl_base_minor_units ? run.pnl_base_minor_units.toLocaleString("ko-KR") : "—"}</td></tr>)}</tbody></table></div>}
    </Card>

    {data.history.length > 0 && <Card title="정리·이전 기록" caption="최근 50건">
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>구분</th><th>통화</th><th>금액</th><th>받은 곳</th><th>실행</th></tr></thead><tbody>
        {data.history.map((item) => <tr key={item.clearing_id}><td>{date(item.recorded_epoch)}</td>
          <td>{item.kind === "recognize" ? "정리(기록)" : "국고 이전"}</td><td>{item.currency_code}</td>
          <td>{amount(item.minor_units, item.decimal_places)}</td><td className="mono">{item.recipient_user_id ?? "—"}</td><td className="mono">{item.actor}</td></tr>)}
      </tbody></table></div>
    </Card>}
  </>;
}
