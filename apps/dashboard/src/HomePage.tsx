import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { NAV_GROUPS } from "./nav";
import { date, number } from "./common";

type Exchange = { exchange_id: string; status: string; trading_halted: number };
type Audit = { auditId: number; actorId: string; action: string; payload: Record<string, unknown>; time: number };
type Index = { exchangeId: string; value: number; change24h: number | null };
type Todo = { id: string; title: string; note: string; count: number };

/**
 * The main page: what needs a decision today, the markets at a glance, the latest administrator work and
 * every page by group. It reads only what other pages already show, so a failed request just leaves that block empty.
 */
export function HomePage({ market, bankDeficits, labels, badges, onPage }: {
  market: Exchange[] | undefined; bankDeficits: { issuer: string; nationality: string }[]; labels: Map<string, string>;
  badges: Record<string, number>; onPage: (page: string) => void;
}) {
  const [court, setCourt] = useState(0);
  const [reports, setReports] = useState(0);
  const [indexes, setIndexes] = useState<Index[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  useEffect(() => {
    api<{ counts: Record<string, number> }>("/api/admin/court").then((result) => setCourt((result.counts.submitted ?? 0) + (result.counts.reviewing ?? 0))).catch(() => undefined);
    api<{ reports: unknown[] }>("/api/admin/board").then((result) => setReports(result.reports.length)).catch(() => undefined);
    api<{ indexes: Index[] }>("/api/market/exchange-indexes").then((result) => setIndexes(result.indexes)).catch(() => undefined);
    api<{ entries: Audit[] }>("/api/admin/audit").then((result) => setAudit(result.entries.slice(0, 5))).catch(() => undefined);
  }, []);

  const todos: Todo[] = ([
    { id: "naturalization", title: "귀화 심사 대기", note: "승인하면 자산이 정리됩니다", count: badges.naturalization ?? 0 },
    { id: "feedback", title: "문의 · 신고 새 글", note: "읽지 않은 글", count: badges.feedback ?? 0 },
    { id: "board", title: "게시판 신고 대기", note: "처리하지 않은 신고", count: reports },
    { id: "court", title: "회생 · 파산 접수", note: "접수·심리 중인 신청", count: court },
    { id: "overview", title: "은행 본사 계좌 적자", note: bankDeficits.map((bank) => bank.issuer).join(" · "), count: bankDeficits.length },
  ] as Todo[]).filter((item) => item.count > 0 && labels.has(item.id));
  const open = (market ?? []).filter((item) => item.status === "open" && !item.trading_halted).length;
  const indexOf = new Map(indexes.map((item) => [item.exchangeId, item]));
  const pending = todos.reduce((sum, item) => sum + item.count, 0);

  return <>
    <section className="kpis" aria-label="요약">
      <div className="kpi"><span>확인할 일</span><b>{number(pending)}건</b><em>{todos.length}개 항목</em></div>
      <div className="kpi"><span>열린 거래소</span><b>{market ? `${open} / ${market.length}` : "—"}</b><em>{market && market.length > open ? `${market.length - open}곳 휴장` : "모두 열림"}</em></div>
    </section>

    <section className="sec">
      <div className="lab"><small>01 / 오늘 확인할 일</small><p>처리를 기다리는 항목만 모았습니다.</p></div>
      <div className="sec-body">{todos.length
        ? <div className="rows">{todos.map((item) => <div className="row" key={item.id}>
          <div><b>{item.title}</b><small>{item.note}</small></div>
          <span className="row-end"><span className="nav-badge">{item.count}</span><a className="link-out" href={`#${item.id}`} onClick={(event) => { event.preventDefault(); onPage(item.id); }}>열기</a></span>
        </div>)}</div>
        : <p className="empty">지금 처리할 일이 없습니다.</p>}</div>
    </section>

    <section className="sec">
      <div className="lab"><small>02 / 시장 한눈에</small><p>거래소별 상태와 지수입니다. 개장과 휴장은 거래소 탭에서 합니다.</p></div>
      <div className="sec-body"><div className="scroll-x"><table><thead><tr><th>거래소</th><th className="r">지수</th><th className="r">24시간</th><th>상태</th></tr></thead><tbody>
        {(market ?? []).map((item) => {
          const index = indexOf.get(item.exchange_id);
          return <tr key={item.exchange_id}><td className="mono">{item.exchange_id}</td>
            <td className="r">{index ? number(Math.round(index.value * 10) / 10) : "—"}</td>
            <td className="r">{index?.change24h == null ? "—" : `${index.change24h >= 0 ? "+" : "−"}${Math.abs(index.change24h).toFixed(1)}%`}</td>
            <td><span className={item.status === "open" && !item.trading_halted ? "state ok" : "state warn"}>{item.trading_halted ? "정지" : item.status === "open" ? "열림" : "휴장"}</span></td></tr>;
        })}
      </tbody></table>{market && !market.length && <p className="empty">등록된 거래소가 없습니다.</p>}</div></div>
    </section>

    <section className="sec">
      <div className="lab"><small>03 / 최근 관리자 작업</small><p>가장 최근 5건입니다. 전체 기록은 작업 기록 탭에서 봅니다.</p>
        <a className="link-out" href="#overview" onClick={(event) => { event.preventDefault(); onPage("overview"); }}>작업 기록</a></div>
      <div className="sec-body"><div className="scroll-x"><table><thead><tr><th>시각</th><th>관리자</th><th>작업</th></tr></thead><tbody>
        {audit.map((entry) => <tr key={entry.auditId}><td>{date(entry.time)}</td><td className="mono">{entry.actorId}</td><td className="mono">{entry.action}</td></tr>)}
      </tbody></table>{!audit.length && <p className="empty">기록이 없습니다.</p>}</div></div>
    </section>

    <section className="sec">
      <div className="lab"><small>04 / 바로가기</small><p>모든 탭을 그룹별로 모았습니다.</p></div>
      <div className="sec-body idx">{NAV_GROUPS.map((group) => <div key={group.id}><h3>{group.label}</h3>
        {group.pages.filter((id) => labels.has(id)).map((id) => <a key={id} className="link-out" href={`#${id}`} onClick={(event) => { event.preventDefault(); onPage(id); }}>{labels.get(id)}</a>)}</div>)}</div>
    </section>
  </>;
}
