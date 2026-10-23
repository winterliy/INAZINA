import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, clearToken, type ApiUser } from "../../shared/api";
import { AuthPanel } from "../../shared/AuthPanel";
import { Card, plain, type Ctx } from "./common";
import { ProductsPage } from "./ProductsPage";
import { UsersPage } from "./UsersPage";
import { ExchangesPage } from "./ExchangesPage";
import { CompaniesPage } from "./CompaniesPage";
import { WarningsCard } from "./WarningsCard";
import { IdentityCard } from "./IdentityCard";
import { FeedbackPage } from "./FeedbackPage";
import { LoginHistoryCard } from "./LoginHistoryCard";
import { LogCard } from "./LogCard";
import { BoardAdminPage } from "./BoardAdminPage";
import { DocumentsPage } from "./DocumentsPage";
import { CompliancePage } from "./CompliancePage";
import { BankJournalPage } from "./BankJournalPage";
import { COMMAND_FORMS, CommandForm } from "./CommandForm";

type Page = "overview" | "instruments" | "finance" | "exchanges" | "companies" | "users" | "feedback" | "board" | "documents" | "compliance" | "accounting" | "events" | "commands" | "database" | "audit";
type FundComponent = { assetCode: string; weight: number; assetClass: string };
type Instrument = { code: string; name: string; type: string; price: number; status: string; exchange_id?: string; fund_category?: string | null; components?: FundComponent[] };
type Exchange = { exchange_id: string; status: string; reason: string | null; trading_halted: number; updated_epoch: number | null; resume_epoch: number | null; circuit_stage: number | null };
type Overview = { activeUsers: number; activeInstruments: number; openOrders: number; market: Exchange[]; bankDeficits?: { issuer: string; account: string; cash: number; nationality: string }[] };
type Audit = { auditId: number; actorId: string; action: string; payload: Record<string, unknown>; time: number };
type Event = { event_id: number; asset_code: string | null; event_type: string; severity: number; starts_epoch: number; ends_epoch: number; created_by: string; target_mode: string | null; target_value: number | null; active: boolean };
type Command = { name: string; usage: string };
type NationalField = { label: string; default: string; required: boolean; long: boolean };
type NationalCommand = { name: string; fields: NationalField[] };
type HelpPage = { label: string; description: string; fields: { name: string; value: string }[] };
type Flow = { history_id: number; asset_code: string; anchor_price: number; target_price: number; change_percent: number; start_epoch: number; end_epoch: number; active: boolean };
type BotJob = { job_id: number; kind: string; actor_id: string; payload: string; status: "pending" | "running" | "done" | "failed"; result: string | null; created_epoch: number; updated_epoch: number };
type CommandResult = { command: string; arguments: string; message: string; success: boolean; time: number; national: boolean };
type Column = { name: string; type: string; notNull: boolean; primaryKey: boolean; default: string | null };
type TableInfo = { name: string; editable: boolean; columns: Column[] };
type Cell = string | number | null;
type Row = Record<string, Cell> & { __rowid__?: number };
type TablePage = { table: string; editable: boolean; columns: Column[]; total: number; offset: number; pageSize: number; rows: Row[] };

const pages: { id: Page; label: string }[] = [
  { id: "overview", label: "운영 현황" },
  { id: "instruments", label: "종목 · 상품" },
  { id: "finance", label: "금융 상품" },
  { id: "exchanges", label: "거래소" },
  { id: "companies", label: "법인 자본" },
  { id: "users", label: "사용자 · 국가" },
  { id: "feedback", label: "문의·신고" },
  { id: "board", label: "게시판 관리" },
  { id: "compliance", label: "KYC · AML" },
  { id: "accounting", label: "은행 원장" },
  { id: "documents", label: "문서 · 약관" },
  { id: "events", label: "시장 사건" },
  { id: "commands", label: "관리자 명령어" },
  { id: "database", label: "DB 편집" },
  { id: "audit", label: "작업 기록" },
];
const subtitles: Record<Page, string> = {
  overview: "시장 상태와 서비스 연결을 확인하고 거래소를 제어합니다.",
  instruments: "주식·자산·ETF·선물·채권·펀드·원자재·옵션·ELS를 생성·수정·중지합니다.",
  finance: "저축·대출·통화·외환통장 상품을 생성·수정·중지합니다.",
  exchanges: "거래소별 개장·휴장·서킷브레이커와 소속 국가·지수를 관리합니다.",
  companies: "법인 현금·보유자산·통장·대출을 보고 법인 계정으로 매수·매도·송금·대출을 실행합니다.",
  feedback: "공식 사이트의 Contact·Report로 들어온 문의와 신고를 확인하고 처리합니다.",
  documents: "약관·처리방침·규칙·도움말과 Discord 등록 안내문을 고치고 확정합니다.",
  board: "HTS 게시판의 신고를 처리하고 글을 숨기거나 고정합니다. 익명 글의 실제 작성자와 IP를 확인할 수 있습니다.",
  compliance: "국적별 KYC 검토 상태와 AML 시뮬레이터 경보·규칙을 관리합니다.",
  accounting: "금융기관의 이중분개 시산 잔액·불균형·결과 원장 대사를 조회합니다.",
  users: "사용자와 국가, 국가 계정의 모든 설정을 관리합니다.",
  events: "호재·악재 사건을 등록하고 최근 사건을 확인합니다.",
  commands: "Discord의 모든 $ 명령어와 도움말을 같은 검증과 기록으로 실행합니다.",
  database: "DB Browser에서 하던 설정 편집을 감사 기록과 함께 처리합니다.",
  audit: "모든 관리자 작업과 관리자 계정 로그인의 최근 기록입니다.",
};
const number = (value: number) => new Intl.NumberFormat("ko-KR").format(value);
const date = (value: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "medium" }).format(value * 1000);
const marketStatusName: Record<string, string> = { open: "개장", closed: "휴장", paused: "일시 정지", halted: "거래 정지" };
const message = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;

