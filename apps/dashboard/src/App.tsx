import type { Money } from "../../shared/money";
import { validMoney, moneyCompare } from "../../shared/money";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, appUrl, clearToken, onUnauthorized, signedOut, type ApiUser } from "../../shared/api";
import { AuthPanel } from "../../shared/AuthPanel";
import { logoutRedirect } from "../../shared/sso";
import { Card, date, errorText, number, plain, type Ctx } from "./common";
import { JumpDialog, type JumpTarget } from "./JumpDialog";
import { TopBar } from "./TopBar";
import { Logo } from "../../shared/Logo";
import { PageHead } from "./PageHead";
import { HomePage } from "./HomePage";
import { Footer } from "./Footer";
import { LEGACY_PAGE } from "./nav";
const ProductsPage = lazy(() => import("./ProductsPage").then((module) => ({ default: module.ProductsPage })));
const UsersPage = lazy(() => import("./UsersPage").then((module) => ({ default: module.UsersPage })));
const ExchangesPage = lazy(() => import("./ExchangesPage").then((module) => ({ default: module.ExchangesPage })));
const CompaniesPage = lazy(() => import("./CompaniesPage").then((module) => ({ default: module.CompaniesPage })));
const WarningsCard = lazy(() => import("./WarningsCard").then((module) => ({ default: module.WarningsCard })));
const IdentityCard = lazy(() => import("./IdentityCard").then((module) => ({ default: module.IdentityCard })));
const CourtPage = lazy(() => import("./CourtPage").then((module) => ({ default: module.CourtPage })));
const FeedbackPage = lazy(() => import("./FeedbackPage").then((module) => ({ default: module.FeedbackPage })));
const LoginHistoryCard = lazy(() => import("./LoginHistoryCard").then((module) => ({ default: module.LoginHistoryCard })));
// lazy() erases the generic parameter of LogCard<T>; restore the component type so rows/idOf keep their row type.
const LogCard = lazy(() => import("./LogCard").then((module) => ({ default: module.LogCard }))) as unknown as typeof import("./LogCard").LogCard;
const BoardAdminPage = lazy(() => import("./BoardAdminPage").then((module) => ({ default: module.BoardAdminPage })));
const DocumentsPage = lazy(() => import("./DocumentsPage").then((module) => ({ default: module.DocumentsPage })));
const CompliancePage = lazy(() => import("./CompliancePage").then((module) => ({ default: module.CompliancePage })));
const BanksPage = lazy(() => import("./BanksPage").then((module) => ({ default: module.BanksPage })));
const SecurityPage = lazy(() => import("./SecurityPage").then((module) => ({ default: module.SecurityPage })));
const DatabasePage = lazy(() => import("./DatabasePage").then((module) => ({ default: module.DatabasePage })));
const GambleTile = lazy(() => import("./GambleTile").then((module) => ({ default: module.GambleTile })));
const EventTimeline = lazy(() => import("./ActivityTiles").then((module) => ({ default: module.EventTimeline })));
const RatesPage = lazy(() => import("./RatesPage").then((module) => ({ default: module.RatesPage })));
const AlmPage = lazy(() => import("./AlmPage").then((module) => ({ default: module.AlmPage })));
const ControlsPage = lazy(() => import("./ControlsPage").then((module) => ({ default: module.ControlsPage })));
const AccountingClosePage = lazy(() => import("./AccountingClosePage").then((module) => ({ default: module.AccountingClosePage })));
const LiquidityPage = lazy(() => import("./LiquidityPage").then((module) => ({ default: module.LiquidityPage })));
const NaturalizationPage = lazy(() => import("./NaturalizationPage").then((module) => ({ default: module.NaturalizationPage })));
import { COMMAND_FORMS, CommandForm } from "./CommandForm";
const AccountPage = lazy(() => import("./AccountPage").then((module) => ({ default: module.AccountPage })));
const OrganizationPage = lazy(() => import("./OrganizationPage").then((module) => ({ default: module.OrganizationPage })));
const OrganizationOwnershipWindow = lazy(() => import("./OrganizationOwnershipWindow").then((module) => ({ default: module.OrganizationOwnershipWindow })));

