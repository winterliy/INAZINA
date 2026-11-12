import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { formatMoney } from "../../shared/money";
import "./SearchIndicators.css";

type Indicators = {
  countries: { country: string; currencyCode: string; baseRate: number | null; legalMaxRate: number | null }[];
  exchanges: { exchangeId: string; name: string; country: string; indexValue: number | null; updatedEpoch: number | null }[];
  products?: { code: string; name: string; issuer: string; productType: string; interestRate: number | null; currencyCode: string }[];
};
type IndicatorChart = { title: string; series: { name: string; color: string; points: { time: number; value: number | null }[] }[] };
const display = (value: number | null, suffix = "") => value === null ? "—" : `${formatMoney(value, 2)}${suffix}`;

function HistoryChart({ data }: { data: IndicatorChart }) {
  const available = data.series.flatMap((item) => item.points.filter((point) => point.value !== null));
  if (!available.length) return <p className="indicator-chart-empty">저장된 변동 이력이 없습니다.</p>;
  const width = 720, height = 190, pad = 18;
  const times = available.map((point) => point.time);
  const values = available.map((point) => point.value as number);
  const minTime = Math.min(...times), maxTime = Math.max(...times);
  const minValue = Math.min(...values), maxValue = Math.max(...values);
  const valueSpan = maxValue - minValue || Math.max(Math.abs(maxValue) * 0.04, 1);
  const x = (time: number) => pad + (maxTime === minTime ? 0.5 : (time - minTime) / (maxTime - minTime)) * (width - pad * 2);
  const y = (value: number) => height - pad - ((value - minValue) / valueSpan) * (height - pad * 2);
  return <div className="indicator-chart">
    <div className="indicator-chart-legend">{data.series.map((item) => <span key={item.name}><i style={{ background: item.color }} />{item.name}</span>)}</div>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${data.title} 변동 그래프`}>
      {[0, 0.5, 1].map((step) => <line key={step} x1={pad} x2={width - pad} y1={pad + step * (height - pad * 2)} y2={pad + step * (height - pad * 2)} className="indicator-chart-grid" />)}
      {data.series.map((item) => {
        const points = item.points.filter((point): point is { time: number; value: number } => point.value !== null);
        return points.length > 1 ? <polyline key={item.name} fill="none" stroke={item.color} strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" points={points.map((point) => `${x(point.time)},${y(point.value)}`).join(" ")} />
          : points.map((point) => <circle key={`${item.name}-${point.time}`} cx={x(point.time)} cy={y(point.value)} r="4" fill={item.color} />);
      })}
    </svg>
    <small>{minTime ? new Date(minTime * 1000).toLocaleDateString("ko-KR") : "—"} — {maxTime ? new Date(maxTime * 1000).toLocaleDateString("ko-KR") : "—"}</small>
  </div>;
}

/** Reference data stays outside the instrument selection and trading-chart flow. */
export function SearchIndicators({ query, onExchangeSelect }: { query: string; onExchangeSelect?: (exchangeId: string) => void }) {
  const [data, setData] = useState<Indicators | null>(null);
  const [failed, setFailed] = useState(false);
  const [openChart, setOpenChart] = useState("");
  const [chart, setChart] = useState<IndicatorChart | null>(null);
  const [chartLoading, setChartLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setFailed(false);
    setOpenChart("");
    setChart(null);
    const timer = window.setTimeout(() => {
      void api<Indicators>(`/api/search-indicators?search=${encodeURIComponent(query.trim())}`, { signal: controller.signal })
        .then(setData).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    }, 150);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [query]);
  async function toggleChart(kind: "country" | "exchange", key: string) {
    const id = `${kind}:${key}`;
    if (openChart === id) { setOpenChart(""); setChart(null); return; }
    setOpenChart(id); setChart(null); setChartLoading(true);
    try {
      const result = await api<IndicatorChart>(`/api/search-indicator-chart?kind=${kind}&key=${encodeURIComponent(key)}`);
      setChart(result);
    } catch {
      setChart({ title: key, series: [] });
    } finally { setChartLoading(false); }
  }
  // These are reference indicators rather than tradeable instruments; clicking expands their history here.
  if (failed || !data) return null;
  return <div className="search-results search-indicator-results" aria-label="금리·지수 검색 결과">
    {data.countries.map((country) => { const id = `country:${country.country}`; return <div key={id}>
      <button type="button" className="search-result search-indicator" aria-expanded={openChart === id} onClick={() => void toggleChart("country", country.country)}>
        <span className="search-name"><b>{country.country} 금리 정보</b><small>{country.currencyCode} · 국가 금리 · 그래프 보기</small></span>
        <span className="search-indicator-rates">
          <span><small>기준금리</small><strong>{display(country.baseRate, "%")}</strong></span>
          <span><small>법정최대이자</small><strong>{display(country.legalMaxRate, "%")}</strong></span>
        </span>
      </button>
      {openChart === id && <div className="indicator-chart-panel">{chartLoading ? <p className="indicator-chart-empty">그래프 불러오는 중…</p> : chart && <HistoryChart data={chart} />}</div>}
    </div>; })}
    {data.exchanges.map((exchange) => { const id = `exchange:${exchange.exchangeId}`; return <div key={id}>
      <button type="button" className="search-result search-indicator" aria-expanded={openChart === id} onClick={() => { onExchangeSelect?.(exchange.exchangeId); void toggleChart("exchange", exchange.exchangeId); }}>
        <span className="search-name"><b>{exchange.name}</b><small>{exchange.exchangeId} · 지수 · {exchange.country} · 그래프 보기</small></span>
        <span className="search-price"><strong>{display(exchange.indexValue)}</strong></span>
      </button>
      {openChart === id && <div className="indicator-chart-panel">{chartLoading ? <p className="indicator-chart-empty">그래프 불러오는 중…</p> : chart && <HistoryChart data={chart} />}</div>}
    </div>; })}
    {(data.products ?? []).map((product) => <div key={`${product.productType}-${product.code}`} className="search-result search-indicator" aria-disabled="true">
      <span className="search-name"><b>{product.name}</b><small>{product.code} · {product.productType}{product.issuer ? ` · ${product.issuer}` : ""}</small></span>
      <span className="search-price"><strong>{display(product.interestRate, "%")}</strong><small>{product.currencyCode || "상품 금리"}</small></span>
    </div>)}
  </div>;
}