// DB Browser semantics: NULL is explicit, numeric columns store numbers.
function parseCell(input: string, column: Column | undefined): Cell {
  if (input === "NULL") return null;
  const numeric = /INT|REAL|NUM|FLOA|DOUB|DEC/i.test(column?.type ?? "");
  if (numeric && input.trim() !== "" && Number.isFinite(Number(input))) return Number(input);
  return input;
}
const showCell = (value: Cell | undefined) => value === null || value === undefined ? "NULL" : String(value);

export default function App() {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [checked, setChecked] = useState(false);
  const [page, setPage] = useState<Page>("overview");
  const [health, setHealth] = useState("확인 중");
  const [connected, setConnected] = useState(false);
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [unreadFeedback, setUnreadFeedback] = useState(0);
  const unreadSeen = useRef(0);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [flows, setFlows] = useState<Flow[]>([]);
  const [query, setQuery] = useState("");
  const [eventType, setEventType] = useState("good");
  const [eventSeverity, setEventSeverity] = useState("5");
  const [eventDuration, setEventDuration] = useState("3600");
  const [eventAsset, setEventAsset] = useState("");
  const [eventTarget, setEventTarget] = useState("");
  const [commands, setCommands] = useState<Command[]>([]);
  const [nationalCommands, setNationalCommands] = useState<NationalCommand[]>([]);
  const [discordOnly, setDiscordOnly] = useState<string[]>([]);
  const [commandScope, setCommandScope] = useState<"admin" | "national" | "help">("admin");
  const [helpPages, setHelpPages] = useState<HelpPage[]>([]);
  const [helpPage, setHelpPage] = useState(0);
  const [jobs, setJobs] = useState<BotJob[]>([]);
  const [nationalCommand, setNationalCommand] = useState("");
  const [nationalValues, setNationalValues] = useState<string[]>([]);
  const [creationHelp, setCreationHelp] = useState("");
  const [command, setCommand] = useState("");
  const [commandArgs, setCommandArgs] = useState("");
  const [commandResults, setCommandResults] = useState<CommandResult[]>([]);
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [table, setTable] = useState("settings");
  const [tablePage, setTablePage] = useState<TablePage | null>(null);
  const [tableOffset, setTableOffset] = useState(0);
  const [edits, setEdits] = useState<Record<number, Record<string, string>>>({});
  const [newRow, setNewRow] = useState<Record<string, string> | null>(null);

  const say = useCallback((text: string, error = false) => setNotice({ text, error }), []);

  const loadSession = useCallback(async () => {
    try {
      const result = await api<{ user: ApiUser }>("/api/auth/session");
      setUser(result.user);
    } catch {
      setUser(null);
    } finally {
      setChecked(true);
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const result = await api<{ status: string; python: string }>("/api/health");
      setConnected(result.python === "connected");
      setHealth(result.python === "connected" ? "API · Python 연결됨" : "Python 연결 끊김");
    } catch {
      setConnected(false);
      setHealth("서버 연결 끊김");
    }
    if (!user || user.role !== "admin") return;
    try {
      if (page === "overview") setOverview(await api<Overview>("/api/admin/overview"));
      if (page === "audit") setAudit((await api<{ entries: Audit[] }>("/api/admin/audit")).entries);
      if (page === "events") {
        const [eventResult, instrumentResult] = await Promise.all([
          api<{ events: Event[]; flows?: Flow[] }>("/api/admin/events"),
          api<{ instruments: Instrument[] }>("/api/admin/instruments"),
        ]);
        setEvents(eventResult.events);
        setFlows(eventResult.flows ?? []);
        setInstruments(instrumentResult.instruments);
      }
      if (page === "commands" && !commands.length) {
        const result = await api<{ commands: Command[]; national: NationalCommand[]; discordOnly: string[]; creationHelp: string }>("/api/admin/commands");
        setCommands(result.commands);
        setNationalCommands(result.national);
        setDiscordOnly(result.discordOnly);
        setCreationHelp(result.creationHelp);
        setCommand((current) => current || result.commands[0]?.name || "");
        setNationalCommand((current) => current || result.national[0]?.name || "");
      }
      if (page === "commands" && commandScope === "help" && !helpPages.length) setHelpPages((await api<{ pages: HelpPage[] }>("/api/admin/help")).pages);
      if (page === "commands" && commandScope === "national") setJobs((await api<{ jobs: BotJob[] }>("/api/admin/bot-jobs")).jobs);
      if (page === "database") {
        const [tableList, rows] = await Promise.all([
          tables.length ? Promise.resolve({ tables }) : api<{ tables: TableInfo[] }>("/api/admin/db/tables"),
          api<TablePage>(`/api/admin/db/tables/${encodeURIComponent(table)}?${new URLSearchParams({ search: query, offset: String(tableOffset) })}`),
        ]);
        setTables(tableList.tables);
        setTablePage(rows);
        setEdits({});
      }
    } catch (cause) {
      say(message(cause, "요청을 처리하지 못했습니다."), true);
    }
  }, [page, user, query, commands.length, tables, table, tableOffset, commandScope, helpPages.length]);

  useEffect(() => { void loadSession(); }, [loadSession]);
  useEffect(() => { if (user) void refresh(); }, [user, refresh]);
  // Official-site Contact/Report alerts: refresh the unread count every minute.
  const loadUnread = useCallback(async () => {
    try {
      const result = await api<{ unread: number }>("/api/admin/feedback?status=new");
      if (result.unread > unreadSeen.current) setNotice({ text: `🔔 공식 사이트에서 새 문의·신고가 ${result.unread - unreadSeen.current}건 왔습니다.`, error: false });
      unreadSeen.current = result.unread;
      setUnreadFeedback(result.unread);
    } catch { /* the badge is best effort */ }
  }, []);
  useEffect(() => {
    if (!user) return;
    void loadUnread();
    const timer = window.setInterval(() => void loadUnread(), 60_000);
    return () => window.clearInterval(timer);
  }, [user, loadUnread]);
  useEffect(() => { setQuery(""); setNotice(null); }, [page]);
  useEffect(() => { setTableOffset(0); setNewRow(null); }, [table, query]);

  const selectedCommand = commands.find((item) => item.name === command);
  const selectedNational = nationalCommands.find((item) => item.name === nationalCommand);
  const eventInstrument = instruments.find((item) => item.code === eventAsset.trim());
  useEffect(() => { setNationalValues(selectedNational?.fields.map((field) => field.default) ?? []); }, [selectedNational]);
  const tableInfo = tables.find((item) => item.name === table);

  async function run<T>(work: () => Promise<T>, fallback: string, done?: (result: T) => void) {
    setBusy(true);
    try {
      const result = await work();
      done?.(result);
      await refresh();
    } catch (cause) {
      say(message(cause, fallback), true);
    } finally {
      setBusy(false);
    }
  }

  function openCommand(name: string, args: string, national = false) {
    setPage("commands");
    if (national) {
      setCommandScope("national");
      setNationalCommand(name);
    } else {
      setCommandScope("admin");
      setCommand(name);
      setCommandArgs(args);
    }
  }

  const ctx: Ctx = { busy, run, say, query, openCommand };

  function marketAction(action: string, exchangeId?: string, stage?: number) {
    const labels: Record<string, string> = { close: "휴장", open: "개장", pause: "일시 정지", resume: "재개", circuit: `서킷브레이커 ${stage}단계`, sidecar: "사이드카(5분)" };
    if (!window.confirm(`${exchangeId ?? "전체 거래소"}에 ${labels[action]}을(를) 적용할까요?`)) return;
    void run(() => api<{ message: string; success: boolean }>("/api/admin/market", { method: "POST", body: JSON.stringify({ action, exchangeId, stage }) }),
      "시장 상태를 변경하지 못했습니다.", (result) => say(plain(result.message), result.success === false));
  }

  function createEvent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const percent = Number(eventSeverity);
    const asset = eventAsset.trim();
    const target = eventTarget.trim() ? Number(eventTarget) : null;
    // Same rules as the Discord $사건 form: a target price needs one asset and
    // must lie above the current price for good news, below it for bad news.
    if (target !== null) {
      if (!Number.isSafeInteger(target) || target < 1) { say("목표가는 1 이상의 정수로 입력해주세요.", true); return; }
      if (!asset) { say("목표가를 지정하려면 대상 종목 코드를 입력해주세요.", true); return; }
      if (eventInstrument && (eventType === "good" ? target < eventInstrument.price : target > eventInstrument.price)) {
        say(`${eventType === "good" ? "호재 목표가는 현재가 이상" : "악재 목표가는 현재가 이하"}로 입력해주세요. (현재가 ${number(eventInstrument.price)})`, true);
        return;
      }
    }
    const goal = target !== null ? `목표가 ${number(target)}` : `변동률 ${percent}%`;
    if (!window.confirm(`${asset || "시장 전체"}에 ${eventType === "good" ? "호재" : "악재"} 사건(${goal})을 적용할까요? 가격에 영향을 줍니다.`)) return;
    void run(() => api<{ success: boolean; message: string }>("/api/admin/events", {
      method: "POST", body: JSON.stringify({ eventType, severity: Math.round(percent), targetPercent: percent, targetPrice: target, durationSeconds: Number(eventDuration), assetCode: asset || null }),
    }), "시장 사건을 등록하지 못했습니다.", (result) => { say(result.message, !result.success); if (result.success) setEventTarget(""); });
  }

  function runCommand(args: string) {
    const text = `$${command}${args ? ` ${args}` : ""}`;
    if (!window.confirm(`다음 명령어를 실행할까요?\n\n${text}`)) return;
    void run(() => api<{ message: string; success: boolean }>("/api/admin/commands", { method: "POST", body: JSON.stringify({ command, arguments: args }) }),
      "명령어를 실행하지 못했습니다.", (result) => {
        setCommandResults((items) => [{ command, arguments: args, message: result.message, success: result.success, time: Date.now() / 1000, national: false }, ...items].slice(0, 20));
        if (result.success) setCommandArgs("");
      });
  }

  function runNationalCommand(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedNational) return;
    const summary = selectedNational.fields.map((field, index) => `${field.label}: ${nationalValues[index] || "—"}`).join("\n");
    if (!window.confirm(`국가(국고) 명의로 $${nationalCommand}을(를) 실행할까요?\n\n${summary}`)) return;
    void run(() => api<{ message: string; success: boolean }>("/api/admin/commands", { method: "POST", body: JSON.stringify({ national: true, command: nationalCommand, values: nationalValues }) }),
      "명령어를 실행하지 못했습니다.", (result) => {
        setCommandResults((items) => [{ command: nationalCommand, arguments: nationalValues.filter(Boolean).join(" "), message: result.message, success: result.success, time: Date.now() / 1000, national: true }, ...items].slice(0, 20));
      });
  }

  function saveRow(row: Row) {
    const rowid = row.__rowid__!;
    const changes = edits[rowid] ?? {};
    const values = Object.fromEntries(Object.entries(changes).map(([name, input]) => [name, parseCell(input, tableInfo?.columns.find((column) => column.name === name))]));
    if (!Object.keys(values).length) return;
    const summary = Object.entries(values).map(([name, value]) => `${name}: ${showCell(row[name])} → ${showCell(value)}`).join("\n");
    if (!window.confirm(`${table} #${rowid} 행을 저장할까요?\n\n${summary}`)) return;
    void run(() => api(`/api/admin/db/tables/${encodeURIComponent(table)}/rows/${rowid}`, { method: "POST", body: JSON.stringify({ values }) }),
      "행을 저장하지 못했습니다.", () => say(`${table} #${rowid} 행을 저장했습니다.`));
  }

  function deleteRow(row: Row) {
    const rowid = row.__rowid__!;
    if (!window.confirm(`${table} #${rowid} 행을 삭제할까요? 되돌릴 수 없으며 작업 기록에 남습니다.`)) return;
    void run(() => api(`/api/admin/db/tables/${encodeURIComponent(table)}/rows/${rowid}/delete`, { method: "POST" }),
      "행을 삭제하지 못했습니다.", () => say(`${table} #${rowid} 행을 삭제했습니다.`));
  }

  function insertRow(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = Object.fromEntries(Object.entries(newRow ?? {}).filter(([, input]) => input !== "")
      .map(([name, input]) => [name, parseCell(input, tableInfo?.columns.find((column) => column.name === name))]));
    if (!Object.keys(values).length) {
      say("한 개 이상의 열 값을 입력해주세요.", true);
      return;
    }
    if (!window.confirm(`${table}에 새 행을 추가할까요?`)) return;
    void run(() => api(`/api/admin/db/tables/${encodeURIComponent(table)}/rows`, { method: "POST", body: JSON.stringify({ values }) }),
      "행을 추가하지 못했습니다.", () => { setNewRow(null); say(`${table}에 행을 추가했습니다.`); });
  }

  function logout() {
    clearToken();
    void api("/api/auth/logout", { method: "POST" });
    setUser(null);
    setOverview(null);
    setInstruments([]);
    setAudit([]);
  }

  if (!checked) return <Stage><main className="center-shell"><p className="loading">불러오는 중…</p></main></Stage>;
  if (!user) return <Stage><AuthPanel app="dashboard" onAuthenticated={setUser} /></Stage>;
  if (user.role !== "admin") {
    return <Stage><main className="center-shell"><section className="glass card access-card"><p className="eyebrow">INAZINA · ACCESS</p><h1>관리자 권한이 필요합니다</h1><p className="cap">현재 계정: {user.nickname}</p><button className="btn primary" onClick={logout}>로그아웃</button></section></main></Stage>;
  }

  const current = pages.find((item) => item.id === page)!;
  const searchable = page === "instruments" || page === "finance" || page === "exchanges" || page === "companies" || page === "users" || page === "feedback" || page === "board" || page === "database";
  const openExchanges = overview ? overview.market.filter((item) => item.status === "open" && !item.trading_halted).length : 0;

  return <Stage>
    <div className="shell">
      <header className="gnav">
        <div className="gnav-in">
          <div className="brand"><span className="brand-mark">I</span><strong>INAZINA</strong><small>운영 Dashboard</small></div>
          <div className="gnav-meta">
            <span className="status"><span className={connected ? "dot ok" : "dot"} />{health}</span>
            <span className="mono dim">127.0.0.1</span>
            <span>{user.nickname} · 관리자</span>
            <button type="button" onClick={() => void refresh()} disabled={busy}>새로고침</button>
            <button type="button" onClick={logout}>로그아웃</button>
          </div>
        </div>
      </header>
      <nav className="subnav" aria-label="관리 메뉴">
        <div className="subnav-in">
          <span className="subnav-title">관리</span>
          {pages.map((item) => <button key={item.id} type="button" className="subnav-item" aria-current={page === item.id ? "page" : undefined} onClick={() => setPage(item.id)}>
            {item.label}
            {item.id === "feedback" && unreadFeedback > 0 && <span className="nav-badge" aria-label={`새 알림 ${unreadFeedback}건`}>{unreadFeedback}</span>}
          </button>)}
        </div>
      </nav>
      <main className="content">
        {page === "overview"
          ? <section className="hero">
            <div className="hero-copy"><p className="eyebrow">INAZINA 로컬 관리 콘솔</p><h1 className="title">{current.label}</h1><p className="sub">{subtitles[page]}</p></div>
            <div className="hero-figure"><div className="hero-number">{overview ? <>{openExchanges}<span> / </span>{overview.market.length}</> : "—"}</div><p>개장 중 거래소</p></div>
          </section>
          : <section className="page-head">
            <div className="page-copy"><h1 className="title">{current.label}</h1><p className="sub">{subtitles[page]}</p></div>
            {searchable && <label className="search">
              <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></svg>
              <input type="search" aria-label="검색" placeholder={page === "users" ? "이름 또는 ID" : page === "database" ? "모든 열에서 검색" : "이름 또는 코드"} value={query} onChange={(event) => setQuery(event.target.value)} />
            </label>}
          </section>}
        {notice && <div className={notice.error ? "notice error" : "notice"} role={notice.error ? "alert" : "status"}><span>{notice.text}</span><button type="button" className="ghost" aria-label="알림 닫기" onClick={() => setNotice(null)}>×</button></div>}

        {page === "overview" && <>
          <section className="spec-strip" aria-label="주요 지표">
            <Metric label="활성 사용자" value={overview ? number(overview.activeUsers) : "—"} />
            <Metric label="거래 중 종목·상품" value={overview ? number(overview.activeInstruments) : "—"} />
            <Metric label="미체결 주문" value={overview ? number(overview.openOrders) : "—"} />
          </section>
          {!!overview?.bankDeficits?.length && <section className="glass card mb rise warn-card" role="alert"><div className="card-head"><div><h2>⚠️ 은행 본사 계좌 적자</h2><p className="cap">적자는 허용되지만 자본 확충을 검토하세요. 처음 적자가 될 때 관리자에게 Discord DM으로도 알립니다.</p></div></div>
            <div className="scroll-x"><table><thead><tr><th>은행</th><th>본사 계좌</th><th>국가</th><th className="r">잔액</th></tr></thead><tbody>{overview.bankDeficits.map((bank) => <tr key={bank.issuer}><td>{bank.issuer}</td><td className="mono">{bank.account}</td><td>{bank.nationality}</td><td className="r mono">{number(Math.round(bank.cash))}</td></tr>)}</tbody></table></div></section>}
          <section className="section">
            <div className="section-head"><div><h2>시장 제어</h2><p className="cap">전체 또는 거래소별로 개장·휴장·정지합니다. 기존 Python 시장 제어와 기록을 거칩니다.</p></div>
              <div className="actions"><button className="btn primary" disabled={busy} onClick={() => marketAction("open")}>전체 개장</button><button className="outline" disabled={busy} onClick={() => marketAction("close")}>전체 휴장</button><button className="outline" disabled={busy} onClick={() => marketAction("pause")}>일시 정지</button><button className="outline" disabled={busy} onClick={() => marketAction("resume")}>재개</button></div></div>
            <div className="exchange-grid">
              {(overview?.market ?? []).map((item) => <article key={item.exchange_id} className="exchange-tile">
                <div className="exchange-top"><h3>{item.exchange_id}</h3><span className={item.status === "open" && !item.trading_halted ? "state ok" : "state warn"}>{item.trading_halted ? "거래 정지" : marketStatusName[item.status] ?? item.status}</span></div>
                <dl className="exchange-facts"><div><dt>사유</dt><dd>{item.reason || "—"}</dd></div><div><dt>서킷</dt><dd>{item.circuit_stage ? `${item.circuit_stage}단계` : "—"}</dd></div><div><dt>변경 시각</dt><dd>{item.updated_epoch ? date(item.updated_epoch) : "—"}</dd></div></dl>
                <div className="exchange-actions">{item.status === "open" ? <button className="tile-btn" disabled={busy} onClick={() => marketAction("close", item.exchange_id)}>휴장</button> : <button className="tile-btn" disabled={busy} onClick={() => marketAction("open", item.exchange_id)}>개장</button>}{item.trading_halted ? <button className="tile-btn" disabled={busy} onClick={() => marketAction("resume", item.exchange_id)}>재개</button> : <button className="tile-btn" disabled={busy} onClick={() => marketAction("pause", item.exchange_id)}>정지</button>}<button className="tile-btn" disabled={busy || item.status === "closed"} onClick={() => marketAction("sidecar", item.exchange_id)}>사이드카</button><select className="tile-select" aria-label={`${item.exchange_id} 서킷브레이커`} value="" disabled={busy || item.status === "closed"} onChange={(event) => { if (event.target.value) marketAction("circuit", item.exchange_id, Number(event.target.value)); }}><option value="">서킷…</option><option value="1">1단계 (20분)</option><option value="2">2단계 (20분)</option><option value="3">3단계 (다음 날)</option></select></div>
              </article>)}
            </div>
            {overview && !overview.market.length && <p className="empty">등록된 거래소가 없습니다.</p>}
          </section>
          <section className="service-tile">
            <div className="section-head"><h2>서비스 연결</h2><p className="cap">모든 서비스는 이 컴퓨터(127.0.0.1)에서만 열립니다.</p></div>
            <dl className="service-grid"><div><dt>웹 API</dt><dd className="mono">:4312</dd></div><div><dt>Python 데이터 서비스</dt><dd className="mono">:4313</dd></div><div><dt>데이터베이스</dt><dd className="mono">inazina.db</dd></div><div><dt>상태</dt><dd className="state-line"><span className={connected ? "dot ok" : "dot"} />{health}</dd></div></dl>
          </section>
        </>}

        {page === "instruments" && <><ProductsPage group="market" ctx={ctx} /><WarningsCard ctx={ctx} /></>}
        {page === "finance" && <ProductsPage group="finance" ctx={ctx} />}
        {page === "exchanges" && <ExchangesPage ctx={ctx} />}
        {page === "companies" && <CompaniesPage ctx={ctx} />}
        {page === "board" && <BoardAdminPage ctx={ctx} />}
        {page === "documents" && <DocumentsPage ctx={ctx} />}
        {page === "compliance" && <CompliancePage ctx={ctx} />}
        {page === "accounting" && <BankJournalPage ctx={ctx} />}
        {page === "feedback" && <FeedbackPage ctx={ctx} onChange={() => void loadUnread()} />}
        {page === "users" && <UsersPage ctx={ctx} currentUserId={user.userId} />}
        {page === "events" && <>
          <section className="glass card mb rise"><div className="card-head"><div><h2>시장 사건 등록</h2><p className="cap">기존 시장 사건 검증과 감사 기록을 적용합니다.</p></div></div><form className="form-grid" onSubmit={createEvent}>
            <label>종류<select value={eventType} onChange={(event) => setEventType(event.target.value)}><option value="good">호재</option><option value="bad">악재</option></select></label>
            <label>{eventTarget.trim() ? "변동성 (%)" : "목표 변동률 (%)"}<input type="number" min="0" max="100" step="0.1" value={eventSeverity} onChange={(event) => setEventSeverity(event.target.value)} required /></label>
            <label>지속 시간 (초)<input type="number" min="1" max="604800" value={eventDuration} onChange={(event) => setEventDuration(event.target.value)} required /></label>
            <label>대상 종목 코드 (비우면 전체)<input list="event-assets" value={eventAsset} onChange={(event) => setEventAsset(event.target.value)} placeholder="예: INZST001" /></label>
            <label>목표가 (선택 · 개별 종목)<input type="number" min="1" step="1" inputMode="numeric" value={eventTarget} disabled={!eventAsset.trim()} onChange={(event) => setEventTarget(event.target.value)} placeholder={eventInstrument ? `현재가 ${number(eventInstrument.price)}` : "종목을 먼저 입력"} /></label>
            <button className="btn primary" disabled={busy}>사건 등록</button>
            <datalist id="event-assets">{instruments.filter((item) => item.status === "active").map((item) => <option key={item.code} value={item.code}>{item.name} · {number(item.price)}</option>)}</datalist>
          </form><p className="hint">목표가를 비우면 변동률만큼 오르거나(호재) 내립니다. 목표가를 넣으면 그 가격을 향해 움직이고, 변동률은 움직이는 동안의 변동성으로 쓰입니다. 호재는 현재가 이상, 악재는 현재가 이하로 입력하세요.{eventInstrument && ` · ${eventInstrument.name} 현재가 ${number(eventInstrument.price)}`}</p></section>
          <LogCard ctx={ctx} log="events" title="최근 사건" caption="관리자가 등록한 사건 · 최근 50건" rows={events} idOf={(item) => item.event_id} deletable={(item) => !item.active}
            keepNote="진행 중인 사건은 남습니다." empty="등록된 사건이 없습니다." onChanged={() => void refresh()}
            head={<><th>ID</th><th>대상</th><th>유형</th><th>강도</th><th>목표</th><th>시작</th><th>종료</th><th>관리자</th></>}
            row={(item) => <><td>#{item.event_id}{item.active && <span className="tag ok inline">진행 중</span>}</td><td>{item.asset_code || "시장 전체"}</td><td><span className={item.event_type === "good" ? "tag up" : "tag down"}>{item.event_type === "good" ? "호재" : "악재"}</span></td><td>{item.severity}%</td><td>{item.target_value === null ? "—" : item.target_mode === "price" ? number(item.target_value) : `${number(item.target_value)}%`}</td><td>{date(item.starts_epoch)}</td><td>{date(item.ends_epoch)}</td><td className="mono">{item.created_by}</td></>} />
          <LogCard ctx={ctx} log="flows" title="알고리즘 시장 흐름" caption="시세 엔진이 종목마다 정한 자동 목표가 · 최근 100건 (최대 5,000건 보관)" rows={flows} idOf={(item) => item.history_id}
            empty="기록된 흐름이 없습니다. 시세 엔진이 새 목표가를 정하면 여기에 쌓입니다." onChanged={() => void refresh()}
            head={<><th>#</th><th>종목</th><th>방향</th><th className="r">기준가</th><th className="r">목표가</th><th className="r">변동</th><th>시작</th><th>종료</th></>}
            row={(item) => <><td className="mono">#{item.history_id}{item.active && <span className="tag ok inline">진행 중</span>}</td><td className="mono">{item.asset_code}</td><td><span className={item.change_percent >= 0 ? "tag up" : "tag down"}>{item.change_percent >= 0 ? "상승" : "하락"}</span></td><td className="r mono">{number(Math.round(item.anchor_price * 100) / 100)}</td><td className="r mono">{number(Math.round(item.target_price * 100) / 100)}</td><td className="r mono">{item.change_percent >= 0 ? "+" : ""}{item.change_percent.toFixed(2)}%</td><td>{date(item.start_epoch)}</td><td>{date(item.end_epoch)}</td></>} />
        </>}

        {page === "commands" && <>
          <div className="segment-tabs mb" role="tablist" aria-label="명령어 종류"><button role="tab" aria-selected={commandScope === "admin"} className={commandScope === "admin" ? "active" : ""} onClick={() => setCommandScope("admin")}>관리자 명령어 <span>{commands.length}</span></button><button role="tab" aria-selected={commandScope === "national"} className={commandScope === "national" ? "active" : ""} onClick={() => setCommandScope("national")}>국가 명의 <span>{nationalCommands.length}</span></button><button role="tab" aria-selected={commandScope === "help"} className={commandScope === "help" ? "active" : ""} onClick={() => setCommandScope("help")}>도움말 ($도움말)</button></div>
          {commandScope === "help" && <section className="glass card mb rise"><div className="card-head"><div><h2>{helpPages[helpPage]?.label ?? "도움말"}</h2><p className="cap">Discord $도움말과 같은 내용입니다.</p></div><div className="actions"><select aria-label="도움말 페이지" value={helpPage} onChange={(event) => setHelpPage(Number(event.target.value))}>{helpPages.map((item, index) => <option key={item.label} value={index}>{item.label}</option>)}</select></div></div>
            {helpPages[helpPage] && <div className="help-page"><pre>{plain(helpPages[helpPage].description)}</pre>{helpPages[helpPage].fields.map((field) => <div key={field.name} className="help-field"><h3>{field.name}</h3><pre>{plain(field.value)}</pre></div>)}</div>}
            {!helpPages.length && <p className="empty">도움말을 불러오는 중입니다.</p>}
          </section>}
          {commandScope === "national" && <IdentityCard ctx={ctx} />}
          {commandScope === "national" && <section className="glass card mb rise"><div className="card-head"><div><h2>국가(국고) 명의 실행</h2><p className="cap">Discord `$매수`·`$송금` 등과 같이 국고 계정으로 실행하고 작업 기록에 남깁니다. {discordOnly.length > 0 && `${discordOnly.map((name) => `$${name}`).join(", ")}은(는) Discord 메시지를 보내야 해서 Discord에서만 사용할 수 있습니다.`}</p></div></div>
            <form className="form-grid" onSubmit={runNationalCommand}>
              <label>명령어<select value={nationalCommand} onChange={(event) => setNationalCommand(event.target.value)}>{nationalCommands.map((item) => <option key={item.name} value={item.name}>${item.name}</option>)}</select></label>
              {selectedNational?.fields.map((field, index) => <label key={`${nationalCommand}-${field.label}`} className={field.long ? "span-wide" : undefined}>{field.label}{!field.required && <small>선택</small>}<input className={field.long ? "mono" : undefined} value={nationalValues[index] ?? ""} required={field.required} onChange={(event) => setNationalValues(nationalValues.map((value, position) => position === index ? event.target.value : value))} /></label>)}
              <button className="btn primary" disabled={busy || !selectedNational}>{busy ? "실행 중…" : selectedNational?.fields.length ? "실행" : "조회"}</button>
            </form>
          </section>}
          {commandScope === "national" && <LogCard ctx={ctx} log="jobs" title="Discord 봇 처리 대기열" caption="$주총처럼 Discord 메시지를 보내야 하는 작업은 실행 중인 봇이 처리합니다. 봇이 꺼져 있으면 대기 상태로 남습니다."
            rows={jobs} idOf={(job) => job.job_id} deletable={(job) => job.status === "done" || job.status === "failed"} keepNote="대기·처리 중인 작업은 남습니다."
            empty="요청한 작업이 없습니다." onChanged={() => void refresh()}
            head={<><th>#</th><th>작업</th><th>상태</th><th>요청</th><th>결과</th><th>시각</th></>}
            row={(job) => <><td className="mono">#{job.job_id}</td><td>{job.kind === "shareholder_meeting" ? "$주총" : job.kind}</td><td><span className={job.status === "done" ? "tag ok" : job.status === "failed" ? "tag warn" : "tag"}>{{ pending: "대기", running: "처리 중", done: "완료", failed: "실패" }[job.status]}</span></td><td className="mono audit-payload">{job.payload}</td><td className="audit-payload">{job.result ? plain(job.result) : "—"}</td><td>{date(job.updated_epoch)}</td></>} />}
          {commandScope === "admin" && <section className="glass card mb rise"><div className="card-head"><div><h2>명령어 실행</h2><p className="cap">명령어를 고르면 입력 칸이 나옵니다. 아래 미리보기는 Discord에 입력하는 `$명령어 인자`와 같습니다. 시장 사건($사건)은 시장 사건 화면에서 등록합니다.</p></div></div>
            <div className="form-grid command-picker"><label>명령어<select value={command} onChange={(event) => { setCommand(event.target.value); setCommandArgs(""); }}>{Array.from(new Set(commands.map((item) => COMMAND_FORMS[item.name]?.group ?? "기타"))).map((group) => <optgroup key={group} label={group}>{commands.filter((item) => (COMMAND_FORMS[item.name]?.group ?? "기타") === group).map((item) => <option key={item.name} value={item.name}>${item.name}</option>)}</optgroup>)}</select></label></div>
            {command && <CommandForm key={`${command}-${commandArgs}`} command={command} usage={selectedCommand?.usage ?? ""} prefill={commandArgs} busy={busy} onRun={runCommand} openProducts={(group) => setPage(group === "market" ? "instruments" : "finance")} />}
            {(command === "상품생성" || command === "상품수정") && <details className="help"><summary>상품 종류별 순서형 인자</summary><pre>{creationHelp}</pre></details>}
          </section>}
          {commandScope !== "help" && <><section className="glass card mb rise"><div className="card-head"><div><h2>실행 결과</h2><p className="cap">이 화면에서 실행한 최근 20건 · 전체 기록은 작업 기록에서 확인합니다.</p></div></div>
            {commandResults.map((item) => <div key={item.time} className={item.success ? "result" : "result failed"}><div className="result-head"><span className="mono">{item.national && <span className="tag inline-start">국가</span>}${item.command} {item.arguments}</span><span>{date(item.time)}</span></div><pre>{plain(item.message)}</pre></div>)}
            {!commandResults.length && <p className="empty">아직 실행한 명령어가 없습니다.</p>}
          </section>
          <section className="glass card rise"><div className="card-head"><div><h2>전체 명령어</h2><p className="cap">{commandScope === "admin" ? commands.length : nationalCommands.length}개 · 선택하면 위 입력에 적용됩니다.</p></div></div><div className="command-list">{commandScope === "admin"
            ? commands.map((item) => <button key={item.name} type="button" className={item.name === command ? "command-chip active" : "command-chip"} onClick={() => { setCommand(item.name); setCommandArgs(""); }}><b>${item.name}</b><small>{item.usage || "인자 없음"}</small></button>)
            : nationalCommands.map((item) => <button key={item.name} type="button" className={item.name === nationalCommand ? "command-chip active" : "command-chip"} onClick={() => setNationalCommand(item.name)}><b>${item.name}</b><small>{item.fields.map((field) => field.label).join(" · ") || "조회"}</small></button>)}</div></section></>}
        </>}

        {page === "database" && <>
          <section className="glass card mb rise"><div className="card-head"><div><h2>테이블</h2><p className="cap">값을 바꾸면 행의 저장 버튼이 켜집니다. `NULL`을 입력하면 NULL로 저장합니다. DB 제약과 트리거 검증을 그대로 거치며 모든 변경은 작업 기록에 남습니다. 비밀번호·작업 기록 테이블은 표시하지 않습니다.</p></div>
              <div className="actions"><select aria-label="테이블 선택" value={table} onChange={(event) => setTable(event.target.value)}>{tables.map((item) => <option key={item.name} value={item.name}>{item.name}{item.editable ? "" : " (읽기 전용)"}</option>)}</select><button className="soft" type="button" disabled={busy || !tablePage?.editable} onClick={() => setNewRow(newRow ? null : {})}>{newRow ? "추가 취소" : "행 추가"}</button></div></div>
            {newRow && tableInfo && <form className="form-grid new-row" onSubmit={insertRow}>{tableInfo.columns.map((column) => <label key={column.name}>{column.name}<small>{column.type || "ANY"}{column.primaryKey ? " · PK" : ""}{column.notNull ? " · 필수" : ""}{column.default !== null ? ` · 기본 ${column.default}` : ""}</small><input value={newRow[column.name] ?? ""} onChange={(event) => setNewRow({ ...newRow, [column.name]: event.target.value })} /></label>)}<button className="btn primary" disabled={busy}>추가</button></form>}
          </section>
          <section className="glass card rise">
            <div className="card-head"><div><h2>{table}</h2><p className="cap">{tablePage ? `${number(tablePage.total)}행 중 ${number(tablePage.total ? tablePage.offset + 1 : 0)}–${number(Math.min(tablePage.total, tablePage.offset + tablePage.rows.length))}` : "—"}</p></div>
              <div className="actions"><button className="soft" type="button" disabled={busy || !tablePage || tablePage.offset === 0} onClick={() => setTableOffset(Math.max(0, tableOffset - (tablePage?.pageSize ?? 50)))}>이전</button><button className="soft" type="button" disabled={busy || !tablePage || tablePage.offset + tablePage.rows.length >= tablePage.total} onClick={() => setTableOffset(tableOffset + (tablePage?.pageSize ?? 50))}>다음</button></div></div>
            <div className="scroll-x tall"><table className="grid"><thead><tr><th>rowid</th>{tablePage?.columns.map((column) => <th key={column.name} title={column.type}>{column.name}{column.primaryKey ? " 🔑" : ""}</th>)}<th /></tr></thead><tbody>
              {tablePage?.rows.map((row) => {
                const rowid = row.__rowid__ ?? -1;
                const rowEdits = edits[rowid] ?? {};
                return <tr key={rowid}><td className="mono muted">{rowid >= 0 ? rowid : "—"}</td>{tablePage.columns.map((column) => {
                  const edited = column.name in rowEdits;
                  const value = edited ? rowEdits[column.name] : showCell(row[column.name]);
                  return <td key={column.name} className={edited ? "cell edited" : "cell"}>{tablePage.editable ? <input aria-label={`${rowid} ${column.name}`} className={row[column.name] === null && !edited ? "mono null" : "mono"} value={value} onChange={(event) => setEdits({ ...edits, [rowid]: { ...rowEdits, [column.name]: event.target.value } })} /> : <span className="mono">{value}</span>}</td>;
                })}<td className="row-actions">{tablePage.editable && <><button className="btn primary small" disabled={busy || !Object.keys(rowEdits).length} onClick={() => saveRow(row)}>저장</button><button className="soft small" disabled={busy || !Object.keys(rowEdits).length} onClick={() => setEdits(Object.fromEntries(Object.entries(edits).filter(([key]) => Number(key) !== rowid)))}>취소</button><button className="soft small danger" disabled={busy} onClick={() => deleteRow(row)}>삭제</button></>}</td></tr>;
              })}
            </tbody></table>{tablePage && !tablePage.rows.length && <p className="empty">행이 없습니다.</p>}</div>
          </section>
        </>}

        {page === "audit" && <Card title="최근 관리자 작업" caption="최근 50건 · 작업 기록은 append-only이며 수정·삭제할 수 없습니다.">
          <div className="scroll-x tall"><table><thead><tr><th>시각</th><th>관리자 ID</th><th>작업</th><th>세부 내용</th></tr></thead><tbody>{audit.map((entry) => <tr key={entry.auditId}>
            <td>{date(entry.time)}</td><td className="mono">{entry.actorId}</td><td><span className="tag">{entry.action}</span></td><td className="audit-payload mono" title={JSON.stringify(entry.payload)}>{JSON.stringify(entry.payload)}</td>
          </tr>)}</tbody></table>{!audit.length && <p className="empty">기록이 없습니다.</p>}</div>
        </Card>}
        {page === "audit" && <LoginHistoryCard ctx={ctx} />}
        <p className="foot">INAZINA 로컬 관리 콘솔 · 모든 관리자 요청은 API에서 권한을 다시 확인합니다.</p>
      </main>
    </div>
  </Stage>;
}

function Stage({ children }: { children: ReactNode }) {
  return <div className="stage">
    <div className="wall" aria-hidden="true" />
    <div className="blob b1" aria-hidden="true" />
    <div className="blob b2" aria-hidden="true" />
    <div className="blob b3" aria-hidden="true" />
    {children}
  </div>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="spec"><span className="spec-value">{value}</span><span className="spec-label">{label}</span></div>;
}