type Page = "home" | "account" | "overview" | "instruments" | "finance" | "exchanges" | "companies" | "organizations" | "users" | "naturalization" | "court" | "feedback" | "board" | "documents" | "credit" | "accounting" | "close" | "alm" | "rates" | "events" | "liquidity" | "commands" | "database" | "security" | "controls" | "ownership";
type FundComponent = { assetCode: string; weight: number; assetClass: string };
type Instrument = { code: string; name: string; type: string; price: Money; status: string; exchange_id?: string; fund_category?: string | null; components?: FundComponent[] };
type Exchange = { exchange_id: string; status: string; reason: string | null; trading_halted: number; updated_epoch: number | null; resume_epoch: number | null; circuit_stage: number | null };
type Overview = { activeUsers: number; activeInstruments: number; openOrders: number; market: Exchange[]; bankDeficits?: { issuer: string; account: string; cash: Money; nationality: string }[] };
type Audit = { auditId: number; actorId: string; action: string; payload: Record<string, unknown>; time: number };
type Event = { event_id: number; asset_code: string | null; event_type: string; severity: number; starts_epoch: number; ends_epoch: number; created_by: string; target_mode: string | null; target_value: number | null; active: boolean };
type Command = { name: string; usage: string };
type NationalField = { label: string; default: string; required: boolean; long: boolean };
type NationalCommand = { name: string; fields: NationalField[] };
type HelpPage = { label: string; description: string; fields: { name: string; value: string }[] };
type Flow = { history_id: number; asset_code: string; anchor_price: Money; target_price: Money; change_percent: number; start_epoch: number; end_epoch: number; active: boolean };
type BotJob = { job_id: number; kind: string; actor_id: string; payload: string; status: "pending" | "running" | "done" | "failed"; result: string | null; created_epoch: number; updated_epoch: number };
type CommandResult = { id: number; command: string; arguments: string; message: string; success: boolean; time: number; national: boolean };

const pages: { id: Page; label: string }[] = [
  { id: "home", label: "메인" },
  { id: "account", label: "내 계정" },
  { id: "overview", label: "운영 현황" },
  { id: "instruments", label: "종목 · 상품" },
  { id: "finance", label: "금융 상품" },
  { id: "exchanges", label: "거래소" },
  { id: "companies", label: "법인 자본" },
  { id: "organizations", label: "기업 구조" },
  { id: "users", label: "계정관리" },
  { id: "naturalization", label: "귀화심사" },
  { id: "court", label: "회생·파산" },
  { id: "feedback", label: "문의·신고" },
  { id: "board", label: "게시판 관리" },
  { id: "credit", label: "신용위험" },
  { id: "accounting", label: "은행 · 원장" },
  { id: "close", label: "회계 마감" },
  { id: "alm", label: "자금 · ALM" },
  { id: "rates", label: "금리 · 한도 · 정리" },
  { id: "documents", label: "문서 · 약관" },
  { id: "events", label: "시장 사건" },
  { id: "liquidity", label: "유동성" },
  { id: "commands", label: "관리자 명령어" },
  { id: "database", label: "DB 편집" },
  { id: "security", label: "보안" },
  { id: "controls", label: "통제 · 복구" },
  { id: "ownership", label: "조직도" },
];
const subtitles: Record<Page, string> = {
  home: "처리할 일과 시장 상황, 최근 작업을 한눈에 봅니다.",
  account: "관리자 계정의 로그인 정보, 실행 명의와 최근 로그인을 관리합니다.",
  overview: "시장 상태와 서비스 연결을 확인하고 거래소를 제어합니다.",
  instruments: "주식·자산·ETF·선물·채권·펀드·원자재·옵션·ELS를 생성·수정·중지합니다.",
  finance: "저축·대출·통화·외환통장 상품을 생성·수정·중지합니다.",
  exchanges: "거래소별 개장·휴장·서킷브레이커와 소속 국가·지수를 관리합니다.",
  companies: "법인 현금·보유자산·통장·대출을 보고 법인 계정으로 매수·매도·송금·대출을 실행합니다.",
  organizations: "기업 규모와 공공 분류, 주식 종류별 권리, 기업집단 및 지분·의결권 구조를 관리합니다.",
  naturalization: "Discord /귀화신청으로 들어온 신청을 심사합니다. 승인하면 신청자의 자산을 정리하고 국적을 옮깁니다.",
  court: "나루 회생 · 파산 탭으로 들어온 신청을 심리하고 인가·기각합니다. 결정은 신청자에게 Discord DM으로 전달됩니다.",
  feedback: "공식 사이트의 Contact·Report로 들어온 문의와 신고를 확인하고 처리합니다.",
  documents: "약관·처리방침·규칙·도움말과 Discord 등록 안내문을 고치고 확정합니다.",
  board: "나루 게시판의 신고를 처리하고 글을 숨기거나 고정합니다. 익명 글의 실제 작성자와 IP를 확인할 수 있습니다.",
  credit: "기대신용손실(ECL), 부도채권 상각·회수, 담보, 신용등급 정책을 관리합니다.",
  alm: "은행의 자본 비율(CET1·Tier 1·총자본), 레버리지, LCR, NSFR을 계산해 보고 관할별 기준을 조정합니다. 조회 전용이며 실제 자금은 움직이지 않습니다.",
  rates: "국가별 최고금리·APR 표시·세금 규칙을 계약일 기준으로 관리합니다. 등록한 규칙은 이후에 체결하는 계약에만 적용되며 기존 계약은 바뀌지 않습니다.",
  close: "마감 게이트를 확인하고 환전 반올림 잔여를 환전 손익 계정에 정리하거나 국고로 이전합니다.",
  accounting: "은행을 고유 ID로 만들고 관리하며, 은행별 이중분개 시산 잔액·불균형·결과 원장 대사를 조회합니다.",
  users: "사용자와 국가, 국가 계정의 모든 설정을 관리합니다.",
  events: "호재·악재 사건을 등록하고 최근 사건을 확인합니다.",
  liquidity: "알고리즘으로 호가를 올려 유동성을 공급하는 회사를 넣고 빼고, 유동성 성향을 정합니다.",
  commands: "Discord의 모든 $ 명령어와 도움말을 같은 검증과 기록으로 실행합니다.",
  database: "DB Browser에서 하던 설정 편집을 감사 기록과 함께 처리합니다.",
  security: "4312·4313 접속 비밀번호를 바꾸고, 접속 시도와 적용 중인 보호를 확인합니다.",
  controls: "통제 증빙표 상태, 복구 훈련·접근 검토 기록, 스트레스 시험 범위를 확인합니다. 읽기 전용입니다.",
  ownership: "기업 지분과 의결권 관계를 넓은 화면에서 확인합니다.",
};
type Notice = { id: number; text: string; error: boolean };
const SUCCESS_NOTICE_MS = 4000;
const SESSION_TRIES = 5;


