import { useCallback, useEffect, useState } from "react";
import type { Money } from "../../shared/money";
import { api } from "../../shared/api";
import { Card, Field, date, number, plain, show, type Ctx } from "./common";

type Component = { company_code: string; name: string | null; previous_price: number; total_shares: number };
type Listing = { code: string; name: string | null; kind: string; share_class: "common" | "preferred" | null; quote_currency: string | null; status: string };
type CurrencyPair = { code: string; name: string; price: Money; quote_currency: string | null; status: string };
type Exchange = { exchange_id: string; nationality: string; operator_company_code?: string | null; operator_name?: string | null; status: "open" | "closed" | "paused"; reason: string; trading_halted: number; circuit_stage: number | null; halt_started_epoch: number | null; resume_epoch: number | null; updated_epoch: number; hours: Schedule; hours_label: string; index_value: number; index_updated_epoch: number; products: number; listings: Listing[]; components: Component[]; currency_pairs?: CurrencyPair[] };
type Result = { message: string; success: boolean };

const STATUS: Record<string, string> = { open: "개장", closed: "휴장", paused: "정지" };
const WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"];
// The server parses and labels the stored hours (functions/trading_hours.py); empty times mean 24 hours.
type Schedule = { enabled: boolean; open: string; close: string; days: number[]; holidays: string[] };
type Hours = { open: string; close: string; days: number[]; holidays: string };
const hoursOf = ({ hours }: Exchange): Hours => ({ open: hours.open, close: hours.close, days: hours.days, holidays: hours.holidays.join("\n") });

