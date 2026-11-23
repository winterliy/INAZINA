import { useEffect, useMemo, useRef, useState } from "react";
import { moneyDivide } from "../../shared/money";
import { instrumentType, recommended, searchTab, type RecommendSort, type SearchTab } from "./appShared";
import type { Instrument } from "./types";

/** State of the top-bar search box: query, tab, ranking, the filtered results and the keyboard-highlighted one. */
export function useInstrumentSearch(instruments: Instrument[], homeCurrency: string) {
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [tab, setTab] = useState<SearchTab>("recommended");
  const [recommendSort, setRecommendSort] = useState<RecommendSort>("value");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    // Exchange rates show as rate cards only; they never open the live chart.
    const tradable = instruments.filter((item) => item.type !== "forex");
    if (!term) return tab === "recommended" ? recommended(tradable, recommendSort) : tradable.filter((item) => searchTab(item) === tab);
    return tradable.filter((item) => `${item.code} ${item.name} ${item.type} ${instrumentType(item)}`.toLocaleLowerCase().includes(term))
      .sort((a, b) => Number(b.code.toLocaleLowerCase() === term) - Number(a.code.toLocaleLowerCase() === term));
  }, [instruments, search, tab, recommendSort]);
  const tabCounts = useMemo(() => {
    const counts: Partial<Record<SearchTab, number>> = { recommended: Math.min(20, instruments.length) };
    for (const item of instruments) if (item.type !== "forex") counts[searchTab(item)] = (counts[searchTab(item)] ?? 0) + 1;
    return counts;
  }, [instruments]);

  // Search combobox: the arrow keys move through the results, Enter picks the highlighted one.
  const visibleResults = filtered.slice(0, 200);
  const activeResult = visibleResults[Math.min(activeIndex, visibleResults.length - 1)];
  useEffect(() => { setActiveIndex(0); }, [search, tab, recommendSort]);
  useEffect(() => {
    if (searchOpen && activeResult) document.getElementById(`search-opt-${activeResult.code}`)?.scrollIntoView({ block: "nearest" });
  }, [activeResult?.code, searchOpen]);

  // The search drops down under the top-bar field, like a portal's: Escape or a click elsewhere closes it.
  useEffect(() => {
    if (!searchOpen) return;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") { setSearchOpen(false); inputRef.current?.blur(); } };
    const onDown = (event: MouseEvent) => { if (!boxRef.current?.contains(event.target as Node)) setSearchOpen(false); };
    window.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onDown);
    return () => { window.removeEventListener("keydown", onKeyDown); document.removeEventListener("mousedown", onDown); };
  }, [searchOpen]);

  // "기준화폐:검색화폐" — how much of the searched currency one unit of the user's currency buys.
  const term = search.trim().toLocaleLowerCase();
  const fxPrice = (code: string, base: string) => code === base ? 1 : instruments.find((item) => item.type === "forex" && item.currency_code === code)?.price;
  const fxRates = term && homeCurrency ? instruments.filter((item) => item.type === "forex" && item.currency_code && item.currency_code !== homeCurrency
    && `${item.currency_code} ${item.name}`.toLocaleLowerCase().includes(term)).flatMap((item) => {
    const home = fxPrice(homeCurrency, item.quote_currency || "");
    return home && Number(item.price) ? [{ code: item.currency_code!, name: item.name, rate: moneyDivide(home, item.price, 6), inverse: moneyDivide(item.price, home, 4) }] : [];
  }) : [];

  return { search, setSearch, searchOpen, setSearchOpen, tab, setTab, recommendSort, setRecommendSort, setActiveIndex, inputRef, boxRef, filtered, tabCounts, visibleResults, activeResult, fxRates };
}
