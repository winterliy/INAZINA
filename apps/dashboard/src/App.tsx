import { useCallback, useEffect, useMemo, useState } from "react";
import { api, clearToken, getToken, signIn, type ApiUser } from "../../shared/api";

type Page = "overview" | "instruments" | "audit";
type Instrument = { code: string; name: string; type: string; price: number; status: string; exchange_id?: string };
type Overview = { activeUsers: number; activeInstruments: number; openOrders: number; market: { status: string; reason: string; trading_halted: number } | null };
type Audit = { actorId: string; action: string; payload: Record<string, unknown>; time: number };

const pages: { id: Page; label: string }[] = [
  { id: "overview", label: "운영 현황" },
  { id: "instruments", label: "종목 · 상품" },
  { id: "audit", label: "작업 기록" },
];
const number = (value: number) => new Intl.NumberFormat("ko-KR").format(value);
const date = (value: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "medium" }).format(value * 1000);

export default function App() {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [token, setToken] = useState("");
  const [page, setPage] = useState<Page>("overview");
  const [health, setHealth] = useState("확인 중");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [query, setQuery] = useState("");

  const loadSession = useCallback(async () => {
    try {
      const result = await api<{ user: ApiUser }>("/api/auth/session");
      setUser(result.user);
      setError("");
    } catch {
      setUser(null);
    }
  }, []);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const result = await api<{ status: string; python: string }>("/api/health");
      setHealth(result.python === "connected" ? "API · Python 연결됨" : "Python 연결 끊김");
      if (!user) return;
      if (user.role !== "admin") return;
      if (page === "overview") setOverview(await api<Overview>("/api/admin/overview"));
      if (page === "instruments") {
        const result = await api<{ instruments: Instrument[] }>("/api/admin/instruments");
        setInstruments(result.instruments);
      }
      if (page === "audit") {
        const result = await api<{ entries: Audit[] }>("/api/admin/audit");
        setAudit(result.entries);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "요청을 처리하지 못했습니다.");
    }
  }, [page, user]);

  useEffect(() => {
    void fetch("/api/health").then((response) => response.json()).then((result) => {
      setHealth(result.python === "connected" ? "API · Python 연결됨" : "Python 연결 끊김");
    }).catch(() => setHealth("서버 연결 끊김"));
    if (getToken()) void loadSession();
  }, [loadSession]);

  useEffect(() => {
    if (user) void refresh();
  }, [user, refresh]);

  const filteredInstruments = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return instruments.filter((item) => !term || `${item.code} ${item.name} ${item.type}`.toLocaleLowerCase().includes(term));
  }, [instruments, query]);

  async function login(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const current = await signIn(token);
      setUser(current);
      setToken("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "로그인에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function marketAction(action: string) {
    const labels: Record<string, string> = { close: "휴장", open: "개장", pause: "일시 정지", resume: "재개" };
    if (!window.confirm(`시장을 ${labels[action]} 상태로 변경할까요?`)) return;
    setBusy(true);
    try {
      const result = await api<{ message: string }>("/api/admin/market", {
        method: "POST", body: JSON.stringify({ action }),
      });
      setError(result.message);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "시장 상태를 변경하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function changeInstrument(item: Instrument) {
    const status = item.status === "active" ? "inactive" : "active";
    if (!window.confirm(`${item.name} (${item.code})을 ${status === "active" ? "재개" : "중지"}할까요?`)) return;
    setBusy(true);
    try {
      const result = await api<{ message: string }>(`/api/admin/instruments/${encodeURIComponent(item.code)}/status`, {
        method: "POST", body: JSON.stringify({ status }),
      });
      setError(result.message);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "상품 상태를 변경하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    clearToken();
    setUser(null);
    setOverview(null);
    setInstruments([]);
    setAudit([]);
  }

  if (!user) {
    return <main className="login-shell">
      <form className="login-form" onSubmit={login}>
        <div className="login-mark">I</div>
        <p className="eyebrow">INAZINA · LOCAL ADMIN</p>
        <h1>관리자 로그인</h1>
        <label htmlFor="token">접속 토큰</label>
        <input id="token" type="password" autoComplete="current-password" value={token} onChange={(event) => setToken(event.target.value)} required />
        <button className="primary" disabled={busy}>{busy ? "확인 중" : "로그인"}</button>
        {error && <p className="error">{error}</p>}
      </form>
    </main>;
  }

  if (user.role !== "admin") {
    return <main className="login-shell"><section className="login-form"><p className="eyebrow">INAZINA · ACCESS</p><h1>관리자 권한이 필요합니다</h1><p className="muted">현재 계정: {user.nickname}</p><button onClick={logout}>로그아웃</button></section></main>;
  }

  return <div className="admin-shell">
    <aside className="sidebar">
      <a className="brand" href="/"><span className="brand-mark">I</span><span><strong>INAZINA</strong><small>운영 Dashboard</small></span></a>
      <div className="nav-label">운영</div>
      <nav>{pages.map((item) => <button key={item.id} className={page === item.id ? "nav-item active" : "nav-item"} onClick={() => setPage(item.id)}>{item.label}</button>)}</nav>
      <div className="sidebar-foot">로컬 관리 콘솔<br /><span>127.0.0.1</span></div>
    </aside>
    <main className="admin-main">
      <header className="topbar"><div className="crumb">운영 <span>/</span> <b>{pages.find((item) => item.id === page)?.label}</b></div><div className="top-actions"><span className="connection"><i />{health}</span><span>{user.nickname} · 관리자</span><button className="text-button" onClick={logout}>로그아웃</button></div></header>
      <div className="content">
        <div className="page-head"><div><p className="eyebrow">LOCAL CONTROL</p><h1>{pages.find((item) => item.id === page)?.label}</h1><p className="subhead">실제 봇 데이터와 관리자 작업을 확인합니다.</p></div><button className="secondary" onClick={() => void refresh()} disabled={busy}>새로고침</button></div>
        {error && <div className="notice" role="status">{error}</div>}
        {page === "overview" && <>
          <section className="metric-grid">
            <Metric label="활성 사용자" value={overview ? number(overview.activeUsers) : "—"} />
            <Metric label="거래 중 종목·상품" value={overview ? number(overview.activeInstruments) : "—"} />
            <Metric label="미체결 주문" value={overview ? number(overview.openOrders) : "—"} />
            <Metric label="시장 상태" value={overview?.market?.status === "open" ? "개장" : overview?.market?.status ?? "—"} />
          </section>
          <section className="workspace-row">
            <div className="section-panel"><div className="section-head"><h2>시장 제어</h2><span className={overview?.market?.status === "open" ? "state open" : "state paused"}>{overview?.market?.reason || (overview?.market?.status === "open" ? "정상 운영" : "확인 필요")}</span></div><p className="muted">변경은 기존 Python 시장 제어 로직을 통해 기록됩니다.</p><div className="actions"><button onClick={() => void marketAction("open")} disabled={busy}>개장</button><button onClick={() => void marketAction("close")} disabled={busy}>휴장</button><button onClick={() => void marketAction("pause")} disabled={busy}>일시 정지</button><button onClick={() => void marketAction("resume")} disabled={busy}>재개</button></div></div>
            <div className="section-panel"><div className="section-head"><h2>서비스 연결</h2><span className="state open">정상</span></div><dl className="service-list"><div><dt>웹 API</dt><dd>127.0.0.1:4312</dd></div><div><dt>Python 데이터 서비스</dt><dd>127.0.0.1:4313</dd></div><div><dt>데이터베이스</dt><dd>SQLite · 읽기/기존 로직</dd></div></dl></div>
          </section>
          <section className="section-panel"><div className="section-head"><h2>운영 범위</h2><span className="muted">관리자 작업은 모두 API에서 역할을 재검증합니다.</span></div><p className="muted">종목 거래 상태와 시장 개폐를 조정할 수 있습니다. 금전·상품 내용 변경은 별도 작업 화면에서 제공할 예정입니다.</p></section>
        </>}
        {page === "instruments" && <section className="section-panel"><div className="section-head"><h2>종목 · 상품</h2><input className="search" placeholder="이름 또는 코드 검색" value={query} onChange={(event) => setQuery(event.target.value)} /></div><div className="table-wrap"><table><thead><tr><th>코드</th><th>이름</th><th>유형</th><th>가격</th><th>거래소</th><th>상태</th><th></th></tr></thead><tbody>{filteredInstruments.map((item) => <tr key={item.code}><td className="mono">{item.code}</td><td>{item.name}</td><td>{item.type}</td><td className="mono">{number(item.price)}</td><td>{item.exchange_id || "—"}</td><td><span className={item.status === "active" ? "state open" : "state paused"}>{item.status}</span></td><td><button className="table-action" disabled={busy || item.status === "matured"} onClick={() => void changeInstrument(item)}>{item.status === "active" ? "중지" : "재개"}</button></td></tr>)}</tbody></table>{!filteredInstruments.length && <p className="empty">표시할 종목이 없습니다.</p>}</div></section>}
        {page === "audit" && <section className="section-panel"><div className="section-head"><h2>최근 관리자 작업</h2><span className="muted">최근 50건</span></div><div className="table-wrap"><table><thead><tr><th>시각</th><th>관리자 ID</th><th>작업</th><th>세부 내용</th></tr></thead><tbody>{audit.map((entry, index) => <tr key={`${entry.time}-${index}`}><td>{date(entry.time)}</td><td className="mono">{entry.actorId}</td><td>{entry.action}</td><td className="audit-payload">{JSON.stringify(entry.payload)}</td></tr>)}</tbody></table>{!audit.length && <p className="empty">기록이 없습니다.</p>}</div></section>}
      </div>
    </main>
  </div>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="metric"><div className="metric-label">{label}</div><div className="metric-value">{value}</div></div>;
}
