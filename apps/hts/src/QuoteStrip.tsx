import { WarningBadge, boardUrl, instrumentType, money } from "./appShared";
import type { Instrument } from "./types";
import type { Money } from "../../shared/money";

type Props = {
  watch: string[];
  instruments: Instrument[];
  selected: Instrument | null;
  selectedCode: string;
  current: Money;
  homeCurrency: string;
  onToggleWatch: (code: string) => void;
  onChoose: (code: string) => void;
  onSwitchInstrument: () => void;
};

/** Watchlist chips and the selected instrument's name, DR/bond notes and latest price. */
export function QuoteStrip({ watch, instruments, selected, selectedCode, current, homeCurrency, onToggleWatch, onChoose, onSwitchInstrument }: Props) {
  return <>
{watch.length > 0 && <nav className="watch-strip" aria-label="관심종목">{watch.map((code) => instruments.find((item) => item.code === code)).filter((item): item is Instrument => Boolean(item)).map((item) => <button type="button" key={item.code} className={item.code === selectedCode ? "active" : ""} onClick={() => onChoose(item.code)}>
  <b>{item.name}</b><span>{money(item.price, 2)}</span>{item.change24h != null && <small className={item.change24h > 0 ? "up" : item.change24h < 0 ? "down" : undefined}>{item.change24h > 0 ? "+" : ""}{Number(item.change24h).toFixed(2)}%</small>}
</button>)}</nav>}
<div className="quote-strip"><div><p className="eyebrow">{selected?.exchange_id || "MARKET"} · {selected ? instrumentType(selected) : "—"}</p><h1>{selected?.name || "종목을 선택하세요"}<small>{selected?.code}</small>{selected && <button type="button" className={watch.includes(selected.code) ? "watch-star on" : "watch-star"} aria-pressed={watch.includes(selected.code)} title={watch.includes(selected.code) ? "관심종목에서 빼기" : "관심종목에 추가"} onClick={() => onToggleWatch(selected.code)}>{watch.includes(selected.code) ? "★" : "☆"}</button>}<WarningBadge warning={selected?.warning} /></h1>{selected?.drProgram && <><p className="orders-hint">{selected.drProgram.receiptType} · 원주 {selected.drProgram.underlyingCode} · 1 DR = {money(selected.drProgram.underlyingPerReceipt, 6)}주 · 원주 거래소 {selected.drProgram.homeExchangeId || "—"} · 예탁 {selected.drProgram.depositaryBank || "—"} · 수탁 {selected.drProgram.custodian || "—"}</p>{selected.drProgram.referenceValue !== undefined && <p className="orders-hint">단순 원주 환산 참고가 {money(selected.drProgram.referenceValue, 4)} {selected.quote_currency || ""}{selected.drProgram.referencePremiumPercent !== undefined && ` · 현재가 괴리 ${selected.drProgram.referencePremiumPercent >= 0 ? "+" : ""}${Number(selected.drProgram.referencePremiumPercent).toFixed(2)}%`}{selected.drProgram.referenceDeviationAlert && " · ⚠ 괴리 주의 (±10% 이상)"} · 수수료·세금·유동성 미반영</p>}</>}{selected?.bondRisk && <p className="orders-hint">액면 {money(selected.bondRisk.faceValue)} 기준 · Macaulay {Number(selected.bondRisk.macaulay_duration).toFixed(3)}년 · 수정듀레이션 {Number(selected.bondRisk.modified_duration).toFixed(3)} · 컨벡서티 {Number(selected.bondRisk.convexity).toFixed(3)} · DV01 {money(selected.bondRisk.dv01, 4)} {selected.quote_currency || homeCurrency} / 1bp. 현 국채 곡선의 평행이동 민감도이며 거래가격 예측이나 규제지표가 아닙니다.</p>}{selected?.warning?.reason && <p className="warn-reason">⚠️ {selected.warning.label} · {selected.warning.reason}</p>}</div>
  <div className="quote-side"><div className="quote-value"><strong>{money(current)}</strong><span>{selected?.quote_currency || homeCurrency}</span>{selected && (selected.change24h != null
    ? <em className={`quote-change ${selected.change24h > 0 ? "up" : selected.change24h < 0 ? "down" : ""}`} title="최근 24시간 등락률">{selected.change24h > 0 ? "▲" : selected.change24h < 0 ? "▼" : ""} {selected.change24h > 0 ? "+" : ""}{Number(selected.change24h).toFixed(2)}%</em>
    : <em className="quote-change" title="최근 24시간 거래가 없어 등락률이 없습니다">—</em>)}</div><button className="quote-switch" onClick={onSwitchInstrument}>종목 바꾸기</button>{selected && <a className="quote-switch" href={boardUrl(selected.code)}>종목 토론</a>}</div></div>
</>;
}
