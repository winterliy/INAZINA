import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, date, errorText, number, type Ctx } from "./common";
import "./liquidity.css";

type Profile = "low" | "medium" | "high";
type Provider = {
  companyCode: string; name: string; profile: Profile; enabled: boolean; halted: boolean; initialCapital: number;
  cash: number; holdingsValue: number; equity: number; pnl: number; lastRunEpoch: number; openOrders: number;
};
type Overview = {
  providers: Provider[]; candidates: { code: string; name: string }[]; firmsPending: string[];
  profiles: Record<Profile, { spreadBps: number; levels: number; assets: number; cycleSeconds: number }>;
};
type Stock = { assetCode: string; name: string; price: number; held: number; buys: number[]; sells: number[]; state: "quoting" | "event" | "halted" | "off" | "waiting" };

const PROFILE_LABEL: Record<Profile, string> = { low: "낮음", medium: "중간", high: "높음" };
const STATE_LABEL: Record<Stock["state"], [string, string]> = {
  quoting: ["호가 중", "ok"], event: ["사건 진행 중 · 거둠", "warn"], halted: ["자본 부족으로 중지", "danger"],
  off: ["꺼짐", "warn"], waiting: ["다음 갱신 대기", "warn"],
};
const list = (prices: number[]) => prices.length ? prices.map(number).join(" · ") : "—";

