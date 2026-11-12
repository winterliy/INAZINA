import type { Money } from "../../shared/money";
import { formatMoneyNumeric } from "../../shared/money";
import { useEffect, useMemo, useState } from "react";
import { GuideLayout, Hero, isDark, pad, RotatingStat, Section } from "./GuideLayout";
import { AllocationBands } from "./HeroData";

export type PortfolioHolding = { code: string; name: string; qty: number; value: Money; type?: string | null; quote_currency?: string; sell_reserved_qty?: number; is_collateral?: boolean };
export type PortfolioInstrument = { code: string; change24h?: number | null };

const PALETTE = ["#f45f00", "#1e3d59", "#fa9819", "#48749e", "#b6c9cf", "#7a2e00", "#2c5a82", "#c6ebf7"];

export function allocationSegments(holdings: PortfolioHolding[]) {
  const positive = holdings.filter((item) => Number(item.value) > 0);
  const total = positive.reduce((sum, item) => sum + Number(item.value), 0);
  return positive.map((item, index) => ({ ...item, percent: total ? Number(item.value) / total * 100 : 0, color: PALETTE[index % PALETTE.length] }));
}

export function performanceRankings(holdings: PortfolioHolding[], instruments: PortfolioInstrument[]) {
  const changes = new Map(instruments.map((item) => [item.code, item.change24h]));
  return holdings.map((item) => ({ ...item, change24h: changes.get(item.code) ?? null })).filter((item) => item.change24h != null).sort((a, b) => Number(b.change24h) - Number(a.change24h));
}

