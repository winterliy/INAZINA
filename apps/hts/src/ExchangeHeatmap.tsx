import { useEffect, useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";

type ExchangeInstrument = { code?: string; name?: string; type?: string; exchange_id?: string | null; change24h?: number | null; volume24h?: number | null };
export type ExchangeSummary = { exchangeId: string; count: number; averageChange: number };

export type HeatmapInstrument = ExchangeInstrument & { code: string; name: string; change24h: number | null; volume24h: number };

export function summarizeExchanges(instruments: ExchangeInstrument[]): ExchangeSummary[] {
  const groups = new Map<string, { count: number; total: number; changed: number }>();
  for (const item of instruments) {
    const exchangeId = item.exchange_id?.trim();
    if (!exchangeId || exchangeId.toUpperCase() === "CURRENCY") continue;
    const group = groups.get(exchangeId) ?? { count: 0, total: 0, changed: 0 };
    group.count += 1;
    if (item.change24h !== null && item.change24h !== undefined && Number.isFinite(item.change24h)) {
      group.total += item.change24h;
      group.changed += 1;
    }
    groups.set(exchangeId, group);
  }
  return [...groups.entries()]
    .map(([exchangeId, group]) => ({ exchangeId, count: group.count, averageChange: group.changed ? group.total / group.changed : 0 }))
    .sort((a, b) => b.count - a.count || a.exchangeId.localeCompare(b.exchangeId));
}

export function exchangeStocks(instruments: ExchangeInstrument[], exchangeId: string): HeatmapInstrument[] {
  return instruments.filter((item): item is HeatmapInstrument =>
    item.exchange_id === exchangeId && (item.type === "stock" || item.type === "preferred") && Boolean(item.code && item.name)
  ).map((item) => ({ ...item, code: item.code!, name: item.name!, change24h: item.change24h ?? null, volume24h: item.volume24h ?? 0 }))
    .sort((a, b) => b.volume24h - a.volume24h || a.code.localeCompare(b.code));
}

export const tone = (change: number) => change > 0 ? "positive" : change < 0 ? "negative" : "neutral";
export const formatChange = (change: number) => `${change > 0 ? "+" : ""}${change.toFixed(2)}%`;

export function ExchangeHeatmap({ instruments, exchangeId, onExchangeChange, onSelect, full = false }: { instruments: ExchangeInstrument[]; exchangeId?: string; onExchangeChange?: (exchangeId: string) => void; onSelect?: (code: string) => void; full?: boolean }): ReactNode {
  const exchanges = useMemo(() => summarizeExchanges(instruments).map((item) => item.exchangeId), [instruments]);
  const [query, setQuery] = useState(exchangeId ?? "");
  useEffect(() => setQuery(exchangeId ?? ""), [exchangeId]);
  const stocks = exchangeId ? exchangeStocks(instruments, exchangeId).slice(0, full ? 40 : 12) : [];
  const maxVolume = Math.max(...stocks.map((item) => item.volume24h), 1);
  return <section className="exchange-heatmap data-panel" aria-label="거래소별 히트맵">
    <div className="panel-title"><div><h2>{exchangeId || "거래소"} 거래소 히트맵</h2><p>타일 크기 · 24시간 거래량 / 색상 · 24시간 변동률</p></div><div className="heatmap-heading-actions">{!full && exchangeId && <a className="heatmap-more" href={`/heatmap?exchange=${encodeURIComponent(exchangeId)}`} target="_blank" rel="noreferrer">더보기 ↗</a>}<label className="heatmap-exchange-search"><span>거래소</span><input list="heatmap-exchanges" value={query} placeholder="거래소 검색" aria-label="히트맵 거래소 검색" onChange={(event) => { const next = event.target.value.toUpperCase(); setQuery(next); const match = exchanges.find((item) => item.toUpperCase() === next); if (match) onExchangeChange?.(match); }} /><datalist id="heatmap-exchanges">{exchanges.map((item) => <option key={item} value={item} />)}</datalist></label></div></div>
    {stocks.length ? <div className="exchange-heatmap-grid">{stocks.map((item) => <button type="button" key={item.code} className={`exchange-heatmap-cell ${tone(item.change24h ?? 0)}`} style={{ "--heat-size": `${1 + Math.sqrt(item.volume24h / maxVolume) * 3}` } as CSSProperties} onClick={() => onSelect?.(item.code)} title={`${item.name} · ${item.code}`}>
      <strong>{item.code}</strong><b>{formatChange(item.change24h ?? 0)}</b><small>{item.name}</small>
    </button>)}</div> : <p className="empty">선택한 거래소의 주식 데이터가 없습니다.</p>}
  </section>;
}
