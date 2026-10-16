import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Field, date, number, plain, show, type Ctx } from "./common";

type Component = { company_code: string; name: string | null; previous_price: number; total_shares: number };
type Exchange = { exchange_id: string; nationality: string; status: "open" | "closed" | "paused"; reason: string; trading_halted: number; circuit_stage: number | null; halt_started_epoch: number | null; resume_epoch: number | null; updated_epoch: number; index_value: number; index_updated_epoch: number; products: number; components: Component[] };
type Result = { message: string; success: boolean };

const STATUS: Record<string, string> = { open: "개장", closed: "휴장", paused: "정지" };

export function ExchangesPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say, query } = ctx;
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [countries, setCountries] = useState<string[]>([]);
  const [selected, setSelected] = useState("");
  const [nationality, setNationality] = useState("");
  const [draft, setDraft] = useState({ id: "", nationality: "" });

  const load = useCallback(async () => {
    const result = await api<{ exchanges: Exchange[]; countries: string[] }>("/api/admin/exchanges");
    setExchanges(result.exchanges);
    setCountries(result.countries);
    setDraft((current) => ({ ...current, nationality: current.nationality || result.countries[0] || "" }));
  }, []);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "거래소를 불러오지 못했습니다.", true)); }, [load]);

  const exchange = exchanges.find((item) => item.exchange_id === selected);
  useEffect(() => { if (exchange) setNationality(exchange.nationality); }, [exchange]);
  const visible = exchanges.filter((item) => !query.trim() || `${item.exchange_id} ${item.nationality}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));

  function act(id: string, body: Record<string, unknown>, question: string) {
    if (!window.confirm(question)) return;
    void run(() => api<Result>(`/api/admin/exchanges/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify(body) }),
      "거래소 설정을 바꾸지 못했습니다.", async (result) => { say(plain(result.message), !result.success); if (body.action === "delete") setSelected(""); await load(); });
  }

  const control = (item: Exchange, action: string, label: string, stage?: number) =>
    act(item.exchange_id, { action, stage }, `${item.exchange_id} 거래소에 ${label}을(를) 적용할까요?`);
  const all = (action: string, label: string) =>
    act("ALL", { action }, `모든 거래소에 ${label}을(를) 적용할까요?`);

  return <>
    <Card title="전체 거래소" caption="모든 거래소에 한 번에 적용합니다 ($휴장·$개장·$장일시정지·$장재개).">
      <div className="actions"><button className="btn primary small" disabled={busy} onClick={() => all("open", "개장")}>전체 개장</button><button className="soft" disabled={busy} onClick={() => all("close", "휴장")}>전체 휴장</button><button className="soft" disabled={busy} onClick={() => all("pause", "일시 정지")}>전체 일시 정지</button><button className="soft" disabled={busy} onClick={() => all("resume", "재개")}>전체 재개</button></div>
    </Card>

    {exchange && <Card title={`${exchange.exchange_id} 거래소`} caption={<>{STATUS[exchange.status] ?? exchange.status}{exchange.reason ? ` · ${exchange.reason}` : ""}{exchange.resume_epoch ? ` · 자동 재개 ${date(exchange.resume_epoch)}` : ""} · 지수 {number(exchange.index_value)} · 활성 상품 {number(exchange.products)}개</>}
      actions={<><button className="soft danger" type="button" disabled={busy || exchange.exchange_id === "CURRENCY"} onClick={() => act(exchange.exchange_id, { action: "delete" }, `${exchange.exchange_id} 거래소를 삭제할까요? 상장·연동 상품이 있으면 거부됩니다.`)}>거래소 삭제</button><button className="soft" type="button" onClick={() => setSelected("")}>닫기</button></>}>
      <h3 className="sub-head">거래 상태</h3>
      <div className="actions">
        <button className="btn primary small" disabled={busy} onClick={() => control(exchange, "open", "개장")}>개장</button>
        <button className="soft" disabled={busy} onClick={() => control(exchange, "close", "휴장")}>휴장</button>
        <button className="soft" disabled={busy} onClick={() => control(exchange, "pause", "일시 정지")}>일시 정지</button>
        <button className="soft" disabled={busy} onClick={() => control(exchange, "resume", "재개")}>재개</button>
        <span className="divider" aria-hidden="true" />
        {[1, 2, 3].map((stage) => <button key={stage} className="soft" disabled={busy} onClick={() => control(exchange, "circuit", `서킷브레이커 ${stage}단계`, stage)}>서킷 {stage}단계</button>)}
        <button className="soft" disabled={busy} onClick={() => control(exchange, "sidecar", "사이드카(5분)")}>사이드카</button>
      </div>
      <p className="hint">서킷 1·2단계는 20분, 3단계는 다음 날 0시(한국시간)까지 정지합니다. 사이드카는 5분입니다. 휴장 중에는 서킷·사이드카를 걸 수 없습니다.</p>
      <h3 className="sub-head">소속 국가</h3>
      <div className="form-grid">
        <Field label="국가" hint="금리·세율·국채 기준이 됩니다"><select value={nationality} onChange={(event) => setNationality(event.target.value)}>{countries.map((country) => <option key={country} value={country}>{country}</option>)}</select></Field>
        <button className="btn primary" type="button" disabled={busy || nationality === exchange.nationality} onClick={() => act(exchange.exchange_id, { action: "nationality", nationality }, `${exchange.exchange_id} 거래소 국가를 ${nationality}(으)로 바꿀까요?`)}>국가 저장</button>
      </div>
      <h3 className="sub-head">지수 구성 종목</h3>
      <div className="scroll-x"><table><thead><tr><th>종목</th><th>이름</th><th className="r">기준 가격</th><th className="r">주식 수</th></tr></thead><tbody>{exchange.components.map((item) => <tr key={item.company_code}><td className="mono">{item.company_code}</td><td>{show(item.name)}</td><td className="r mono">{number(item.previous_price)}</td><td className="r mono">{number(item.total_shares)}</td></tr>)}</tbody></table>{!exchange.components.length && <p className="empty">지수 구성 종목이 없습니다. 이 거래소에 회사 주식을 상장하면 자동으로 포함됩니다.</p>}</div>
    </Card>}

    <Card title="새 거래소" caption="거래소를 만든 뒤 상품 생성 시 거래소ID로 지정하세요.">
      <form className="form-grid" onSubmit={(event) => { event.preventDefault(); act(draft.id.trim(), { action: "create", nationality: draft.nationality }, `${draft.id} 거래소를 ${draft.nationality} 소속으로 만들까요?`); }}>
        <Field label="거래소 ID" hint="공백 없이"><input className="mono" value={draft.id} required onChange={(event) => setDraft({ ...draft, id: event.target.value.toUpperCase() })} /></Field>
        <Field label="국가"><select value={draft.nationality} onChange={(event) => setDraft({ ...draft, nationality: event.target.value })}>{countries.map((country) => <option key={country} value={country}>{country}</option>)}</select></Field>
        <button className="btn primary" disabled={busy || !draft.id.trim()}>거래소 만들기</button>
      </form>
    </Card>

    <Card title="거래소" caption="행을 누르면 거래 상태·국가·지수를 설정합니다.">
      <div className="scroll-x tall"><table><thead><tr><th>거래소</th><th>국가</th><th>상태</th><th>사유</th><th>자동 재개</th><th className="r">지수</th><th className="r">상품</th><th className="r">지수 종목</th></tr></thead><tbody>
        {visible.map((item) => <tr key={item.exchange_id} className={item.exchange_id === selected ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => setSelected(item.exchange_id)} onKeyDown={(event) => { if (event.key === "Enter") setSelected(item.exchange_id); }}>
          <td className="mono">{item.exchange_id}</td><td>{item.nationality}</td>
          <td><span className={item.status === "open" && !item.trading_halted ? "tag ok" : "tag warn"}>{item.circuit_stage ? `서킷 ${item.circuit_stage}단계` : STATUS[item.status] ?? item.status}</span></td>
          <td>{item.reason || "—"}</td><td>{item.resume_epoch ? date(item.resume_epoch) : "—"}</td>
          <td className="r mono">{number(item.index_value)}</td><td className="r">{number(item.products)}</td><td className="r">{number(item.components.length)}</td>
        </tr>)}
      </tbody></table>{!visible.length && <p className="empty">거래소가 없습니다.</p>}</div>
    </Card>
  </>;
}
