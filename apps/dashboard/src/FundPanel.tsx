import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Field, number, plain, type Ctx } from "./common";

type Fund = {
  code: string; mode: "synthetic" | "backed"; availableCash?: number; reservedCash?: number;
  netAssets?: number; issuedUnits?: number; nav?: number; ledgerBalanced?: boolean; conversionCash?: number; receivables?: number;
  positions?: { code: string; quantity: number; price: number; value: number }[];
  orders: { order_id: number; asset_code: string; side: string; remaining_quantity: number; reserved_cash: number }[];
  ledger: { entry_id: number; event: string; amount: number; balance: number; reference: string | null }[];
};

export function FundPanel({ code, ctx, close }: { code: string; ctx: Ctx; close: () => void }) {
  const [fund, setFund] = useState<Fund | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [asset, setAsset] = useState("");
  const [side, setSide] = useState("buy");
  const [quantity, setQuantity] = useState("1");
  const [price, setPrice] = useState("");
  const path = `/api/admin/funds/${encodeURIComponent(code)}`;
  useEffect(() => {
    let cancelled = false;
    setError("");
    api<Fund>(path).then((value) => { if (!cancelled) setFund(value); })
      .catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause.message : "펀드 정보를 불러오지 못했습니다."); });
    return () => { cancelled = true; };
  }, [path, revision]);

  function act(body: Record<string, unknown>) {
    void ctx.run(() => api<{ success: boolean; message: string; fund: Fund }>(path, {
      method: "POST", body: JSON.stringify(body),
    }), "펀드 작업을 처리하지 못했습니다.", (result) => {
      ctx.say(plain(result.message), !result.success); setFund(result.fund);
    });
  }

  return <Card title={`${code} · 펀드 운용`} caption="시민 계정이 아닌 펀드 전용 계정입니다. 구성자산 매수는 자동이 아니며, 아래 주문으로 운용합니다. 금액은 NWX입니다."
    actions={<><button className="soft" disabled={ctx.busy} onClick={() => setRevision((value) => value + 1)}>새로고침</button><button className="soft" onClick={close}>닫기</button></>}>
    {error && <p role="alert">{error}</p>}
    {!fund ? <p>불러오는 중…</p> : fund.mode === "synthetic" ? <>
      <p>기존 가격추종형 펀드입니다. 기존 {number(fund.issuedUnits ?? 0)}좌를 유지하려면 국고에서 {number(fund.conversionCash ?? 0)} NWX를 펀드로 이전해야 합니다. 전환 후 구성자산은 별도로 매수하세요.</p>
      <button disabled={ctx.busy} onClick={() => { if (window.confirm(`국고에서 ${number(fund.conversionCash ?? 0)} NWX를 이전하고 실보유형으로 전환할까요? 이후 가입금은 펀드 계정에 적립됩니다. 되돌리는 자동 기능은 없습니다.`)) act({ action: "activate", expectedFunding: fund.conversionCash ?? 0 }); }}>자금 이전 및 펀드 계정 개설</button>
    </> : <>
      <p>사용 가능 현금 {number(fund.availableCash ?? 0)} · 주문 예약금 {number(fund.reservedCash ?? 0)} · 미수배당 {number(fund.receivables ?? 0)} · 순자산 {number(fund.netAssets ?? 0)} · 발행좌수 {number(fund.issuedUnits ?? 0)} · NAV {number(fund.nav ?? 0)}</p>
      <p className="hint">원장 대사: {fund.ledgerBalanced ? "일치" : "불일치 — 거래 중단 필요"}. 현금이 부족한 환매는 거절되며, 매도 체결 또는 매수 주문 취소 후 다시 요청해야 합니다.</p>
      <form className="form-grid" onSubmit={(event) => {
        event.preventDefault();
        if (window.confirm(`${code}의 자금으로 ${asset} ${quantity}개를 ${price} NWX 지정가에 ${side === "buy" ? "매수" : "매도"}할까요?`)) {
          act({ action: "order", assetCode: asset.trim(), side, quantity, price: Number(price) });
        }
      }}>
        <Field label="구성자산 코드"><input required value={asset} onChange={(event) => setAsset(event.target.value)} /></Field>
        <Field label="방향"><select value={side} onChange={(event) => setSide(event.target.value)}><option value="buy">매수</option><option value="sell">매도</option></select></Field>
        <Field label="수량"><input required type="number" min="0.001" step="0.001" value={quantity} onChange={(event) => setQuantity(event.target.value)} /></Field>
        <Field label="지정가 NWX"><input required type="number" min="1" step="1" value={price} onChange={(event) => setPrice(event.target.value)} /></Field>
        <button disabled={ctx.busy || !fund.ledgerBalanced} type="submit">펀드 명의 주문</button>
      </form>
      <h3>실제 보유자산</h3>
      <div className="scroll-x"><table><thead><tr><th>코드</th><th>수량</th><th>가격</th><th>평가액</th></tr></thead><tbody>
        {fund.positions?.map((item) => <tr key={item.code}><td>{item.code}</td><td>{number(item.quantity)}</td><td>{number(item.price)}</td><td>{number(item.value)}</td></tr>)}
      </tbody></table></div>
      <h3>미체결 주문</h3>
      {fund.orders.map((order) => <p key={order.order_id}>#{order.order_id} {order.asset_code} {order.side === "buy" ? "매수" : "매도"} · 잔여 {number(order.remaining_quantity)} · 예약금 {number(order.reserved_cash)} <button className="soft small" disabled={ctx.busy} onClick={() => act({ action: "cancel", orderId: order.order_id })}>취소</button></p>)}
      <h3>현금 원장 · 최근 100건</h3>
      <p className="hint">잔액은 사용 가능 현금입니다. 주문 예약금은 별도 자산으로 NAV에 포함됩니다.</p>
      <div className="scroll-x"><table><thead><tr><th>번호</th><th>구분</th><th>증감</th><th>잔액</th><th>참조</th></tr></thead><tbody>
        {fund.ledger.map((entry) => <tr key={entry.entry_id}><td>{entry.entry_id}</td><td>{entry.event}</td><td>{number(entry.amount)}</td><td>{number(entry.balance)}</td><td>{entry.reference ?? "—"}</td></tr>)}
      </tbody></table></div>
    </>}
  </Card>;
}
