import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { formatMoney } from "../../shared/money";
import "./SearchIndicators.css";

type Indicators = {
  countries: { country: string; currencyCode: string; baseRate: number | null; legalMaxRate: number | null }[];
  exchanges: { exchangeId: string; name: string; country: string; indexValue: number | null; updatedEpoch: number | null }[];
};
const display = (value: number | null, suffix = "") => value === null ? "—" : `${formatMoney(value, 2)}${suffix}`;

/** Reference data stays outside the instrument selection and trading-chart flow. */
export function SearchIndicators({ query }: { query: string }) {
  const [data, setData] = useState<Indicators | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setFailed(false);
    const timer = window.setTimeout(() => {
      void api<Indicators>(`/api/search-indicators?search=${encodeURIComponent(query.trim())}`, { signal: controller.signal })
        .then(setData).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    }, 150);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [query]);
  // Shown only while searching, as plain result rows: picking one never opens the live chart.
  if (failed || !data) return null;
  return <div className="search-results search-indicator-results" aria-label="금리·지수 검색 결과">
    {data.countries.map((country) => <div key={country.country} className="search-result search-indicator" aria-disabled="true">
      <span className="search-name"><b>{country.country} 기준금리</b><small>{country.currencyCode} · 금리 · 법정 최고 {display(country.legalMaxRate, "%")}</small></span>
      <span className="search-price"><strong>{display(country.baseRate, "%")}</strong></span>
    </div>)}
    {data.exchanges.map((exchange) => <div key={exchange.exchangeId} className="search-result search-indicator" aria-disabled="true">
      <span className="search-name"><b>{exchange.name}</b><small>{exchange.exchangeId} · 지수 · {exchange.country}</small></span>
      <span className="search-price"><strong>{display(exchange.indexValue)}</strong></span>
    </div>)}
  </div>;
}