// Each page (and each command category) has its own address: #instruments, #commands/거래소.
function readRoute(): { page: Page; sub: string } {
  const [head, ...rest] = decodeURIComponent(location.hash.replace(/^#\/?/, "")).replace(/\/+$/, "").split("/");
  const id = LEGACY_PAGE[head] ?? head;
  return { page: pages.some((item) => item.id === id) ? id as Page : "home", sub: rest.join("/") };
}
const NATIONAL = "국가 명의", HELP = "도움말";
const categoryOf = (name: string) => COMMAND_FORMS[name]?.group ?? "기타";
const scopeOf = (section: string) => section === NATIONAL ? "national" : section === HELP ? "help" : "admin";
const initialRoute = readRoute();

export default function App() {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [checked, setChecked] = useState(false);
  const [page, setPage] = useState<Page>(initialRoute.page);
  const [connection, setConnection] = useState<"checking" | "ok" | "down">("checking");
  const connected = connection === "ok";
  const [sessionError, setSessionError] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const noticeSeq = useRef(0);
  const resultSeq = useRef(0);
  const navSeq = useRef(0);
  const [busy, setBusy] = useState(false);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [unreadFeedback, setUnreadFeedback] = useState(0);
  const [pendingNaturalization, setPendingNaturalization] = useState(0);
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
  const [commandScope, setCommandScope] = useState<"admin" | "national" | "help">(initialRoute.page === "commands" ? scopeOf(initialRoute.sub) : "admin");
  const [commandCategory, setCommandCategory] = useState(initialRoute.page === "commands" && scopeOf(initialRoute.sub) === "admin" ? initialRoute.sub : "");
  const [helpPages, setHelpPages] = useState<HelpPage[]>([]);
  const [helpPage, setHelpPage] = useState(0);
  const [jobs, setJobs] = useState<BotJob[]>([]);
  const [nationalCommand, setNationalCommand] = useState("");
  const [nationalValues, setNationalValues] = useState<string[]>([]);
  const [creationHelp, setCreationHelp] = useState("");
  const [command, setCommand] = useState("");
  const [commandArgs, setCommandArgs] = useState("");
  const [commandResults, setCommandResults] = useState<CommandResult[]>([]);

  const say = useCallback((text: string, error = false) => { noticeSeq.current += 1; setNotice({ id: noticeSeq.current, text, error }); }, []);
  // Every page change goes through here, so a notice raised in the same handler (after the change) survives it.
  const go = useCallback((next: Page) => { navSeq.current = noticeSeq.current; setPage(next); }, []);
  // ⌘K / Ctrl+K: jump to any page or command category by typing.
  const [jumpOpen, setJumpOpen] = useState(false);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setJumpOpen((open) => !open); }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  // Successes fade on their own; errors stay until they are dismissed.
  useEffect(() => {
    if (!notice || notice.error) return;
    const timer = window.setTimeout(() => setNotice((current) => current?.id === notice.id ? null : current), SUCCESS_NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const resetSessionState = useCallback(() => {
    setOverview(null); setInstruments([]); setAudit([]); setEvents([]); setFlows([]);
    setCommands([]); setNationalCommands([]); setDiscordOnly([]); setHelpPages([]); setJobs([]);
    setCommand(""); setCommandArgs(""); setCommandResults([]); setNationalCommand(""); setNationalValues([]);
    setUnreadFeedback(0); setPendingNaturalization(0); unreadSeen.current = 0;
    setQuery(""); setNotice(null); navSeq.current = noticeSeq.current; setPage("home");
  }, []);
  // The server refused our session (expired or signed out elsewhere): drop to the login screen.
  useEffect(() => onUnauthorized(() => {
    setUser(null);
    resetSessionState();
    noticeSeq.current += 1;
    setNotice({ id: noticeSeq.current, text: "로그인이 만료되었습니다. 다시 로그인해주세요.", error: true });
  }), [resetSessionState]);

  const sessionTries = useRef(0);
  const sessionTimer = useRef<number | undefined>(undefined);

  const loadSession = useCallback(async () => {
    window.clearTimeout(sessionTimer.current);
    try {
      const result = await api<{ user: ApiUser }>("/api/auth/session");
      sessionTries.current = 0;
      setSessionError("");
      setUser(result.user);
      setChecked(true);
    } catch (cause) {
      // Only a refused session signs out; a slow or unreachable server is retried a few times.
      if (!signedOut(cause)) {
        sessionTries.current += 1;
        if (sessionTries.current >= SESSION_TRIES) { setSessionError(errorText(cause, "서버에 연결하지 못했습니다.")); return; }
        sessionTimer.current = window.setTimeout(() => void loadSession(), 3000);
        return;
      }
      setUser(null);
      setChecked(true);
    }
  }, []);

  // Read through refs so that these counts changing does not recreate refresh (and refetch the page).
  const commandCount = useRef(0);
  const helpCount = useRef(0);
  commandCount.current = commands.length;
  helpCount.current = helpPages.length;

  const refresh = useCallback(async () => {
    try {
      const result = await api<{ status: string; python: string }>("/api/health");
      setConnection(result.python === "connected" ? "ok" : "down");
    } catch {
      setConnection("down");
    }
    if (!user || user.role !== "admin") return;
    try {
      if (page === "overview" || page === "home") setOverview(await api<Overview>("/api/admin/overview"));
      if (page === "overview") setAudit((await api<{ entries: Audit[] }>("/api/admin/audit")).entries);
      if (page === "events") {
        const [eventResult, instrumentResult] = await Promise.all([
          api<{ events: Event[]; flows?: Flow[] }>("/api/admin/events"),
          api<{ instruments: Instrument[] }>("/api/admin/instruments"),
        ]);
        setEvents(eventResult.events);
        setFlows(eventResult.flows ?? []);
        setInstruments(instrumentResult.instruments);
      }
      if (page === "commands" && !commandCount.current) {
        const result = await api<{ commands: Command[]; national: NationalCommand[]; discordOnly: string[]; creationHelp: string }>("/api/admin/commands");
        setCommands(result.commands);
        setNationalCommands(result.national);
        setDiscordOnly(result.discordOnly);
        setCreationHelp(result.creationHelp);
        setNationalCommand((current) => current || result.national[0]?.name || "");
      }
      if (page === "commands" && commandScope === "help" && !helpCount.current) setHelpPages((await api<{ pages: HelpPage[] }>("/api/admin/help")).pages);
      if (page === "commands" && commandScope === "national") setJobs((await api<{ jobs: BotJob[] }>("/api/admin/bot-jobs")).jobs);
    } catch (cause) {
      say(errorText(cause, "요청을 처리하지 못했습니다."), true);
    }
  }, [page, user, commandScope, say]);

  useEffect(() => {
    void loadSession();
    return () => window.clearTimeout(sessionTimer.current);
  }, [loadSession]);
  useEffect(() => { if (user && page !== "ownership") void refresh(); }, [user, page, refresh]);
  // Official-site Contact/Report alerts: refresh the unread count every minute.
  const loadUnread = useCallback(async () => {
    try {
      const result = await api<{ unread: number }>("/api/admin/feedback?status=new");
      if (result.unread > unreadSeen.current) say(`공식 사이트에서 새 문의·신고가 ${result.unread - unreadSeen.current}건 왔습니다.`);
      unreadSeen.current = result.unread;
      setUnreadFeedback(result.unread);
      setPendingNaturalization((await api<{ pending: number }>("/api/admin/naturalization")).pending);
    } catch { /* the badge is best effort */ }
  }, [say]);
  useEffect(() => {
    if (!user || user.role !== "admin" || page === "ownership") return;
    void loadUnread();
    // Skip polling while the tab is hidden and catch up as soon as it is shown again.
    const timer = window.setInterval(() => { if (!document.hidden) void loadUnread(); }, 60_000);
    const onVisible = () => { if (!document.hidden) void loadUnread(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [user, page, loadUnread]);
  // A page change clears the search box and any notice raised before it; notices raised after it stay.
  useEffect(() => { setQuery(""); setNotice((current) => current && current.id <= navSeq.current ? null : current); }, [page]);

  const selectedCommand = commands.find((item) => item.name === command);
  const commandCategories = Array.from(new Set(commands.map((item) => categoryOf(item.name))));
  const activeCategory = commandCategories.includes(commandCategory) ? commandCategory : commandCategories[0] ?? "";
  const visibleCommands = commands.filter((item) => categoryOf(item.name) === activeCategory);
  const commandSection = commandScope === "national" ? NATIONAL : commandScope === "help" ? HELP : activeCategory;

  // Keep the address in step with the page; back/forward restore it.
  useEffect(() => {
    const hash = `#${page}${page === "commands" && commandSection ? `/${encodeURIComponent(commandSection)}` : ""}`;
    if (page === "ownership") return;
    if (decodeURIComponent(location.hash) !== decodeURIComponent(hash)) history.pushState(null, "", hash);
  }, [page, commandSection]);
  useEffect(() => {
    const restore = () => {
      const route = readRoute();
      go(route.page);
      if (route.page !== "commands") return;
      setCommandScope(scopeOf(route.sub));
      if (route.sub && scopeOf(route.sub) === "admin") setCommandCategory(route.sub);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [go]);
  // A command outside the open category (e.g. opened from another page) moves to its category.
  useEffect(() => {
    if (!commands.length) return;
    if (!command) setCommand(visibleCommands[0]?.name ?? "");
    else if (commandScope === "admin" && categoryOf(command) !== activeCategory) setCommandCategory(categoryOf(command));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [command, commands.length]);
  function openCategory(category: string) {
    setCommandScope(scopeOf(category));
    if (scopeOf(category) !== "admin") return;
    setCommandCategory(category);
    if (categoryOf(command) !== category) { setCommand(commands.find((item) => categoryOf(item.name) === category)?.name ?? ""); setCommandArgs(""); }
  }
  const selectedNational = nationalCommands.find((item) => item.name === nationalCommand);
  const eventInstrument = instruments.find((item) => item.code === eventAsset.trim());
  useEffect(() => { setNationalValues(selectedNational?.fields.map((field) => field.default) ?? []); }, [selectedNational]);

  const run = useCallback(async <T,>(work: () => Promise<T>, fallback: string, done?: (result: T) => void) => {
    setBusy(true);
    try {
      const result = await work();
      done?.(result);
      await refresh();
      return true;
    } catch (cause) {
      say(errorText(cause, fallback), true);
      return false;
    } finally {
      setBusy(false);
    }
  }, [refresh, say]);

  const openCommand = useCallback((name: string, args: string, national = false) => {
    go("commands");
    if (national) {
      setCommandScope("national");
      setNationalCommand(name);
    } else {
      setCommandScope("admin");
      setCommand(name);
      setCommandArgs(args);
    }
  }, [go]);

  const ctx: Ctx = useMemo(() => ({ busy, run, say, query, openCommand }), [busy, run, say, query, openCommand]);

  function createEvent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const percent = Number(eventSeverity);
    const asset = eventAsset.trim();
    const target = eventTarget.trim() ? eventTarget.trim() : null;
    // Same rules as the Discord $사건 form: a target price needs one asset and
    // must lie above the current price for good news, below it for bad news.
    if (target !== null) {
      if (!validMoney(target, true)) { say("목표가는 0보다 큰 금액으로 입력해주세요.", true); return; }
      if (!asset) { say("목표가를 지정하려면 대상 종목 코드를 입력해주세요.", true); return; }
      if (eventInstrument && (eventType === "good" ? moneyCompare(target, eventInstrument.price) < 0 : moneyCompare(target, eventInstrument.price) > 0)) {
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
        setCommandResults((items) => [{ id: ++resultSeq.current, command, arguments: args, message: result.message, success: result.success, time: Date.now() / 1000, national: false }, ...items].slice(0, 20));
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
        setCommandResults((items) => [{ id: ++resultSeq.current, command: nationalCommand, arguments: nationalValues.filter(Boolean).join(" "), message: result.message, success: result.success, time: Date.now() / 1000, national: true }, ...items].slice(0, 20));
      });
  }

  async function logout() {
    // Leaving a country or company account ($login) returns to the administrator, on this page.
    const delegated = Boolean(user?.delegatedBy);
    let result: { returnedTo?: ApiUser } = {};
    try {
      result = await api<{ returnedTo?: ApiUser }>("/api/auth/logout", { method: "POST" });
    } catch (cause) {
      // An already-refused session is as good as logged out; any other failure leaves everything as it was.
      if (!signedOut(cause)) { say(errorText(cause, delegated ? "관리자 계정으로 돌아가지 못했습니다. 잠시 후 다시 시도해주세요." : "로그아웃하지 못했습니다. 잠시 후 다시 시도해주세요."), true); return; }
    }
    clearToken();
    resetSessionState();
    if (result.returnedTo) { setUser(result.returnedTo); go("account"); say(`관리자 ${result.returnedTo.nickname} 계정으로 돌아왔습니다.`); return; }
    setUser(null);
    const singleSignOut = logoutRedirect(result); // also ends the single sign-on session
    if (singleSignOut) window.location.assign(singleSignOut);
  }

  const noticeBar = notice && <div className={notice.error ? "notice error" : "notice ok"} role={notice.error ? "alert" : "status"}><span>{notice.text}</span><button type="button" className="ghost" aria-label="알림 닫기" onClick={() => setNotice(null)}>×</button></div>;

  if (!checked) return <Stage><main className="center-shell">
    {sessionError
      ? <div className="notice error" role="alert"><span>{sessionError} 서버가 켜져 있는지 확인한 뒤 다시 시도해주세요.</span><button type="button" className="soft" onClick={() => { sessionTries.current = 0; setSessionError(""); void loadSession(); }}>다시 시도</button></div>
      : <p className="loading">불러오는 중…</p>}
  </main></Stage>;
  if (!user) return <Stage>{noticeBar && <div className="center-shell">{noticeBar}</div>}<AuthPanel app="dashboard" onAuthenticated={(next) => { setNotice(null); setUser(next); }} /></Stage>;
  if (page === "ownership" && user.role === "admin") return <Stage><Suspense fallback={<main className="center-shell"><p className="loading">조직도를 여는 중…</p></main>}><OrganizationOwnershipWindow route={initialRoute.sub} /></Suspense></Stage>;
  // Users, country and company accounts get their own account dashboard, like the HTS.
  if (user.role !== "admin") {
    return <Stage><div className="shell">
      <header className="gnav"><div className="gnav-in">
        <a className="gnav-logo" href="#account" aria-label="inazina 내 대시보드"><Logo /></a>
        <div className="gnav-meta"><a className="gnav-text" href={appUrl("hts")}>나루 열기 ↗</a><button type="button" className="gnav-text" onClick={logout}>{user.delegatedBy ? "관리자로 돌아가기" : "로그아웃"}</button></div>
      </div></header>
      <main className="content user-view">
        {user.delegatedBy && <div className="notice" role="status"><span>관리자 {user.delegatedByName ?? user.delegatedBy}님이 {user.nickname} 계정으로 로그인 중입니다.</span></div>}
        {noticeBar}
        <Suspense fallback={<p className="loading">계정 화면을 불러오는 중…</p>}><AccountPage ctx={ctx} user={user} onLogout={logout} /></Suspense>
        <footer className="bot"><div className="bot-in"><span className="bot-status"><Logo className="bot-logo" /></span><span>© INAZINA. All rights reserved.</span></div></footer>
      </main>
    </div></Stage>;
  }

  const current = pages.find((item) => item.id === page)!;
  const searchable = page === "instruments" || page === "finance" || page === "exchanges" || page === "companies" || page === "users" || page === "court" || page === "feedback" || page === "board" || page === "database" || page === "accounting";
  const labels = new Map<string, string>(pages.filter((item) => item.id !== "account" && item.id !== "ownership").map((item) => [item.id, item.label]));
  const badges = { feedback: unreadFeedback, naturalization: pendingNaturalization };
  const jumpTargets: JumpTarget[] = !jumpOpen ? [] : [
    ...pages.map((item) => ({ key: item.id, label: item.label, hint: subtitles[item.id], go: () => go(item.id) })),
    ...commandCategories.map((category) => ({ key: `commands/${category}`, label: `관리자 명령어 · ${category}`, hint: `${commands.filter((item) => categoryOf(item.name) === category).length}개 명령어`, go: () => { go("commands"); openCategory(category); } })),
    ...commands.map((item) => ({ key: `$${item.name}`, label: `$${item.name}`, hint: item.usage || categoryOf(item.name), go: () => openCommand(item.name, "") })),
  ];

  return <Stage>
    <div className="shell">
      <TopBar user={user} page={page} pages={pages.filter((item) => item.id !== "account" && item.id !== "ownership")}
        badges={badges} connection={connection} onPage={(next) => go(next as Page)} onLogout={logout} />
      <main className="content">
        <PageHead page={page} title={current.label} labels={labels} badges={badges} searchable={searchable} query={query}
          placeholder={page === "users" ? "이름 또는 ID" : page === "database" ? "모든 열에서 검색" : "이름 또는 코드"} onQuery={setQuery} onPage={(next) => go(next as Page)} />
        {noticeBar}

        <Suspense fallback={<p className="loading">화면을 불러오는 중…</p>}>
        {page === "home" && <HomePage market={overview?.market} bankDeficits={overview?.bankDeficits ?? []} labels={labels} badges={badges} onPage={(next) => go(next as Page)} />}
        {page === "overview" && <>
          <section className="spec-strip" aria-label="주요 지표">
            <Metric label="활성 사용자" value={overview ? number(overview.activeUsers) : "—"} />
            <Metric label="거래 중 종목·상품" value={overview ? number(overview.activeInstruments) : "—"} />
            <Metric label="미체결 주문" value={overview ? number(overview.openOrders) : "—"} />
          </section>
          {!!overview?.bankDeficits?.length && <section className="glass card mb rise warn-card" role="alert"><div className="card-head"><div><h2>⚠️ 은행 본사 계좌 적자</h2><p className="cap">적자는 허용되지만 자본 확충을 검토하세요. 처음 적자가 될 때 관리자에게 Discord DM으로도 알립니다.</p></div></div>
            <div className="scroll-x"><table><thead><tr><th>은행</th><th>본사 계좌</th><th>국가</th><th className="r">잔액</th></tr></thead><tbody>{overview.bankDeficits.map((bank) => <tr key={bank.issuer}><td>{bank.issuer}</td><td className="mono">{bank.account}</td><td>{bank.nationality}</td><td className="r mono">{number(bank.cash)}</td></tr>)}</tbody></table></div></section>}
        </>}

        {page === "instruments" && <><ProductsPage group="market" ctx={ctx} /><WarningsCard ctx={ctx} /></>}
        {page === "finance" && <ProductsPage group="finance" ctx={ctx} />}
        {page === "exchanges" && <ExchangesPage ctx={ctx} />}
        {page === "companies" && <CompaniesPage ctx={ctx} />}
        {page === "organizations" && <OrganizationPage ctx={ctx} />}
        {page === "board" && <BoardAdminPage ctx={ctx} />}
        {page === "documents" && <DocumentsPage ctx={ctx} />}
        {page === "credit" && <CompliancePage ctx={ctx} />}
        {page === "accounting" && <BanksPage ctx={ctx} />}
        {page === "court" && <CourtPage ctx={ctx} />}
        {page === "feedback" && <FeedbackPage ctx={ctx} onChange={() => void loadUnread()} />}
        {page === "naturalization" && <NaturalizationPage ctx={ctx} onChange={() => void loadUnread()} />}
        {page === "liquidity" && <LiquidityPage ctx={ctx} />}
        {page === "close" && <AccountingClosePage ctx={ctx} />}
        {page === "alm" && <AlmPage ctx={ctx} />}
        {page === "rates" && <RatesPage ctx={ctx} />}
        {page === "users" && <UsersPage ctx={ctx} currentUserId={user.userId} />}
        {page === "events" && <>
          <EventTimeline events={events} />
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
            row={(item) => <><td className="mono">#{item.history_id}{item.active && <span className="tag ok inline">진행 중</span>}</td><td className="mono">{item.asset_code}</td><td><span className={item.change_percent >= 0 ? "tag up" : "tag down"}>{item.change_percent >= 0 ? "상승" : "하락"}</span></td><td className="r mono">{number(item.anchor_price)}</td><td className="r mono">{number(item.target_price)}</td><td className="r mono">{item.change_percent >= 0 ? "+" : ""}{Number(item.change_percent).toFixed(2)}%</td><td>{date(item.start_epoch)}</td><td>{date(item.end_epoch)}</td></>} />
        </>}

        {page === "commands" && <div className="command-layout">
          <nav className="command-rail" aria-label="명령어 카테고리">
            <p>관리자 명령어</p>
            {commandCategories.map((category) => <button key={category} type="button" aria-current={commandSection === category ? "page" : undefined} onClick={() => openCategory(category)}>{category}<span>{commands.filter((item) => categoryOf(item.name) === category).length}</span></button>)}
            <p>국가 · 참고</p>
            <button type="button" aria-current={commandSection === NATIONAL ? "page" : undefined} onClick={() => openCategory(NATIONAL)}>국가 명의 실행<span>{nationalCommands.length}</span></button>
            <button type="button" aria-current={commandSection === HELP ? "page" : undefined} onClick={() => openCategory(HELP)}>도움말 ($도움말)</button>
          </nav>
          <div className="command-content">
          {commandScope === "admin" && <div className="feature-pair"><GambleTile ctx={ctx} /></div>}
          {commandScope === "admin" && <section className="command-category-head rise"><p className="eyebrow">관리자 명령어</p><h2>{activeCategory || "불러오는 중…"}</h2><p className="cap">{visibleCommands.length}개 명령어 · 고르면 아래에 입력 칸이 열립니다. 시장 사건($사건)은 시장 사건 화면에서 등록합니다.</p>
            <div className="command-list">{visibleCommands.map((item) => <button key={item.name} type="button" className={item.name === command ? "command-chip active" : "command-chip"} aria-pressed={item.name === command} onClick={() => { setCommand(item.name); setCommandArgs(""); }}><b>${item.name}</b><small>{item.usage || "인자 없음"}</small></button>)}</div>
          </section>}
          {commandScope === "admin" && command && <section className="glass card mb rise"><div className="card-head"><div><h2>${command}</h2><p className="cap">아래 미리보기는 Discord에 입력하는 `$명령어 인자`와 같습니다.</p></div></div>
            <CommandForm key={command} command={command} usage={selectedCommand?.usage ?? ""} prefill={commandArgs} busy={busy} onRun={runCommand} openProducts={(group) => go(group === "market" ? "instruments" : "finance")} />
            {(command === "상품생성" || command === "상품수정") && <details className="help"><summary>상품 종류별 순서형 인자</summary><pre>{creationHelp}</pre></details>}
          </section>}
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
          {commandScope === "national" && <section className="glass card mb rise"><div className="card-head"><div><h2>국가 명의 명령어</h2><p className="cap">{nationalCommands.length}개 · 선택하면 위 입력에 적용됩니다.</p></div></div><div className="command-list">{nationalCommands.map((item) => <button key={item.name} type="button" className={item.name === nationalCommand ? "command-chip active" : "command-chip"} onClick={() => setNationalCommand(item.name)}><b>${item.name}</b><small>{item.fields.map((field) => field.label).join(" · ") || "조회"}</small></button>)}</div></section>}
          {commandScope !== "help" && <section className="glass card mb rise"><div className="card-head"><div><h2>실행 결과</h2><p className="cap">이 화면에서 실행한 최근 20건 · 전체 기록은 작업 기록에서 확인합니다.</p></div></div>
            {commandResults.map((item) => <div key={item.id} className={item.success ? "result" : "result failed"}><div className="result-head"><span className="mono">{item.national && <span className="tag inline-start">국가</span>}${item.command} {item.arguments}</span><span>{date(item.time)}</span></div><pre>{plain(item.message)}</pre></div>)}
            {!commandResults.length && <p className="empty">아직 실행한 명령어가 없습니다.</p>}
          </section>}
          </div>
        </div>}

        {page === "database" && <DatabasePage ctx={ctx} />}
        {page === "security" && <SecurityPage ctx={ctx} />}
        {page === "controls" && <ControlsPage ctx={ctx} />}

        {page === "overview" && <Card title="작업 기록"  caption="최근 50건 · 작업 기록은 append-only이며 수정·삭제할 수 없습니다.">
          <div className="scroll-x tall"><table><thead><tr><th>시각</th><th>관리자 ID</th><th>작업</th><th>세부 내용</th></tr></thead><tbody>{audit.map((entry) => <tr key={entry.auditId}>
            <td>{date(entry.time)}</td><td className="mono">{entry.actorId}</td><td><span className="tag">{entry.action}</span></td><td className="audit-payload mono" title={JSON.stringify(entry.payload)}>{JSON.stringify(entry.payload)}</td>
          </tr>)}</tbody></table>{!audit.length && <p className="empty">기록이 없습니다.</p>}</div>
        </Card>}
        {page === "overview" && <LoginHistoryCard ctx={ctx} />}
        {page === "account" && <AccountPage ctx={ctx} user={user} onLogout={logout} />}
        </Suspense>
        <Footer connected={connected} />
      </main>
    </div>
    {jumpOpen && <JumpDialog targets={jumpTargets} onClose={() => setJumpOpen(false)} />}
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
