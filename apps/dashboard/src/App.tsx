import { useCallback, useEffect, useMemo, useState } from "react";
import { api, clearToken, type ApiUser } from "../../shared/api";
import { AuthPanel } from "../../shared/AuthPanel";

type Page = "overview" | "instruments" | "users" | "country-accounts" | "events" | "audit";
type Instrument = { code: string; name: string; type: string; price: number; status: string; exchange_id?: string };
type Overview = { activeUsers: number; activeInstruments: number; openOrders: number; market: { status: string; reason: string; trading_halted: number } | null };
type Audit = { actorId: string; action: string; payload: Record<string, unknown>; time: number };
type Citizen = { userId: string; nickname: string; nationality: string; cash: number; creditScore: number; status: string; role: string };
type Country = { countryId: string; currencyName: string; currencyCode: string };
type CountryAccount = { countryId: string; cash: number; status: string };
type Event = { event_id: number; asset_code: string | null; event_type: string; severity: number; starts_epoch: number; ends_epoch: number; created_by: string; target_mode: string; target_value: number };

const pages: { id: Page; label: string }[] = [
  { id: "overview", label: "운영 현황" },
  { id: "instruments", label: "종목 · 상품" },
  { id: "users", label: "사용자" },
  { id: "country-accounts", label: "국가 계정" },
  { id: "events", label: "시장 사건" },
  { id: "audit", label: "작업 기록" },
];
const number = (value: number) => new Intl.NumberFormat("ko-KR").format(value);
const date = (value: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "medium" }).format(value * 1000);

