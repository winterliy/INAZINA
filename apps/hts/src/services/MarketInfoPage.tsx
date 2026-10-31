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
  const [error, setError] = useState("");
  useEffect(() => {
    setError("");
    api<Panel>(`/api/info/panel?category=${encodeURIComponent(category)}&page=${page}`).then(setPanel).catch((cause) => setError(cause instanceof Error ? cause.message : "정보를 불러오지 못했습니다."));
  }, [category, page]);
  const choose = (next: string) => { setCategory(next); setPage(0); };
  return <div className="svc-market">
    <SearchIndicators query="" />
    <nav className="svc-market-nav" aria-label="정보 종류">{(panel?.categories ?? [{ key: "main", label: "메인" }]).map((item) => <button key={item.key} type="button" aria-current={category === item.key ? "page" : undefined} onClick={() => choose(item.key)}>{item.label}</button>)}</nav>
    <section className="svc-card"><SearchIndicators query="" /></section>
    <section className="svc-card">
      <header><h3>{panel?.title.replace(/^\S+\s/, "") ?? "불러오는 중…"}</h3></header>
      {error && <p className="account-error" role="alert">{error}</p>}
      {panel && <EmbedView embed={panel} />}
      {panel && (panel.hasPrevious || panel.hasNext) && <div className="svc-actions"><button type="button" disabled={!panel.hasPrevious} onClick={() => setPage(page - 1)}>이전</button><button type="button" disabled={!panel.hasNext} onClick={() => setPage(page + 1)}>다음</button></div>}
    </section>
  </div>;
}
