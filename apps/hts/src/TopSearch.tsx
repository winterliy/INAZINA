import { SearchIndicators } from "./SearchIndicators";
import { tabListKeys } from "./tabKeys";
import { RECOMMEND_SORTS, SEARCH_TABS, WarningBadge, instrumentType, money } from "./appShared";
import type { useInstrumentSearch } from "./useInstrumentSearch";

type Props = {
  state: ReturnType<typeof useInstrumentSearch>;
  selectedCode: string;
  homeCurrency: string;
  onChoose: (code: string) => void;
  onExchangeSelect: (exchangeId: string) => void;
};

/** The top-bar search combobox with its drop-down of product tabs, rankings, rate cards and results. */
export function TopSearch({ state, selectedCode, homeCurrency, onChoose, onExchangeSelect }: Props) {
  const { search, setSearch, searchOpen, setSearchOpen, tab, setTab, recommendSort, setRecommendSort, setActiveIndex, inputRef, boxRef, filtered, tabCounts, visibleResults, activeResult, fxRates } = state;
  // While the tabs are shown, the result list is their tab panel.
  const panel = search.trim() ? {} : { id: "search-tab-panel", role: "tabpanel", "aria-labelledby": `search-tab-${tab}` };
  return <div className="top-search" ref={boxRef}>
  <label className="top-search-field"><svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M20 20l-4-4" /></svg>
    <input ref={inputRef} value={search} role="combobox" aria-autocomplete="list" aria-label="종목·상품코드·금리·지수 검색" aria-expanded={searchOpen} aria-controls="top-search-list" aria-activedescendant={searchOpen && activeResult ? `search-opt-${activeResult.code}` : undefined} placeholder="종목명·상품코드·국가·거래소 검색" onFocus={() => setSearchOpen(true)} onChange={(event) => { setSearch(event.target.value); setSearchOpen(true); }} onKeyDown={(event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!searchOpen) { setSearchOpen(true); return; }
        if (visibleResults.length) setActiveIndex((index) => (Math.min(index, visibleResults.length - 1) + (event.key === "ArrowDown" ? 1 : -1) + visibleResults.length) % visibleResults.length);
      } else if (event.key === "Enter" && searchOpen && activeResult) { event.preventDefault(); onChoose(activeResult.code); }
    }} />
    {search && <button type="button" className="top-search-clear" aria-label="검색어 지우기" onClick={() => { setSearch(""); inputRef.current?.focus(); }}>×</button>}
  </label>
  {searchOpen && <section id="top-search-pop" className="search-pop" aria-label="종목 검색 결과">{!search.trim() && <div className="search-tabs" role="tablist" aria-label="상품 종류" onKeyDown={tabListKeys}>{SEARCH_TABS.filter(([id]) => id === "recommended" || tabCounts[id]).map(([id, label]) => <button key={id} type="button" role="tab" id={`search-tab-${id}`} aria-controls="search-tab-panel" tabIndex={tab === id ? 0 : -1} aria-selected={tab === id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>{label}<span>{tabCounts[id] ?? 0}</span></button>)}</div>}{!search.trim() && tab === "recommended" && <><div className="recommend-sorts" role="group" aria-label="추천 기준">{RECOMMEND_SORTS.map(([id, label]) => <button key={id} type="button" aria-pressed={recommendSort === id} className={recommendSort === id ? "active" : ""} onClick={() => setRecommendSort(id)}>{label}</button>)}</div><p className="search-caption">{RECOMMEND_SORTS.find(([id]) => id === recommendSort)![2]}</p></>}<div className="search-panel" {...panel}><div className="search-results" id="top-search-list" role="listbox" aria-label="종목 검색 결과">{fxRates.map((fx) => <div key={`fx-${fx.code}`} role="group" className="search-result fx-rate" aria-label={`${homeCurrency}:${fx.code} 환율`}><span><span className="search-name"><b>{homeCurrency}:{fx.code}</b><small>{fx.name} · 환율</small></span></span><span className="search-price"><strong>1 : {money(fx.rate, 6)}</strong><small>1 {fx.code} = {money(fx.inverse, 4)} {homeCurrency}</small></span></div>)}{visibleResults.map((item, index) => <button type="button" role="option" id={`search-opt-${item.code}`} aria-selected={activeResult?.code === item.code} tabIndex={-1} key={item.code} className={`search-result${selectedCode === item.code ? " active" : ""}${activeResult?.code === item.code ? " kbd" : ""}`} onClick={() => onChoose(item.code)}><span>{!search.trim() && tab === "recommended" && <em className="search-rank">{index + 1}</em>}<span className="search-name"><b>{item.name} <WarningBadge warning={item.warning} /></b><small>{item.code} · {instrumentType(item)}{!search.trim() && tab === "recommended" && recommendSort === "volume" ? (item.volume24h ? ` · 24시간 ${money(item.volume24h, 2)}주` : "") : item.trades24h ? ` · 24시간 ${money(item.trades24h)}건` : ""}</small></span></span><span className="search-price"><strong>{money(item.price, 2)} {item.quote_currency || homeCurrency}</strong>{item.change24h !== null && item.change24h !== undefined && <small className={item.change24h > 0 ? "up" : item.change24h < 0 ? "down" : undefined}>{item.change24h > 0 ? "+" : ""}{Number(item.change24h).toFixed(2)}%</small>}</span></button>)}{!filtered.length && !fxRates.length && <p className="empty">{search.trim() ? "거래 종목 결과가 없습니다. 상품 코드·금리·지수는 아래 관련 결과를 확인하세요." : tab === "recommended" ? "최근 24시간 동안 해당하는 종목이 없습니다." : "이 종류의 상품이 없습니다."}</p>}</div></div>{search.trim() && <SearchIndicators query={search} onExchangeSelect={onExchangeSelect} />}</section>}
</div>;
}