export function ExchangesPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say, query } = ctx;
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [countries, setCountries] = useState<string[]>([]);
  const [selected, setSelected] = useState("");
  const [nationality, setNationality] = useState("");
  const [draft, setDraft] = useState({ id: "", nationality: "" });
  const [hours, setHours] = useState<Hours>({ open: "", close: "", days: [1, 2, 3, 4, 5], holidays: "" });

  const load = useCallback(async () => {
    const result = await api<{ exchanges: Exchange[]; countries: string[] }>("/api/admin/exchanges");
    setExchanges(result.exchanges);
    setCountries(result.countries);
    setDraft((current) => ({ ...current, nationality: current.nationality || result.countries[0] || "" }));
  }, []);
  useEffect(() => { load().catch((cause) => say(cause instanceof Error ? cause.message : "거래소를 불러오지 못했습니다.", true)); }, [load]);

  const exchange = exchanges.find((item) => item.exchange_id === selected);
  useEffect(() => { if (exchange) { setNationality(exchange.nationality); setHours(hoursOf(exchange)); } }, [exchange]);
  const visible = exchanges.filter((item) => !query.trim() || `${item.exchange_id} ${item.nationality}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));

  function act(id: string, body: Record<string, unknown>, question: string) {
    if (!window.confirm(question)) return;
    void run(() => api<Result>(`/api/admin/exchanges/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify(body) }),
      "거래소 설정을 바꾸지 못했습니다.", async (result) => { say(plain(result.message), !result.success); if (body.action === "delete") setSelected(""); await load(); });
  }

  const control = (item: Exchange, action: string, label: string, stage?: number) =>
    act(item.exchange_id, { action, stage }, `${item.exchange_id} 거래소에 ${label}을(를) 적용할까요?`);
  const all = (action: string, label: string, stage?: number) =>
    act("ALL", { action, stage }, `모든 일반 거래소에 ${label}을(를) 적용할까요? (CURRENCY 제외)`);

  return <>
    <Card title="일반 거래소 일괄 제어" caption="개장·휴장·정지·서킷·사이드카를 모든 일반 거래소에 적용합니다. 시스템 거래소 CURRENCY는 항상 운영되며 여기서 제외됩니다.">
      <div className="actions"><button className="btn primary small" disabled={busy} onClick={() => all("open", "개장")}>전체 개장</button><button className="soft" disabled={busy} onClick={() => all("close", "휴장")}>전체 휴장</button><button className="soft" disabled={busy} onClick={() => all("pause", "일시 정지")}>전체 일시 정지</button><button className="soft" disabled={busy} onClick={() => all("resume", "재개")}>전체 재개</button><span className="divider" aria-hidden="true" />{[1, 2, 3].map((stage) => <button key={stage} className="soft" disabled={busy} onClick={() => all("circuit", `서킷브레이커 ${stage}단계`, stage)}>전체 서킷 {stage}단계</button>)}<button className="soft" disabled={busy} onClick={() => all("sidecar", "사이드카(5분)")}>전체 사이드카</button></div>
    </Card>

    {exchange?.exchange_id === "CURRENCY" && <Card title="CURRENCY · 외환 시스템" caption="환전과 통화쌍 시세를 위한 시스템 전용 거래소입니다. 시장 상태·거래시간·소속 국가는 자동 관리됩니다."
      actions={<><span className="tag ok">시스템 운영</span><button className="soft" type="button" onClick={() => setSelected("")}>닫기</button></>}>
      <div className="stats">
        <div><small>기능</small><b>환전 · 통화쌍 시세</b></div>
        <div><small>운영 시간</small><b>항상 운영</b></div>
        <div><small>활성 통화쌍</small><b>{number(exchange.currency_pairs?.length ?? 0)}개</b></div>
        <div><small>활성 시스템 상품</small><b>{number(exchange.products)}개</b></div>
      </div>
      <p className="hint">환율과 통화쌍은 통화 시스템이 관리합니다. CURRENCY는 개장·휴장·정지·서킷·사이드카·거래시간·국가 변경 대상이 아닙니다.</p>
      <h3 className="sub-head">통화쌍</h3>
      <div className="scroll-x"><table><thead><tr><th>코드</th><th>통화쌍</th><th className="r">현재 환율</th><th>기준 통화</th></tr></thead><tbody>{(exchange.currency_pairs ?? []).map((pair) => <tr key={pair.code}><td className="mono">{pair.code}</td><td>{pair.name}</td><td className="r mono">{number(pair.price)}</td><td className="mono">{pair.quote_currency || "—"}</td></tr>)}</tbody></table>{!exchange.currency_pairs?.length && <p className="empty">활성 통화쌍이 없습니다.</p>}</div>
    </Card>}

    {exchange && exchange.exchange_id !== "CURRENCY" && <Card title={`${exchange.exchange_id} 거래소`} caption={<>{STATUS[exchange.status] ?? exchange.status}{exchange.reason ? ` · ${exchange.reason}` : ""}{exchange.resume_epoch ? ` · 자동 재개 ${date(exchange.resume_epoch)}` : ""} · 지수 {number(exchange.index_value)} · 활성 상품 {number(exchange.products)}개 · 운영사 {exchange.operator_name ? `${exchange.operator_name} (${exchange.operator_company_code})` : "미지정"}</>}
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
      <h3 className="sub-head">거래 시간</h3>
      <form className="form-grid hours-form" onSubmit={(event) => { event.preventDefault(); act(exchange.exchange_id, { action: "hours", hours: { open: hours.open, close: hours.close, days: hours.days, holidays: hours.holidays.split(/[\s,]+/).filter(Boolean) } }, `${exchange.exchange_id} 거래 시간을 ${hours.open && hours.close ? `${hours.open}~${hours.close}` : "24시간"}(으)로 저장할까요?`); }}>
        <Field label="개장 시각" hint="24시간제 HH:MM · 비우면 24시간"><input className="mono" inputMode="numeric" maxLength={5} placeholder="09:00" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" title="24시간제 HH:MM (예: 09:00)" value={hours.open} onChange={(event) => setHours({ ...hours, open: event.target.value.replace(/[^0-9:]/g, "") })} /></Field>
        <Field label="폐장 시각" hint="24시간제 HH:MM · 비우면 24시간"><input className="mono" inputMode="numeric" maxLength={5} placeholder="15:30" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" title="24시간제 HH:MM (예: 15:30)" value={hours.close} onChange={(event) => setHours({ ...hours, close: event.target.value.replace(/[^0-9:]/g, "") })} /></Field>
        <div className="hours-days span-wide" role="group" aria-label="거래 요일"><span>거래 요일</span><div className="segment-tabs">{WEEKDAYS.map((label, index) => <button key={label} type="button" className={hours.days.includes(index + 1) ? "active" : undefined} aria-pressed={hours.days.includes(index + 1)} disabled={!hours.open && !hours.close} onClick={() => setHours({ ...hours, days: hours.days.includes(index + 1) ? hours.days.filter((day) => day !== index + 1) : [...hours.days, index + 1].sort() })}>{label}</button>)}</div></div>
        <Field label="휴장일" hint="YYYY-MM-DD, 줄마다 하나" wide><textarea rows={3} className="mono" value={hours.holidays} disabled={!hours.open && !hours.close} onChange={(event) => setHours({ ...hours, holidays: event.target.value })} /></Field>
        <div className="actions span-wide"><button className="btn primary" disabled={busy}>거래 시간 저장</button><button className="soft" type="button" disabled={busy} onClick={() => setHours({ ...hours, open: "", close: "" })}>24시간으로</button></div>
      </form>
      <p className="hint">한국시간 기준입니다. 폐장 시각이 되면 자동으로 장 마감(휴장)되고 다음 거래일 개장 시각에 자동 개장합니다. 장 마감 중에는 시세가 멈추고 신규 주문을 받지 않습니다. 개장·폐장 시각을 둘 다 비우면 24시간 개장합니다.</p>
      <h3 className="sub-head">소속 국가</h3>
      <div className="form-grid">
        <Field label="국가" hint="금리·세율·국채 기준이 됩니다"><select value={nationality} onChange={(event) => setNationality(event.target.value)}>{countries.map((country) => <option key={country} value={country}>{country}</option>)}</select></Field>
        <button className="btn primary" type="button" disabled={busy || nationality === exchange.nationality} onClick={() => act(exchange.exchange_id, { action: "nationality", nationality }, `${exchange.exchange_id} 거래소 국가를 ${nationality}(으)로 바꿀까요?`)}>국가 저장</button>
      </div>
      <h3 className="sub-head">상장 상품 전체</h3>
      <div className="scroll-x"><table><thead><tr><th>코드</th><th>이름</th><th>상품 종류</th><th>주식 클래스</th><th>통화</th></tr></thead><tbody>{exchange.listings.map((item) => <tr key={`${item.kind}:${item.code}`}><td className="mono">{item.code}</td><td>{show(item.name)}</td><td>{item.kind}</td><td>{item.share_class === "preferred" ? "우선주" : item.share_class === "common" ? "보통주" : "—"}</td><td className="mono">{item.quote_currency || "—"}</td></tr>)}</tbody></table>{!exchange.listings.length && <p className="empty">활성 상장 상품이 없습니다.</p>}</div>
      <h3 className="sub-head">지수 구성 종목</h3>
      <div className="scroll-x"><table><thead><tr><th>종목</th><th>이름</th><th className="r">기준 가격</th><th className="r">주식 수</th></tr></thead><tbody>{exchange.components.map((item) => <tr key={item.company_code}><td className="mono">{item.company_code}</td><td>{show(item.name)}</td><td className="r mono">{number(item.previous_price)}</td><td className="r mono">{number(item.total_shares)}</td></tr>)}</tbody></table>{!exchange.components.length && <p className="empty">지수 구성 종목이 없습니다. 이 거래소에 회사 주식을 상장하면 자동으로 포함됩니다.</p>}</div>
    </Card>}

    <Card title="새 거래소" caption="거래소를 만든 뒤 상품 생성 시 거래소ID로 지정하세요. CURRENCY는 시스템 전용 ID입니다.">
      <form className="form-grid" onSubmit={(event) => { event.preventDefault(); act(draft.id.trim(), { action: "create", nationality: draft.nationality }, `${draft.id} 거래소를 ${draft.nationality} 소속으로 만들까요?`); }}>
        <Field label="거래소 ID" hint="공백 없이"><input className="mono" value={draft.id} required onChange={(event) => setDraft({ ...draft, id: event.target.value.toUpperCase() })} /></Field>
        <Field label="국가"><select value={draft.nationality} onChange={(event) => setDraft({ ...draft, nationality: event.target.value })}>{countries.map((country) => <option key={country} value={country}>{country}</option>)}</select></Field>
        <button className="btn primary" disabled={busy || !draft.id.trim()}>거래소 만들기</button>
      </form>
    </Card>

    <Card title="거래소" caption="일반 거래소 행을 눌러 거래 상태·국가·지수를 설정합니다. CURRENCY는 환전 시스템 전용으로 상태를 자동 운영합니다.">
      <div className="scroll-x tall"><table><thead><tr><th>거래소</th><th>국가</th><th>상태</th><th>거래 시간</th><th>사유</th><th>자동 재개</th><th className="r">지수</th><th className="r">상품</th><th className="r">지수 종목</th><th /></tr></thead><tbody>
        {visible.map((item) => <tr key={item.exchange_id} className={item.exchange_id === selected ? "clickable selected" : "clickable"} tabIndex={0} onClick={() => setSelected(item.exchange_id)} onKeyDown={(event) => { if (event.key === "Enter") setSelected(item.exchange_id); }}>
          <td className="mono">{item.exchange_id}{item.exchange_id === "CURRENCY" && <span className="tag inline" title="환전과 통화쌍 시세(CUR_…)를 처리하는 시스템 전용 거래소입니다.">시스템</span>}</td><td>{item.exchange_id === "CURRENCY" ? "—" : item.nationality}</td>
          <td><span className={item.exchange_id === "CURRENCY" || (item.status === "open" && !item.trading_halted) ? "tag ok" : "tag warn"}>{item.exchange_id === "CURRENCY" ? "시스템 운영" : item.circuit_stage ? `서킷 ${item.circuit_stage}단계` : STATUS[item.status] ?? item.status}</span></td>
          <td className="mono">{item.exchange_id === "CURRENCY" ? "24시간" : item.hours_label}</td><td>{item.exchange_id === "CURRENCY" ? "자동 관리" : item.reason || "—"}</td><td>{item.exchange_id === "CURRENCY" ? "—" : item.resume_epoch ? date(item.resume_epoch) : "—"}</td>
          <td className="r mono">{item.exchange_id === "CURRENCY" ? "—" : number(item.index_value)}</td><td className="r">{item.exchange_id === "CURRENCY" ? number(item.currency_pairs?.length ?? 0) : number(item.products)}</td><td className="r">{item.exchange_id === "CURRENCY" ? "—" : number(item.components.length)}</td>
          <td className="row-actions"><button className="soft small danger" disabled={busy || item.exchange_id === "CURRENCY"} title={item.products ? "상장 상품이 있으면 서버가 이유와 함께 거부합니다" : "거래소 삭제"} onClick={(event) => { event.stopPropagation(); act(item.exchange_id, { action: "delete" }, `${item.exchange_id} 거래소를 삭제할까요? 상장·연동된 상품이 있으면 거부됩니다.`); }}>삭제</button></td>
        </tr>)}
      </tbody></table>{!visible.length && <p className="empty">거래소가 없습니다.</p>}</div>
    </Card>
  </>;
}
