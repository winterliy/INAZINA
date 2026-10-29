import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Field, date, plain, type Ctx } from "./common";

type Warning = { asset_code: string; level: "caution" | "warning" | "danger"; reason: string; set_by: string; created_epoch: number; expires_epoch: number | null };
type Result = { message: string; success: boolean };

const LEVELS: [Warning["level"], string][] = [["caution", "투자주의"], ["warning", "투자경고"], ["danger", "투자위험"]];
const NAME = Object.fromEntries(LEVELS) as Record<Warning["level"], string>;

/** 거래경고: designate instruments as 투자주의·경고·위험; shown in Discord, the 나루 and order confirmations. */
export function WarningsCard({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [warnings, setWarnings] = useState<Warning[]>([]);
  const [draft, setDraft] = useState({ code: "", level: "warning", reason: "", days: "" });

  const load = useCallback(async () => setWarnings((await api<{ warnings: Warning[] }>("/api/admin/warnings")).warnings), []);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "거래경고를 불러오지 못했습니다.", true)); }, [load]);

  function save(code: string, body: Record<string, string>, question: string) {
    if (!window.confirm(question)) return;
    void run(() => api<Result>(`/api/admin/warnings/${encodeURIComponent(code)}`, { method: "POST", body: JSON.stringify(body) }),
      "거래경고를 저장하지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  }

  return <><div className="tile-row warning-lights" aria-label="거래경고 신호등">
    {LEVELS.map(([id, name]) => <section key={id} className={`tile light-${id}`}><p className="tile-label">{name}</p><p className="tile-value">{warnings.filter((item) => item.level === id).length}</p></section>)}
  </div>
  <Card title="거래경고" caption="종목을 투자주의·투자경고·투자위험으로 지정하면 Discord 시세, 나루, 주문 확인에 표시됩니다. 거래를 멈추지는 않습니다 ($거래경고와 같음).">
    <form className="form-grid" onSubmit={(event) => { event.preventDefault(); save(draft.code.trim(), { level: draft.level, reason: draft.reason, days: draft.days }, `${draft.code}을(를) ${NAME[draft.level as Warning["level"]]} 종목으로 지정할까요?`); }}>
      <Field label="종목 코드"><input className="mono" value={draft.code} required onChange={(event) => setDraft({ ...draft, code: event.target.value.trim() })} /></Field>
      <Field label="단계"><select value={draft.level} onChange={(event) => setDraft({ ...draft, level: event.target.value })}>{LEVELS.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></Field>
      <Field label="사유" hint="선택"><input value={draft.reason} maxLength={200} onChange={(event) => setDraft({ ...draft, reason: event.target.value })} /></Field>
      <Field label="기간 (일)" hint="비우면 해제할 때까지"><input inputMode="decimal" value={draft.days} onChange={(event) => setDraft({ ...draft, days: event.target.value })} /></Field>
      <button className="btn primary" disabled={busy || !draft.code}>지정</button>
    </form>
    <div className="scroll-x"><table><thead><tr><th>종목</th><th>단계</th><th>사유</th><th>지정</th><th>만료</th><th /></tr></thead><tbody>
      {warnings.map((item) => <tr key={item.asset_code}>
        <td className="mono">{item.asset_code}</td><td><span className={item.level === "caution" ? "tag" : "tag warn"}>{NAME[item.level]}</span></td>
        <td>{item.reason || "—"}</td><td>{date(item.created_epoch)} · {item.set_by}</td><td>{item.expires_epoch ? date(item.expires_epoch) : "해제 시"}</td>
        <td className="row-actions"><button className="soft small" disabled={busy} onClick={() => save(item.asset_code, { level: "해제" }, `${item.asset_code} 거래경고를 해제할까요?`)}>해제</button></td>
      </tr>)}
    </tbody></table>{!warnings.length && <p className="empty">지정된 거래경고가 없습니다.</p>}</div>
  </Card></>;
}
