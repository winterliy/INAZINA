import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { formatMoney } from "../../shared/money";
import "./SearchIndicators.css";

type Indicators = {
  countries: { country: string; currencyCode: string; baseRate: number | null; legalMaxRate: number | null }[];
  exchanges: { exchangeId: string; name: string; country: string; indexValue: number | null; updatedEpoch: number | null }[];
  products?: { code: string; name: string; issuer: string; productType: string; interestRate: number | null; currencyCode: string }[];
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
      <span className="search-name"><b>{country.country} 금리 정보</b><small>{country.currencyCode} · 국가 금리</small></span>
      <span className="search-indicator-rates">
        <span><small>기준금리</small><strong>{display(country.baseRate, "%")}</strong></span>
        <span><small>법정최대이자</small><strong>{display(country.legalMaxRate, "%")}</strong></span>
      </span>
    </div>)}
    {data.exchanges.map((exchange) => <div key={exchange.exchangeId} className="search-result search-indicator" aria-disabled="true">
      <span className="search-name"><b>{exchange.name}</b><small>{exchange.exchangeId} · 지수 · {exchange.country}</small></span>
      <span className="search-price"><strong>{display(exchange.indexValue)}</strong></span>
    </div>)}
    {(data.products ?? []).map((product) => <div key={`${product.productType}-${product.code}`} className="search-result search-indicator" aria-disabled="true">
      <span className="search-name"><b>{product.name}</b><small>{product.code} · {product.productType}{product.issuer ? ` · ${product.issuer}` : ""}</small></span>
      <span className="search-price"><strong>{display(product.interestRate, "%")}</strong><small>{product.currencyCode || "상품 금리"}</small></span>
    </div>)}
  </div>;
}
