import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { exchangeStocks, formatChange, summarizeExchanges, tone } from "./ExchangeHeatmap";
import { GuideLayout, Hero, pad, RotatingStat, Section, type Stat } from "./GuideLayout";
import { formatMoneyNumeric } from "../../shared/money";
import { DotField } from "./HeroData";

type Instrument = Parameters<typeof summarizeExchanges>[0][number];

/** Full-page exchange heatmap (/heatmap). The embedded HTS widget stays in ExchangeHeatmap.tsx. */
export function HeatmapPage({ instruments, exchangeId, onExchangeChange, onSelect }: { instruments: Instrument[]; exchangeId?: string; onExchangeChange?: (exchangeId: string) => void; onSelect?: (code: string) => void }) {
  const summaries = useMemo(() => summarizeExchanges(instruments), [instruments]);
  const exchanges = summaries.map((item) => item.exchangeId);
  const [query, setQuery] = useState(exchangeId ?? "");
  useEffect(() => setQuery(exchangeId ?? ""), [exchangeId]);
  const stocks = exchangeId ? exchangeStocks(instruments, exchangeId).slice(0, 40) : [];
  const maxVolume = Math.max(...stocks.map((item) => item.volume24h), 1);
  const up = stocks.filter((item) => (item.change24h ?? 0) > 0).length;
  const down = stocks.filter((item) => (item.change24h ?? 0) < 0).length;
  const average = summaries.find((item) => item.exchangeId === exchangeId)?.averageChange ?? 0;
  const leader = stocks.reduce<(typeof stocks)[number] | null>((best, item) => (item.change24h ?? -Infinity) > (best?.change24h ?? -Infinity) ? item : best, null);
  const heroStats: Stat[] = [
    { label: "거래소", value: exchangeId || "거래소", unit: stocks.length ? `${stocks.length}종목` : undefined },
    ...(stocks.length ? [
      { label: "평균 변동률 · 24시간", value: formatChange(average), tone: average > 0 ? "up" as const : average < 0 ? "down" as const : undefined },
      { label: "상승 / 하락 종목", value: `▲ ${up} · ▼ ${down}`, unit: `${stocks.length}종목 중` },
      { label: "24시간 거래량", value: formatMoneyNumeric(String(stocks.reduce((sum, item) => sum + item.volume24h, 0))), unit: "주" },
      ...(leader?.change24h != null ? [{ label: "최대 상승", value: formatChange(leader.change24h), unit: `${leader.name} · ${leader.code}`, tone: leader.change24h > 0 ? "up" as const : leader.change24h < 0 ? "down" as const : undefined }] : []),
    ] : []),
  ];
  const tally = stocks.length > 0 && `▲ ${up} · ▼ ${down} · ${stocks.length}종목`;
  return <GuideLayout
    nav={summaries.map((item, index) => <button type="button" key={item.exchangeId} aria-current={item.exchangeId === exchangeId} onClick={() => onExchangeChange?.(item.exchangeId)}>{item.exchangeId}<small>{pad(index + 1)} · <span className={item.averageChange > 0 ? "up" : item.averageChange < 0 ? "down" : ""}>{formatChange(item.averageChange)}</span></small></button>)}
    footer={<a href="/">HTS로 돌아가기</a>}>
    <Hero background={<DotField stocks={stocks.map((item) => ({ code: item.code, name: item.name, volume: item.volume24h, change: item.change24h ?? 0 }))} onSelect={onSelect} />}><RotatingStat stats={heroStats} /></Hero>
    <header className="bg-intro">
      <div><p className="bg-label">INAZINA · HEATMAP</p><h1>{exchangeId || "거래소"} 거래소 히트맵</h1></div>
      <div className="bg-copy">
        <p>타일 크기 · 24시간 거래량 / 색상 · 24시간 변동률</p>
        <label className="bg-search"><span>거래소</span><input list="heatmap-exchanges" value={query} placeholder="거래소 검색" aria-label="히트맵 거래소 검색" onChange={(event) => { const next = event.target.value.toUpperCase(); setQuery(next); const match = exchanges.find((item) => item.toUpperCase() === next); if (match) onExchangeChange?.(match); }} /><datalist id="heatmap-exchanges">{exchanges.map((item) => <option key={item} value={item} />)}</datalist></label>
      </div>
    </header>
    <Section index={1} title="거래소별 히트맵" aside={tally && <p>{tally}</p>}>
      {stocks.length ? <div className="bg-heat">{stocks.map((item) => { const change = item.change24h ?? 0; return <button type="button" key={item.code} className={`bg-tile ${tone(change)}`} style={{ "--heat-size": `${1 + Math.sqrt(item.volume24h / maxVolume) * 3}`, "--heat-alpha": `${Math.min(1, .55 + Math.abs(change) / 10)}` } as CSSProperties} onClick={() => onSelect?.(item.code)} title={`${item.name} · ${item.code}`}>
        <strong>{item.code}</strong><b>{formatChange(change)}</b><small>{item.name}</small>
      </button>; })}</div> : <p className="bg-empty">선택한 거래소의 주식 데이터가 없습니다.</p>}
    </Section>
  </GuideLayout>;
}
