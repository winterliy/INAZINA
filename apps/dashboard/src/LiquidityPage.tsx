import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, date, errorText, number, type Ctx } from "./common";

type Profile = "low" | "medium" | "high";
type Provider = {
  companyCode: string; name: string; profile: Profile; enabled: boolean; halted: boolean; initialCapital: number;
  cash: number; holdingsValue: number; equity: number; pnl: number; lastRunEpoch: number; openOrders: number;
};
type Overview = {
  providers: Provider[]; candidates: { code: string; name: string }[]; firmsPending: string[];
  profiles: Record<Profile, { spreadBps: number; levels: number; assets: number; cycleSeconds: number }>;
};

const PROFILE_LABEL: Record<Profile, string> = { low: "낮음", medium: "중간", high: "높음" };

/** 유동성: companies whose algorithm quotes the order book under their own names. */
export function LiquidityPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [data, setData] = useState<Overview | null>(null);
  const [addCode, setAddCode] = useState("");
  const [addProfile, setAddProfile] = useState<Profile>("medium");

  const load = useCallback(async () => setData(await api<Overview>("/api/admin/liquidity")), []);
  useEffect(() => {
    load().catch((cause) => say(errorText(cause, "유동성 회사를 불러오지 못했습니다."), true));
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load().catch(() => undefined); }, 10_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const act = (body: Record<string, unknown>, fallback: string, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    void run(() => api<{ message: string }>("/api/admin/liquidity", { method: "POST", body: JSON.stringify(body) }), fallback,
      (result) => { say(result.message); void load(); });
  };

  if (!data) return <p className="cap">불러오는 중…</p>;
  const spec = (profile: Profile) => { const item = data.profiles[profile]; return `스프레드 ${(item.spreadBps / 100).toFixed(2)}% · ${item.levels}단계 · ${item.assets}종목 · ${item.cycleSeconds}초`; };
  return <>
    {data.firmsPending.length > 0 && <Card title="유동성 회사 만들기" caption="서로 관계없는 비상장·비주식회사 12곳(낮음·중간·높음 각 4곳)을 만들고 낙원국고에서 회사마다 5,000,000,000 NWX를 지급합니다.">
      <button type="button" className="btn primary" disabled={busy}
        onClick={() => act({ action: "provision" }, "회사를 만들지 못했습니다.", `${data.firmsPending.length}개 회사를 만들고 낙원국고에서 자본을 지급할까요?\n지급은 되돌릴 수 없습니다.`)}>
        회사 {data.firmsPending.length}개 만들기
      </button>
    </Card>}

    <Card title="유동성 공급 회사" caption="선택된 회사가 성향에 따라 자기 이름으로 호가를 올려 거래합니다. 가격 사건이 진행 중이거나 가격이 호가를 지나가면 호가를 거두고, 자본이 시작 자본의 절반 아래로 내려가면 거래를 멈춥니다.">
      <div className="scroll-x"><table>
        <thead><tr><th>회사</th><th>성향</th><th>상태</th><th>현금</th><th>보유 주식</th><th>순자산</th><th>손익</th><th>호가</th><th>마지막 호가</th><th /></tr></thead>
        <tbody>
          {data.providers.map((item) => <tr key={item.companyCode}>
            <td>{item.name} <span className="mono">{item.companyCode}</span></td>
            <td><select aria-label={`${item.name} 유동성 성향`} value={item.profile} disabled={busy} title={spec(item.profile)}
              onChange={(event) => act({ action: "profile", companyCode: item.companyCode, profile: event.target.value }, "성향을 바꾸지 못했습니다.")}>
              {(Object.keys(PROFILE_LABEL) as Profile[]).map((key) => <option key={key} value={key}>{PROFILE_LABEL[key]}</option>)}
            </select></td>
            <td>{item.halted ? <span className="tag danger">자본 부족으로 중지</span> : item.enabled ? <span className="tag ok">공급 중</span> : <span className="tag warn">꺼짐</span>}</td>
            <td>{number(item.cash)}</td><td>{number(item.holdingsValue)}</td><td>{number(item.equity)}</td>
            <td><span className={item.pnl > 0 ? "up" : item.pnl < 0 ? "down" : ""}>{item.pnl > 0 ? "+" : ""}{number(item.pnl)}</span></td>
            <td>{item.openOrders}건</td>
            <td>{item.lastRunEpoch ? date(item.lastRunEpoch) : "—"}</td>
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
          </tr>)}
          {!data.providers.length && <tr><td colSpan={10} className="cap">유동성 공급 회사가 없습니다. 아래에서 회사를 넣어 주세요.</td></tr>}
        </tbody>
      </table></div>
    </Card>

    <Card title="회사 넣기" caption="이미 있는 회사의 자본 계정으로 거래합니다. 넣는 시점의 순자산이 시작 자본이 됩니다.">
      <div className="row" style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label>회사<select value={addCode} onChange={(event) => setAddCode(event.target.value)}>
          <option value="">선택</option>
          {data.candidates.map((item) => <option key={item.code} value={item.code}>{item.name} ({item.code})</option>)}
        </select></label>
        <label>유동성 성향<select value={addProfile} onChange={(event) => setAddProfile(event.target.value as Profile)}>
          {(Object.keys(PROFILE_LABEL) as Profile[]).map((key) => <option key={key} value={key}>{PROFILE_LABEL[key]} · {spec(key)}</option>)}
        </select></label>
        <button type="button" className="btn primary" disabled={busy || !addCode}
          onClick={() => act({ action: "add", companyCode: addCode, profile: addProfile }, "회사를 넣지 못했습니다.")}>넣기</button>
      </div>
    </Card>
  </>;
}