export default function App() {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [page, setPage] = useState<Page>("overview");
  const [health, setHealth] = useState("확인 중");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [citizens, setCitizens] = useState<Citizen[]>([]);
  const [countries, setCountries] = useState<Country[]>([]);
  const [countryAccounts, setCountryAccounts] = useState<CountryAccount[]>([]);
  const [countryId, setCountryId] = useState("");
  const [countryPassword, setCountryPassword] = useState("");
  const [countryPasswordConfirm, setCountryPasswordConfirm] = useState("");
  const [events, setEvents] = useState<Event[]>([]);
  const [query, setQuery] = useState("");
  const [eventType, setEventType] = useState("good");
  const [eventSeverity, setEventSeverity] = useState("5");
  const [eventDuration, setEventDuration] = useState("3600");
  const [eventAsset, setEventAsset] = useState("");

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
      if (page === "users") {
        const result = await api<{ users: Citizen[] }>(`/api/admin/users?search=${encodeURIComponent(query)}`);
        setCitizens(result.users);
      }
      if (page === "country-accounts") {
        const [countryResult, accountResult] = await Promise.all([
          api<{ countries: Country[] }>("/api/auth/countries"),
          api<{ accounts: CountryAccount[] }>("/api/admin/country-accounts"),
        ]);
        setCountries(countryResult.countries);
        setCountryAccounts(accountResult.accounts);
        setCountryId((current) => current || countryResult.countries[0]?.countryId || "");
      }
      if (page === "events") {
        const result = await api<{ events: Event[] }>("/api/admin/events");
        setEvents(result.events);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "요청을 처리하지 못했습니다.");
    }
  }, [page, user, query, countryId]);

  useEffect(() => {
    void fetch("/api/health").then((response) => response.json()).then((result) => {
      setHealth(result.python === "connected" ? "API · Python 연결됨" : "Python 연결 끊김");
    }).catch(() => setHealth("서버 연결 끊김"));
    void loadSession();
  }, [loadSession]);

  useEffect(() => {
    if (user) void refresh();
  }, [user, refresh]);

  const filteredInstruments = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return instruments.filter((item) => !term || `${item.code} ${item.name} ${item.type}`.toLocaleLowerCase().includes(term));
  }, [instruments, query]);

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

  async function editInstrument(item: Instrument) {
    const name = window.prompt("상품 이름", item.name);
    if (name === null) return;
    const price = window.prompt("현재 가격 (변경하지 않으려면 비우기)", String(item.price));
    if (price === null) return;
    const supply = window.prompt("발행량 (변경하지 않으려면 비우기)", "");
    if (supply === null) return;
    const changes = { name: name.trim(), price: price.trim(), supply: supply.trim() };
    if (!changes.name || (!changes.price && !changes.supply)) {
      setError("이름을 입력하고 가격 또는 발행량을 하나 이상 입력해주세요.");
      return;
    }
    if (!window.confirm(`${item.code} 상품 설정을 저장할까요?`)) return;
    setBusy(true);
    try {
      const result = await api<{ message: string }>(`/api/admin/instruments/${encodeURIComponent(item.code)}`, {
        method: "POST", body: JSON.stringify(changes),
      });
      setError(result.message);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "상품 설정을 변경하지 못했습니다.");
    } finally { setBusy(false); }
  }

  async function changeCitizen(citizen: Citizen, field: "status" | "role") {
    const changes = field === "status"
      ? { status: citizen.status === "active" ? "inactive" : "active" }
      : { role: citizen.role === "admin" ? "user" : "admin" };
    const value = Object.values(changes)[0];
    if (!window.confirm(`${citizen.nickname} (${citizen.userId})의 ${field === "status" ? "계정 상태" : "권한"}을 ${value}로 변경할까요?`)) return;
    setBusy(true);
    try {
      const result = await api<{ message: string }>(`/api/admin/users/${citizen.userId}`, { method: "POST", body: JSON.stringify(changes) });
      setError(result.message);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "사용자 설정을 변경하지 못했습니다.");
    } finally { setBusy(false); }
  }

  async function changeCash(citizen: Citizen) {
    const input = window.prompt(`${citizen.nickname}의 현금 잔액을 입력하세요 (NWX)`, String(citizen.cash));
    if (input === null) return;
    const cash = Number(input);
    if (!Number.isSafeInteger(cash) || cash < 0) {
      setError("현금은 0 이상의 안전한 정수로 입력해주세요.");
      return;
    }
    if (!window.confirm(`${citizen.nickname}의 현금을 ${number(cash)} NWX로 설정할까요?`)) return;
    setBusy(true);
    try {
      const result = await api<{ message: string }>(`/api/admin/users/${citizen.userId}`, {
        method: "POST", body: JSON.stringify({ cash }),
      });
      setError(result.message);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "현금 잔액을 변경하지 못했습니다.");
    } finally { setBusy(false); }
  }

  async function createEvent(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const percent = Number(eventSeverity);
    if (!window.confirm(`${eventAsset || "시장 전체"}에 ${eventType === "good" ? "호재" : "악재"} 사건을 적용할까요? 가격에 영향을 줍니다.`)) return;
    setBusy(true);
    try {
      const result = await api<{ message: string }>("/api/admin/events", {
        method: "POST", body: JSON.stringify({ eventType, severity: Math.round(percent), targetPercent: percent, durationSeconds: Number(eventDuration), assetCode: eventAsset.trim() || null }),
      });
      setError(result.message);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "시장 사건을 등록하지 못했습니다.");
    } finally { setBusy(false); }
  }

  async function createCountryAccount(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (countryPassword !== countryPasswordConfirm) {
      setError("비밀번호 확인이 일치하지 않습니다.");
      return;
    }
    if (countryPassword.length < 12 || countryPassword.length > 128 ||
        !/[A-Za-z]/.test(countryPassword) || !/\d/.test(countryPassword)) {
      setError("비밀번호는 영문과 숫자를 포함해 12~128자로 입력해주세요.");
      return;
    }
    if (!window.confirm(`${countryId} 국가 계정을 만들고 1,000,000 NWX를 지급할까요?`)) return;
    setBusy(true);
    try {
      await api("/api/admin/country-accounts", {
        method: "POST", body: JSON.stringify({ countryId, password: countryPassword }),
      });
      setCountryPassword("");
      setCountryPasswordConfirm("");
      setError(`${countryId} 국가 계정을 생성했습니다.`);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "국가 계정을 만들지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    clearToken();
    void api("/api/auth/logout", { method: "POST" });
    setUser(null);
    setOverview(null);
    setInstruments([]);
    setAudit([]);
  }

  if (!user) {
    return <AuthPanel app="dashboard" onAuthenticated={setUser} />;
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
          <section className="section-panel"><div className="section-head"><h2>운영 범위</h2><span className="muted">관리자 작업은 모두 API에서 역할을 재검증합니다.</span></div><p className="muted">시장 상태, 상품 이름·가격·발행량, 사용자 현금·권한·계정 상태, 시장 사건을 관리할 수 있습니다. 각 변경은 기존 Python 검증과 관리자 작업 기록을 거칩니다.</p></section>
        </>}
        {page === "instruments" && <section className="section-panel"><div className="section-head"><h2>종목 · 상품</h2><input className="search" placeholder="이름 또는 코드 검색" value={query} onChange={(event) => setQuery(event.target.value)} /></div><div className="table-wrap"><table><thead><tr><th>코드</th><th>이름</th><th>유형</th><th>가격</th><th>거래소</th><th>상태</th><th></th></tr></thead><tbody>{filteredInstruments.map((item) => <tr key={item.code}><td className="mono">{item.code}</td><td>{item.name}</td><td>{item.type}</td><td className="mono">{number(item.price)}</td><td>{item.exchange_id || "—"}</td><td><span className={item.status === "active" ? "state open" : "state paused"}>{item.status}</span></td><td className="user-actions"><button className="table-action" disabled={busy} onClick={() => void editInstrument(item)}>설정</button><button className="table-action" disabled={busy || item.status === "matured"} onClick={() => void changeInstrument(item)}>{item.status === "active" ? "중지" : "재개"}</button></td></tr>)}</tbody></table>{!filteredInstruments.length && <p className="empty">표시할 종목이 없습니다.</p>}</div></section>}
        {page === "users" && <section className="section-panel"><div className="section-head"><h2>사용자 계정</h2><input className="search" placeholder="이름 또는 Discord ID 검색" value={query} onChange={(event) => setQuery(event.target.value)} /></div><div className="table-wrap"><table><thead><tr><th>사용자</th><th>ID</th><th>국적</th><th>현금</th><th>신용</th><th>권한</th><th>상태</th><th></th></tr></thead><tbody>{citizens.map((citizen) => <tr key={citizen.userId}><td>{citizen.nickname}</td><td className="mono">{citizen.userId}</td><td>{citizen.nationality}</td><td className="mono">{number(citizen.cash)}</td><td>{number(citizen.creditScore)}</td><td>{citizen.role}</td><td>{citizen.status}</td><td className="user-actions"><button className="table-action" disabled={busy || citizen.userId === user.userId} onClick={() => void changeCash(citizen)}>현금</button><button className="table-action" disabled={busy || citizen.userId === user.userId} onClick={() => void changeCitizen(citizen, "status")}>{citizen.status === "active" ? "비활성" : "활성화"}</button><button className="table-action" disabled={busy || citizen.userId === user.userId} onClick={() => void changeCitizen(citizen, "role")}>{citizen.role === "admin" ? "관리자 해제" : "관리자 지정"}</button></td></tr>)}</tbody></table>{!citizens.length && <p className="empty">사용자를 찾지 못했습니다.</p>}</div></section>}
        {page === "country-accounts" && <>
          <section className="section-panel event-create"><div className="section-head"><h2>국가 계정 생성</h2><span className="muted">관리자 전용 · 시작 잔액 1,000,000 NWX</span></div><form onSubmit={(event) => void createCountryAccount(event)}>
            <label>국가 ID<select value={countryId} onChange={(event) => setCountryId(event.target.value)} required>{countries.map((country) => <option key={country.countryId} value={country.countryId}>{country.countryId}</option>)}</select></label>
            <label>비밀번호<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={countryPassword} onChange={(event) => setCountryPassword(event.target.value)} required /></label>
            <label>비밀번호 확인<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={countryPasswordConfirm} onChange={(event) => setCountryPasswordConfirm(event.target.value)} required /></label>
            <button className="primary" disabled={busy || !countries.length}>{busy ? "생성 중…" : "국가 계정 생성"}</button>
          </form><p className="muted">비밀번호는 영문과 숫자를 포함해 12~128자로 설정하세요. 계정은 HTS의 국가 계정 로그인에서 사용합니다.</p></section>
          <section className="section-panel"><div className="section-head"><h2>생성된 국가 계정</h2><span className="muted">국가당 계정 1개</span></div><div className="table-wrap"><table><thead><tr><th>국가 ID</th><th>HTS 잔액</th><th>상태</th></tr></thead><tbody>{countryAccounts.map((account) => <tr key={account.countryId}><td>{account.countryId}</td><td className="mono">{number(account.cash)} NWX</td><td>{account.status}</td></tr>)}</tbody></table>{!countryAccounts.length && <p className="empty">생성된 국가 계정이 없습니다.</p>}</div></section>
        </>}
        {page === "events" && <>
          <section className="section-panel event-create"><div className="section-head"><h2>시장 사건 등록</h2><span className="muted">기존 시장 사건 검증과 감사 기록 적용</span></div><form onSubmit={createEvent}><label>종류<select value={eventType} onChange={(event) => setEventType(event.target.value)}><option value="good">호재</option><option value="bad">악재</option></select></label><label>목표 변동률 (%)<input type="number" min="0" max="100" step="0.1" value={eventSeverity} onChange={(event) => setEventSeverity(event.target.value)} required /></label><label>지속 시간 (초)<input type="number" min="1" max="604800" value={eventDuration} onChange={(event) => setEventDuration(event.target.value)} required /></label><label>대상 종목 코드 (비우면 전체)<input value={eventAsset} onChange={(event) => setEventAsset(event.target.value)} placeholder="예: INZST001" /></label><button className="primary" disabled={busy}>사건 검토 · 등록</button></form></section>
          <section className="section-panel"><div className="section-head"><h2>최근 사건</h2><span className="muted">최근 50건</span></div><div className="table-wrap"><table><thead><tr><th>ID</th><th>대상</th><th>유형</th><th>강도</th><th>목표</th><th>시작</th><th>종료</th><th>관리자</th></tr></thead><tbody>{events.map((item) => <tr key={item.event_id}><td>#{item.event_id}</td><td>{item.asset_code || "시장 전체"}</td><td>{item.event_type === "good" ? "호재" : "악재"}</td><td>{item.severity}%</td><td>{item.target_mode === "price" ? `${number(item.target_value)} NWX` : `${number(item.target_value)}%`}</td><td>{date(item.starts_epoch)}</td><td>{date(item.ends_epoch)}</td><td className="mono">{item.created_by}</td></tr>)}</tbody></table>{!events.length && <p className="empty">등록된 사건이 없습니다.</p>}</div></section>
        </>}
        {page === "audit" && <section className="section-panel"><div className="section-head"><h2>최근 관리자 작업</h2><span className="muted">최근 50건</span></div><div className="table-wrap"><table><thead><tr><th>시각</th><th>관리자 ID</th><th>작업</th><th>세부 내용</th></tr></thead><tbody>{audit.map((entry, index) => <tr key={`${entry.time}-${index}`}><td>{date(entry.time)}</td><td className="mono">{entry.actorId}</td><td>{entry.action}</td><td className="audit-payload">{JSON.stringify(entry.payload)}</td></tr>)}</tbody></table>{!audit.length && <p className="empty">기록이 없습니다.</p>}</div></section>}
      </div>
    </main>
  </div>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="metric"><div className="metric-label">{label}</div><div className="metric-value">{value}</div></div>;
}