const signed = (value: number) => `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
const SECTIONS = ["보유 비중", "순위", "자산군과 성장", "포트폴리오 리포트", "모든 내 주식"];

export function PortfolioPage({ holdings, instruments, homeCurrency, cash = 0, totalAssets = 0, onSelect }: { holdings: PortfolioHolding[]; instruments: PortfolioInstrument[]; homeCurrency: string; cash?: Money; totalAssets?: Money; onSelect: (code: string) => void }) {
  const [filter, setFilter] = useState<"all" | "up" | "down">("all");
  const [sort, setSort] = useState<"value" | "change" | "qty">("value");
  const segments = allocationSegments(holdings);
  const rankings = performanceRankings(holdings, instruments);
  const total = segments.reduce((sum, item) => sum + Number(item.value), 0);
  const changes = useMemo(() => new Map(instruments.map((item) => [item.code, item.change24h])), [instruments]);
  const assetGroups = useMemo(() => { const groups = new Map<string, number>(); holdings.forEach((item) => groups.set(item.type || "주식", (groups.get(item.type || "주식") || 0) + Number(item.value))); return [...groups].sort((a, b) => b[1] - a[1]); }, [holdings]);
  const topThree = segments.slice().sort((a, b) => Number(b.value) - Number(a.value)).slice(0, 3).reduce((sum, item) => sum + item.percent, 0);
  const cashRatio = Number(totalAssets) > 0 ? Number(cash) / Number(totalAssets) * 100 : 0;
  const diversity = Math.max(0, Math.min(100, Math.round(100 - topThree * .55 + Math.min(20, segments.length * 2))));
  const visibleHoldings = useMemo(() => holdings.filter((item) => filter === "all" || (filter === "up" ? Number(changes.get(item.code) || 0) >= 0 : Number(changes.get(item.code) || 0) < 0)).sort((a, b) => sort === "qty" ? b.qty - a.qty : sort === "change" ? Number(changes.get(b.code) || 0) - Number(changes.get(a.code) || 0) : Number(b.value) - Number(a.value)), [holdings, changes, filter, sort]);
  const [snapshots, setSnapshots] = useState<number[]>([]);
  useEffect(() => { const key = "inazina-portfolio-snapshots"; try { const old = JSON.parse(localStorage.getItem(key) || "[]"); const next = [...(Array.isArray(old) ? old.filter((item) => Number.isFinite(item)) : []), total].slice(-30); localStorage.setItem(key, JSON.stringify(next)); setSnapshots(next); } catch { setSnapshots([total]); } }, [total]);
  // The hero's figure sits over the middle of the bands; pick its text colour from that band.
  let cumulative = 0;
  const centreBand = segments.find((item) => (cumulative += item.percent) >= 50);
  const formatValue = (value: Money) => `${formatMoneyNumeric(value)} ${homeCurrency}`;
  const low = Math.min(...snapshots), high = Math.max(...snapshots);
  const points = snapshots.map((value, index) => `${(index / Math.max(1, snapshots.length - 1)) * 300},${100 - ((value - low) / Math.max(1, high - low)) * 82 - 9}`).join(" ");

  const ranking = (items: typeof rankings, empty: string) => items.length ? <ol className="bg-list">{items.slice(0, 5).map((item, index) => <li key={item.code}><button type="button" className="bg-row" onClick={() => onSelect(item.code)}>
    <em>{pad(index + 1)}</em><span><b>{item.name}</b><small>{item.code}</small></span><strong className={Number(item.change24h) >= 0 ? "up" : "down"}>{signed(Number(item.change24h))}</strong>
  </button></li>)}</ol> : <p className="bg-empty">{empty}</p>;

  return <GuideLayout
    nav={SECTIONS.map((label, index) => <a key={label} href={`#pf-${index + 1}`}>{label}<small>{pad(index + 1)}</small></a>)}
    footer={<a href="/">HTS로 돌아가기</a>}>
    <Hero dark={isDark(centreBand?.color ?? "#ffffff")} background={<AllocationBands bands={segments} onSelect={onSelect} />}><RotatingStat stats={[
      { label: "평가액", value: formatMoneyNumeric(total), unit: homeCurrency },
      { label: "현금", value: formatMoneyNumeric(cash), unit: homeCurrency },
      { label: "총자산", value: formatMoneyNumeric(totalAssets), unit: homeCurrency },
      { label: "현금 비중", value: `${cashRatio.toFixed(1)}%` },
      { label: "보유 종목", value: `${holdings.length}개`, unit: `평가액이 있는 종목 ${segments.length}개` },
    ]} /></Hero>
    <header className="bg-intro">
      <div><p className="bg-label">INAZINA · PORTFOLIO</p><h1>내 주식</h1></div>
      <div className="bg-copy">
        <p>보유 비중과 최근 성과를 한눈에 확인합니다.</p>
        <p className="bg-muted">{holdings.length}개 종목 · 평가액 기준</p>
        <a className="bg-btn" href="/">HTS로 돌아가기 <i aria-hidden="true">→</i></a>
      </div>
    </header>

    <Section id="pf-1" index={1} title="보유 비중" aside={<p>{formatMoneyNumeric(total)} {homeCurrency}</p>}>
      {segments.length > 0 && <div className="bg-stack" role="img" aria-label="보유 비중 막대">{segments.map((item) => <i key={item.code} style={{ flexGrow: item.percent, background: item.color }} title={`${item.name} ${item.percent.toFixed(1)}%`} />)}</div>}
      <div className="bg-list">{segments.length ? segments.map((item) => <button type="button" className="bg-row" key={item.code} onClick={() => onSelect(item.code)}>
        <i className="bg-swatch" style={{ background: item.color }} /><span><b>{item.name}</b><small>{item.code}</small></span>
        <strong>{item.percent.toFixed(1)}%<small>{formatValue(item.value)}</small></strong>
      </button>) : <p className="bg-empty">보유 중인 주식이 없습니다.</p>}</div>
    </Section>

    <Section id="pf-2" index={2} title="순위" aside={<p>24시간</p>}>
      <div className="bg-cols">
        <article><h3>성장률 순위 <small>24시간</small></h3>{ranking(rankings, "변동률 데이터가 없습니다.")}</article>
        <article><h3>하락률 순위 <small>24시간</small></h3>{ranking([...rankings].reverse(), "변동률 데이터가 없습니다.")}</article>
      </div>
    </Section>

    <Section id="pf-3" index={3} title="자산군과 성장">
      <div className="bg-cols">
        <article><h3>자산군별 비중 <small>{assetGroups.length}종류</small></h3>
          {assetGroups.length ? <div className="bg-list">{assetGroups.map(([label, value]) => <div className="bg-row static" key={label}><span><b>{label}</b><small>{total ? (value / total * 100).toFixed(1) : "0.0"}%</small></span><strong>{formatMoneyNumeric(value)} {homeCurrency}</strong></div>)}</div> : <p className="bg-empty">자산 데이터가 없습니다.</p>}
        </article>
        <article><h3>포트폴리오 성장 <small>최근 방문 기준</small></h3>
          <div className="bg-panel bg-spark" aria-label="최근 포트폴리오 평가액 변화"><svg viewBox="0 0 300 100" preserveAspectRatio="none" role="img">{snapshots.length > 1 && <polyline points={points} fill="none" stroke="currentColor" strokeWidth="2.5" vectorEffect="non-scaling-stroke" />}</svg></div>
          <p className="bg-muted">평균매수가 데이터가 없어 누적 손익률은 계산하지 않습니다. 이 브라우저에서 확인한 평가액을 저장해 추이를 표시합니다.</p>
        </article>
      </div>
    </Section>

    <Section id="pf-4" index={4} title="포트폴리오 리포트" aside={<p>분산·집중도</p>}>
      <div className="bg-metrics">
        <div><span>상위 3종목 비중</span><b>{topThree.toFixed(1)}%</b><small>{topThree >= 70 ? "집중도가 높습니다" : "분산 상태 양호"}</small></div>
        <div><span>현금 비중</span><b>{cashRatio.toFixed(1)}%</b><small>{formatMoneyNumeric(cash)} {homeCurrency}</small></div>
        <div><span>보유 종목 수</span><b>{holdings.length}개</b><small>평가액이 있는 종목 {segments.length}개</small></div>
        <div><span>분산 점수</span><b>{diversity}</b><small>상위 비중과 종목 수 기반 참고치</small></div>
      </div>
    </Section>

    <Section id="pf-5" index={5} title="모든 내 주식" aside={<p>{visibleHoldings.length} / {holdings.length}개</p>}>
      <div className="bg-toolbar">
        <div className="bg-seg" role="group" aria-label="등락 필터">{([["all", "전체"], ["up", "상승"], ["down", "하락"]] as const).map(([id, label]) => <button key={id} type="button" aria-pressed={filter === id} onClick={() => setFilter(id)}>{label}</button>)}</div>
        <select value={sort} onChange={(event) => setSort(event.target.value as typeof sort)} aria-label="보유 주식 정렬"><option value="value">평가액순</option><option value="change">변동률순</option><option value="qty">수량순</option></select>
      </div>
      <div className="bg-list two">{visibleHoldings.length ? visibleHoldings.map((item) => { const change = changes.get(item.code); return <button type="button" className="bg-row" key={item.code} onClick={() => onSelect(item.code)}>
        <span><b>{item.name}</b><small>{item.code} · {item.qty}주{item.is_collateral ? " · 담보" : ""}</small></span>
        <strong>{formatValue(item.value)}<small className={change == null ? "" : Number(change) >= 0 ? "up" : "down"}>{change == null ? "—" : signed(Number(change))}</small></strong>
      </button>; }) : <p className="bg-empty">조건에 맞는 보유 종목이 없습니다.</p>}</div>
    </Section>
  </GuideLayout>;
}
