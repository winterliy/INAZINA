import { useEffect, useState } from "react";
import { api } from "../../../shared/api";
import { EmbedView, type Embed } from "./DiscordText";
import { SearchIndicators } from "../SearchIndicators";
import type { ServiceProps } from "./ServiceKit";

type Panel = Embed & { categories: { key: string; label: string }[]; category: string; page: number; hasPrevious: boolean; hasNext: boolean };

/** 시장정보: the Discord /검색 panel — rankings, asset ranking, indices, rates and every product list. */
export function MarketInfoPage(_props: ServiceProps) {
  const [category, setCategory] = useState(() => new URLSearchParams(location.search).get("category") ?? "main");
  const [page, setPage] = useState(0);
  const [panel, setPanel] = useState<Panel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    setLoading(true);
    api<Panel>(`/api/info/panel?category=${encodeURIComponent(category)}&page=${page}`, { signal: controller.signal })
      .then((next) => { setPanel(next); setLoading(false); })
      .catch((cause) => { if (!controller.signal.aborted) { setError(cause instanceof Error ? cause.message : "정보를 불러오지 못했습니다."); setLoading(false); } });
    return () => controller.abort();
  }, [category, page]);
  function choose(next: string) {
    setCategory(next);
    setPage(0);
    // The chosen list stays in the address, so it survives a reload and can be shared.
    const url = new URL(location.href);
    if (next === "main") url.searchParams.delete("category"); else url.searchParams.set("category", next);
    history.replaceState(history.state, "", url);
  }
  const categories = panel?.categories ?? [{ key: "main", label: "메인" }];
  const title = panel?.title.replace(/^\S+\s/, "") ?? "불러오는 중…";
  const pager = panel && (panel.hasPrevious || panel.hasNext) && <div className="svc-market-pager">
    <button type="button" disabled={loading || !panel.hasPrevious} onClick={() => setPage(page - 1)} aria-label="이전 쪽">‹</button>
    <span>{page + 1}쪽</span>
    <button type="button" disabled={loading || !panel.hasNext} onClick={() => setPage(page + 1)} aria-label="다음 쪽">›</button>
  </div>;
  return <div className="svc-market">
    <nav className="svc-market-nav" aria-label="정보 종류">{categories.map((item) => <button key={item.key} type="button" aria-current={category === item.key ? "page" : undefined} onClick={() => choose(item.key)}>{item.label}</button>)}</nav>
    <div className="svc-market-main">
      <section className="svc-card svc-market-panel" aria-busy={loading}>
        <header className="svc-market-head"><h3>{title}</h3>{pager}</header>
        {error && <p className="account-error" role="alert">{error}</p>}
        {panel ? <div className={loading ? "svc-market-body loading" : "svc-market-body"}><EmbedView embed={panel} /></div> : !error && <div className="svc-market-skeleton" aria-hidden="true"><span /><span /><span /></div>}
        {pager && <footer className="svc-market-foot">{pager}</footer>}
      </section>
      {category === "main" && <section className="svc-card"><header><h3>금리 · 거래소 지수</h3><p>나라별 기준금리·법정최고금리와 거래소 지수입니다. 항목을 누르면 변동 그래프가 열립니다.</p></header><SearchIndicators query="" /></section>}
    </div>
  </div>;
}
