import { lazy } from "react";
import type { Audit, Overview } from "./appTypes";
import { Card, date, number, type Ctx } from "./common";
const LoginHistoryCard = lazy(() => import("./LoginHistoryCard").then((module) => ({ default: module.LoginHistoryCard })));

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="spec"><span className="spec-value">{value}</span><span className="spec-label">{label}</span></div>;
}

export function OverviewSection({ ctx, overview, audit }: { ctx: Ctx; overview: Overview | null; audit: Audit[] }) {
  return <>
    <section className="spec-strip" aria-label="주요 지표">
      <Metric label="활성 사용자" value={overview ? number(overview.activeUsers) : "—"} />
      <Metric label="거래 중 종목·상품" value={overview ? number(overview.activeInstruments) : "—"} />
      <Metric label="미체결 주문" value={overview ? number(overview.openOrders) : "—"} />
    </section>
    {!!overview?.bankDeficits?.length && <section className="glass card mb rise warn-card" role="alert"><div className="card-head"><div><h2>⚠️ 은행 본사 계좌 적자</h2><p className="cap">적자는 허용되지만 자본 확충을 검토하세요. 처음 적자가 될 때 관리자에게 Discord DM으로도 알립니다.</p></div></div>
      <div className="scroll-x"><table><thead><tr><th>은행</th><th>본사 계좌</th><th>국가</th><th className="r">잔액</th></tr></thead><tbody>{overview.bankDeficits.map((bank) => <tr key={bank.issuer}><td>{bank.issuer}</td><td className="mono">{bank.account}</td><td>{bank.nationality}</td><td className="r mono">{number(bank.cash)}</td></tr>)}</tbody></table></div></section>}
    {!!overview?.priceFloor?.actions.length && <section className="glass card mb rise warn-card" role="alert"><div className="card-head"><div><h2>📈 주가 하한({number(overview.priceFloor.floor)}원) 조치</h2><p className="cap">최근 7일 동안 {number(overview.priceFloor.floor)}원 미만이어서 하한까지 올린 종목입니다. 하한 근처에서는 가격을 조금씩 위로 받치고, 하한 아래로는 내려가지 않습니다. 시스템 설정 price_floor_won으로 바꾸거나(0이면 끔) 자본금·발행주식수를 조정해 근본 가격을 바로잡을 수 있습니다.</p></div></div>
      <div className="scroll-x"><table><thead><tr><th>시각</th><th>종목</th><th className="r">조정 전</th><th className="r">조정 후</th></tr></thead><tbody>{overview.priceFloor.actions.map((item) => <tr key={`${item.code}-${item.epoch}`}><td>{date(item.epoch)}</td><td>{item.name} <span className="mono hint">{item.code}</span></td><td className="r mono">{number(item.from)}</td><td className="r mono">{number(item.to)}</td></tr>)}</tbody></table></div></section>}
    <Card title="작업 기록" caption="최근 50건 · 작업 기록은 append-only이며 수정·삭제할 수 없습니다.">
      <div className="scroll-x tall"><table><thead><tr><th>시각</th><th>관리자 ID</th><th>작업</th><th>세부 내용</th></tr></thead><tbody>{audit.map((entry) => <tr key={entry.auditId}>
        <td>{date(entry.time)}</td><td className="mono">{entry.actorId}</td><td><span className="tag">{entry.action}</span></td><td className="audit-payload mono" title={JSON.stringify(entry.payload)}>{JSON.stringify(entry.payload)}</td>
      </tr>)}</tbody></table>{!audit.length && <p className="empty">기록이 없습니다.</p>}</div>
    </Card>
    <LoginHistoryCard ctx={ctx} />
  </>;
}
