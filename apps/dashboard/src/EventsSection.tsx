import { lazy, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { moneyCompare, validMoney } from "../../shared/money";
import type { Event, Flow, Instrument } from "./appTypes";
import { confirmAction, date, number, type Ctx } from "./common";
import { LogCard } from "./lazyLog";
const EventTimeline = lazy(() => import("./ActivityTiles").then((module) => ({ default: module.EventTimeline })));

/** The "시장 사건 등록" form. It lives in App (not in the section) so a half-filled form survives page changes. */
export function useEventForm(ctx: Ctx, instruments: Instrument[]) {
  const { say, run } = ctx;
  const [type, setType] = useState("good");
  const [severity, setSeverity] = useState("5");
  const [duration, setDuration] = useState("3600");
  const [asset, setAsset] = useState("");
  const [target, setTarget] = useState("");
  const instrument = instruments.find((item) => item.code === asset.trim());

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const percent = Number(severity);
    const code = asset.trim();
    const targetPrice = target.trim() ? target.trim() : null;
    // Same rules as the Discord $사건 form: a target price needs one asset and
    // must lie above the current price for good news, below it for bad news.
    if (targetPrice !== null) {
      if (!validMoney(targetPrice, true)) { say("목표가는 0보다 큰 금액으로 입력해주세요.", true); return; }
      if (!code) { say("목표가를 지정하려면 대상 종목 코드를 입력해주세요.", true); return; }
      if (instrument && (type === "good" ? moneyCompare(targetPrice, instrument.price) < 0 : moneyCompare(targetPrice, instrument.price) > 0)) {
        say(`${type === "good" ? "호재 목표가는 현재가 이상" : "악재 목표가는 현재가 이하"}로 입력해주세요. (현재가 ${number(instrument.price)})`, true);
        return;
      }
    }
    const goal = targetPrice !== null ? `목표가 ${number(targetPrice)}` : `변동률 ${percent}%`;
    if (!await confirmAction(`${code || "시장 전체"}에 ${type === "good" ? "호재" : "악재"} 사건(${goal})을 적용할까요? 가격에 영향을 줍니다.`)) return;
    void run(() => api<{ success: boolean; message: string }>("/api/admin/events", {
      method: "POST", body: JSON.stringify({ eventType: type, severity: Math.round(percent), targetPercent: percent, targetPrice, durationSeconds: Number(duration), assetCode: code || null }),
    }), "시장 사건을 등록하지 못했습니다.", (result) => { say(result.message, !result.success); if (result.success) setTarget(""); });
  }

  return { type, setType, severity, setSeverity, duration, setDuration, asset, setAsset, target, setTarget, instrument, submit };
}
export type EventForm = ReturnType<typeof useEventForm>;

export function EventsSection({ ctx, form, events, flows, instruments, onChanged }: { ctx: Ctx; form: EventForm; events: Event[]; flows: Flow[]; instruments: Instrument[]; onChanged: () => void }) {
  const { instrument } = form;
  return <>
    <EventTimeline events={events} />
    <section className="glass card mb rise"><div className="card-head"><div><h2>시장 사건 등록</h2><p className="cap">기존 시장 사건 검증과 감사 기록을 적용합니다.</p></div></div><form className="form-grid" onSubmit={form.submit}>
      <label>종류<select value={form.type} onChange={(event) => form.setType(event.target.value)}><option value="good">호재</option><option value="bad">악재</option></select></label>
      <label>{form.target.trim() ? "변동성 (%)" : "목표 변동률 (%)"}<input type="number" min="0" max="100" step="0.1" value={form.severity} onChange={(event) => form.setSeverity(event.target.value)} required /></label>
      <label>지속 시간 (초)<input type="number" min="1" max="604800" value={form.duration} onChange={(event) => form.setDuration(event.target.value)} required /></label>
      <label>대상 종목 코드 (비우면 전체)<input list="event-assets" value={form.asset} onChange={(event) => form.setAsset(event.target.value)} placeholder="예: INZST001" /></label>
      <label>목표가 (선택 · 개별 종목)<input type="number" min="1" step="1" inputMode="numeric" value={form.target} disabled={!form.asset.trim()} onChange={(event) => form.setTarget(event.target.value)} placeholder={instrument ? `현재가 ${number(instrument.price)}` : "종목을 먼저 입력"} /></label>
      <button className="btn primary" disabled={ctx.busy}>사건 등록</button>
      <datalist id="event-assets">{instruments.filter((item) => item.status === "active").map((item) => <option key={item.code} value={item.code}>{item.name} · {number(item.price)}</option>)}</datalist>
    </form><p className="hint">목표가를 비우면 변동률만큼 오르거나(호재) 내립니다. 목표가를 넣으면 그 가격을 향해 움직이고, 변동률은 움직이는 동안의 변동성으로 쓰입니다. 호재는 현재가 이상, 악재는 현재가 이하로 입력하세요.{instrument && ` · ${instrument.name} 현재가 ${number(instrument.price)}`}</p></section>
    <LogCard ctx={ctx} log="events" title="최근 사건" caption="관리자가 등록한 사건 · 최근 50건" rows={events} idOf={(item) => item.event_id} deletable={(item) => !item.active}
      keepNote="진행 중인 사건은 남습니다." empty="등록된 사건이 없습니다." onChanged={onChanged}
      head={<><th>ID</th><th>대상</th><th>유형</th><th>강도</th><th>목표</th><th>시작</th><th>종료</th><th>관리자</th></>}
      row={(item) => <><td>#{item.event_id}{item.active && <span className="tag ok inline">진행 중</span>}</td><td>{item.asset_code || "시장 전체"}</td><td><span className={item.event_type === "good" ? "tag up" : "tag down"}>{item.event_type === "good" ? "호재" : "악재"}</span></td><td>{item.severity}%</td><td>{item.target_value === null ? "—" : item.target_mode === "price" ? number(item.target_value) : `${number(item.target_value)}%`}</td><td>{date(item.starts_epoch)}</td><td>{date(item.ends_epoch)}</td><td className="mono">{item.created_by}</td></>} />
    <LogCard ctx={ctx} log="flows" title="알고리즘 시장 흐름" caption="시세 엔진이 종목마다 정한 자동 목표가 · 최근 100건 (최대 5,000건 보관)" rows={flows} idOf={(item) => item.history_id}
      empty="기록된 흐름이 없습니다. 시세 엔진이 새 목표가를 정하면 여기에 쌓입니다." onChanged={onChanged}
      head={<><th>#</th><th>종목</th><th>방향</th><th className="r">기준가</th><th className="r">목표가</th><th className="r">변동</th><th>시작</th><th>종료</th></>}
      row={(item) => <><td className="mono">#{item.history_id}{item.active && <span className="tag ok inline">진행 중</span>}</td><td className="mono">{item.asset_code}</td><td><span className={item.change_percent >= 0 ? "tag up" : "tag down"}>{item.change_percent >= 0 ? "상승" : "하락"}</span></td><td className="r mono">{number(item.anchor_price)}</td><td className="r mono">{number(item.target_price)}</td><td className="r mono">{item.change_percent >= 0 ? "+" : ""}{Number(item.change_percent).toFixed(2)}%</td><td>{date(item.start_epoch)}</td><td>{date(item.end_epoch)}</td></>} />
  </>;
}