/** 유동성: companies whose algorithm quotes the order book under their own names. */
export function LiquidityPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<Overview | null>(null);
  const [addCode, setAddCode] = useState("");
  const [addProfile, setAddProfile] = useState<Profile>("medium");
  const [open, setOpen] = useState<string | null>(null);
  const [stocks, setStocks] = useState<Stock[] | null>(null);

  const load = useCallback(async () => setData(await api<Overview>("/api/admin/liquidity")), []);
  const loadDetail = useCallback(async (code: string) => {
    setStocks((await api<{ stocks: Stock[] }>(`/api/admin/liquidity/${encodeURIComponent(code)}`)).stocks);
  }, []);
  useEffect(() => {
    load().catch((cause) => say(errorText(cause, "유동성 회사를 불러오지 못했습니다."), true));
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load().catch(() => undefined); }, 10_000);
    return () => window.clearInterval(timer);
  }, [load]);
  useEffect(() => {
    if (!open) { setStocks(null); return; }
    loadDetail(open).catch((cause) => say(errorText(cause, "종목별 호가를 불러오지 못했습니다."), true));
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void loadDetail(open).catch(() => undefined); }, 10_000);
    return () => window.clearInterval(timer);
  }, [open, loadDetail]);

  const act = (body: Record<string, unknown>, fallback: string, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    void run(() => api<{ message: string }>("/api/admin/liquidity", { method: "POST", body: JSON.stringify(body) }), fallback,
      (result) => { say(result.message); void load(); if (open) void loadDetail(open).catch(() => undefined); });
  };

  if (!data) return <p className="cap">불러오는 중…</p>;
  const spec = (profile: Profile) => { const item = data.profiles[profile]; return `스프레드 ${(item.spreadBps / 100).toFixed(2)}% · ${item.levels}단계 · ${item.assets}종목 · ${item.cycleSeconds}초`; };
  const providers = data.providers;
  const active = providers.filter((item) => item.enabled && !item.halted).length;
  const halted = providers.filter((item) => item.halted).length;
  const initial = providers.reduce((sum, item) => sum + item.initialCapital, 0);
  const equity = providers.reduce((sum, item) => sum + item.equity, 0);
  const pnl = equity - initial;
  const orders = providers.reduce((sum, item) => sum + item.openOrders, 0);
  const lastRun = Math.max(0, ...providers.map((item) => item.lastRunEpoch));
  return <>
    {data.firmsPending.length > 0 && <Card title="유동성 회사 만들기" caption="서로 관계없는 비상장·비주식회사 12곳(낮음·중간·높음 각 4곳)을 만들고 낙원국고에서 회사마다 5,000,000,000 NWX를 지급합니다.">
      <button type="button" className="btn primary" disabled={busy}
        onClick={() => act({ action: "provision" }, "회사를 만들지 못했습니다.", `${data.firmsPending.length}개 회사를 만들고 낙원국고에서 자본을 지급할까요?\n지급은 되돌릴 수 없습니다.`)}>
        회사 {data.firmsPending.length}개 만들기
      </button>
    </Card>}

    <div className="tile-row">
      <div className="tile"><div className="tile-label">공급 중인 회사</div><div className="tile-value">{active} / {providers.length}</div>
        <p className="cap liq-tile-note">{providers.length - active - halted}곳 꺼짐 · {halted}곳 자본 부족 중지</p></div>
      <div className="tile"><div className="tile-label">총 순자산</div><div className="tile-value liq-tile-number">{number(equity)}</div>
        <p className="cap liq-tile-note">시작 자본 {number(initial)} NWX</p></div>
      <div className="tile"><div className="tile-label">총 손익</div><div className={`tile-value liq-tile-number ${pnl > 0 ? "up" : pnl < 0 ? "down" : ""}`}>{pnl > 0 ? "+" : ""}{number(pnl)}</div>
        <p className="cap liq-tile-note">{initial ? `${pnl > 0 ? "+" : ""}${(pnl / initial * 100).toFixed(2)}%` : "—"}</p></div>
      <div className="tile"><div className="tile-label">올려 둔 호가</div><div className="tile-value">{orders}건</div>
        <p className="cap liq-tile-note">{lastRun ? `마지막 갱신 ${date(lastRun)}` : "아직 호가를 올리지 않았습니다"}</p></div>
    </div>

    <Card title="유동성 공급 회사" caption="회사를 누르면 아래에 종목별 호가가 열립니다. 자본이 시작 자본의 절반(막대의 세로선) 아래로 내려가면 그 회사는 거래를 멈춥니다. 가격 사건이 진행 중이거나 가격이 호가를 지나가면 호가를 거둡니다.">
      <div className="scroll-x"><table className="liq-table">
        <thead><tr><th>회사</th><th>성향</th><th>상태</th><th>자본 (시작 대비)</th><th>순자산</th><th>손익</th><th>호가</th><th /></tr></thead>
        <tbody>
          {providers.map((item) => {
            const ratio = item.initialCapital ? item.equity / item.initialCapital : 1;
            const expanded = open === item.companyCode;
            return <Fragment key={item.companyCode}>
              <tr className={expanded ? "liq-open" : ""}>
                <td><button type="button" className="liq-name" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : item.companyCode)}>
                  <span aria-hidden="true">{expanded ? "▾" : "▸"}</span> {item.name} <span className="mono">{item.companyCode}</span></button></td>
                <td><div className="liq-seg" role="group" aria-label={`${item.name} 유동성 성향`}>
                  {(Object.keys(PROFILE_LABEL) as Profile[]).map((key) => <button key={key} type="button" aria-pressed={item.profile === key} disabled={busy} title={spec(key)}
                    onClick={() => item.profile !== key && act({ action: "profile", companyCode: item.companyCode, profile: key }, "성향을 바꾸지 못했습니다.")}>{PROFILE_LABEL[key]}</button>)}
                </div></td>
                <td>{item.halted ? <span className="tag danger">자본 부족으로 중지</span> : item.enabled ? <span className="tag ok">공급 중</span> : <span className="tag warn">꺼짐</span>}</td>
                <td><div className="liq-capbar" role="img" aria-label={`시작 자본의 ${(ratio * 100).toFixed(0)}%`}><i style={{ width: `${Math.min(100, ratio * 50)}%` }} /><u /></div></td>
                <td>{number(item.equity)}</td>
                <td><span className={item.pnl > 0 ? "liq-up" : item.pnl < 0 ? "liq-down" : ""}>{item.pnl > 0 ? "+" : ""}{number(item.pnl)}</span></td>
                <td>{item.openOrders}건</td>
                <td>
                  <button type="button" className="outline" disabled={busy}
                    onClick={() => act({ action: "enable", companyCode: item.companyCode, enabled: !item.enabled || item.halted }, "상태를 바꾸지 못했습니다.")}>
                    {item.enabled && !item.halted ? "끄기" : "켜기"}
                  </button>{" "}
                  <button type="button" className="outline" disabled={busy}
                    onClick={() => act({ action: "remove", companyCode: item.companyCode }, "회사를 빼지 못했습니다.", `${item.name}을(를) 유동성 공급에서 뺄까요?\n올려 둔 호가는 모두 거둡니다. 회사와 자본은 그대로 남습니다.`)}>
                    빼기
                  </button>
                </td>
              </tr>
              {expanded && <tr className="liq-detail"><td colSpan={8}>
                <p className="cap">{item.name} · 종목별 호가 · 현금 {number(item.cash)} · 보유 주식 {number(item.holdingsValue)}</p>
                {!stocks ? <p className="cap">불러오는 중…</p> : stocks.length === 0 ? <p className="cap">아직 호가를 올린 종목이 없습니다.</p> :
                  <div className="scroll-x"><table><thead><tr><th>종목</th><th>현재가</th><th>보유</th><th>매수 호가</th><th>매도 호가</th><th>상태</th></tr></thead><tbody>
                    {stocks.map((stock) => <tr key={stock.assetCode}>
                      <td>{stock.name} <span className="mono">{stock.assetCode}</span></td><td>{number(stock.price)}</td><td>{number(stock.held)}주</td>
                      <td>{list(stock.buys)}</td><td>{list(stock.sells)}</td>
                      <td><span className={`tag ${STATE_LABEL[stock.state][1]}`}>{STATE_LABEL[stock.state][0]}</span></td></tr>)}
                  </tbody></table></div>}
              </td></tr>}
            </Fragment>;
          })}
          {!providers.length && <tr><td colSpan={8} className="cap">유동성 공급 회사가 없습니다. 아래에서 회사를 넣어 주세요.</td></tr>}
        </tbody>
      </table></div>
    </Card>

    <Card title="성향 안내" caption="성향이 바뀌면 다음 갱신 주기부터 적용됩니다.">
      <div className="liq-profiles">{(Object.keys(PROFILE_LABEL) as Profile[]).map((key) => <div key={key}><b>{PROFILE_LABEL[key]}</b><span>{spec(key)}</span></div>)}</div>
    </Card>

    <Card title="회사 넣기" caption="이미 있는 회사의 자본 계정으로 거래합니다. 넣는 시점의 순자산이 시작 자본이 됩니다.">
      <div className="liq-add">
        <label htmlFor="liq-company">회사<select id="liq-company" value={addCode} onChange={(event) => setAddCode(event.target.value)}>
          <option value="">선택</option>
          {data.candidates.map((item) => <option key={item.code} value={item.code}>{item.name} ({item.code})</option>)}
        </select></label>
        <label htmlFor="liq-profile">유동성 성향<select id="liq-profile" value={addProfile} onChange={(event) => setAddProfile(event.target.value as Profile)}>
          {(Object.keys(PROFILE_LABEL) as Profile[]).map((key) => <option key={key} value={key}>{PROFILE_LABEL[key]} · {spec(key)}</option>)}
        </select></label>
        <button type="button" className="btn primary" disabled={busy || !addCode}
          onClick={() => act({ action: "add", companyCode: addCode, profile: addProfile }, "회사를 넣지 못했습니다.")}>넣기</button>
      </div>
    </Card>
  </>;
}
