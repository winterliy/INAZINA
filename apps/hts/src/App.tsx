import { formatMoney, formatMoneyNumeric, exactMoney, moneyAdd, moneyMultiply, moneyCompare, moneyDivide, minorMoney, validMoney, type Money } from "../../shared/money";
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, apiError, clearToken, onUnauthorized, signedOut, ApiError, type ApiUser } from "../../shared/api";
import { Logo } from "../../shared/Logo";
import { navigate } from "./nav";
import { SsoGate } from "../../shared/SsoGate";
import { AccountWithdrawal } from "./AccountWithdrawal";
import { totalInHome } from "./exactTotal";
import { logoutRedirect, currentReturnTo, ssoLoginUrl } from "../../shared/sso";
import { guestCanView, loginPanelRequested } from "./guest";
import "./guest.css";
import { DEFAULT_INDICATORS, INDICATOR_CATALOG, PriceChart, type Candle, type Drawing, type IndicatorCatalog, type IndicatorChoice, type IndicatorData, type Point, type Tool } from "./PriceChart";
import { OPEN } from "./commandKit";
import { ErrorBoundary } from "./ErrorBoundary";
import { legacyCommandPath, servicePath } from "./services/paths";
import { SERVICES } from "./services/ServiceKit";
import { Backdrop } from "./Backdrop";
import { SearchIndicators } from "./SearchIndicators";
import { ExchangeHeatmap } from "./ExchangeHeatmap";
import { PortfolioPage } from "./PortfolioPage";
import { HeatmapPage } from "./HeatmapPage";
import { CreditPage, type CreditReport } from "./CreditPage";
import { PasswordSection } from "./PasswordSection";
import { NotificationBell } from "./NotificationBell";
import { PayoutPage } from "./PayoutPage";
import { PendingPayouts } from "./PendingPayouts";
import { RefreshRateButton } from "./RefreshRateButton";
import { OrderReviewModal } from "./OrderReviewModal";
import { usePolling } from "./usePolling";
import { tabListKeys } from "./tabKeys";
import type { Instrument, KycProfile, Order, Portfolio, Warning } from "./types";
import "./pages.css";
import "./notifications.css";
import type { OwnershipGraphData } from "../../shared/OwnershipGraph";

// Heavy, route-only screens load on first visit instead of with the trading screen.
const ServicesApp = lazy(() => import("./services/ServicesApp").then((m) => ({ default: m.ServicesApp })));
const OwnershipGraph = lazy(() => import("../../shared/OwnershipGraph").then((m) => ({ default: m.OwnershipGraph })));

type DrMovement = { movement_id: number; dr_code: string; underlying_code: string; underlying_currency: string; movement_type: "issue" | "redeem"; dr_quantity: number; underlying_quantity: number; underlying_average_cost: number; underlying_average_cost_text: string; decimal_places: number; recorded_epoch: number };
type DrCustodyInstruction = { reference_key: string; dr_code: string; underlying_code: string; movement_type: "issue" | "redeem"; dr_quantity: number; underlying_quantity: number; status: "requested" | "verified" | "settled" | "rejected" | "cancelled"; requested_epoch: number; settle_after_epoch: number | null };
type DrVotingDelegation = { delegation_id: number; dr_code: string; underlying_code: string; proxy_user_id: string; dr_quantity: number; status: "active" | "revoked"; created_epoch: number; revoked_epoch: number | null; events: { event_type: string; recorded_epoch: number }[] };
type ReceivedDrVotingDelegation = { delegation_id: number; dr_code: string; underlying_code: string; owner_user_id: string; dr_quantity: number; created_epoch: number };
type DrVotingAgenda = { meetingId: number; agendaItemId: number; underlyingCode: string; meetingTitle: string; agendaTitle: string; choices: string[]; recordEpoch: number; opensEpoch: number | null; closesEpoch: number | null; quorumPercent: number; votingPower: number; totalEligible: number; myChoice: string | null; state: "unscheduled" | "upcoming" | "open" | "closed" };

// Administrator trading warnings (투자주의·경고·위험).
const WarningBadge = ({ warning }: { warning?: Warning }) => warning ? <span className={`warn-badge ${warning.level}`} title={warning.reason || warning.label}>{warning.label}</span> : null;
type LoanSchedule = { schedule_id: number; installment_number: number; due_epoch: number; principal_due: Money; interest_due: Money; fee_due: Money; principal_paid: Money; interest_paid: Money; fee_paid: Money; amount_paid: Money; status: string };
type LoanRateReset = { old_rate: number; new_rate: number; scheduled_epoch: number; applied_epoch: number; benchmark_key: string; benchmark_tenor_days: number };
const RATE_BENCHMARK_LABELS: Record<string, string> = { policy: "정책금리", funding: "조달금리", bank_bond: "은행채 수익률", treasury: "국채 수익률" };
type LoanScheduleView = { loan_id: number; product_code: string; quote_currency: string; decimal_places: number; outstanding_balance: Money; status: string; rate_type: string; contract_rate: number | null; next_repricing_epoch: number | null; benchmark_key: string | null; benchmark_tenor_days: number; repricing_lag_seconds: number; collection_status: string; maturity_epoch: number | null; stage_cure_since_epoch: number | null; underwriting_dsr: number | null; underwriting_stress_dsr: number | null; underwriting_stress_shock_bps: number; underwriting_fx_stress_shock_percent: number; origination_pd_12m: number | null; current_ecl_stage: number | null; current_ecl_pd_12m: number | null; installments: LoanSchedule[]; rate_resets: LoanRateReset[] };
type TaxResidency = { nationality: string | null; taxResidency: string | null; countries: string[] };
type Book = { bids: { limit_price: Money; quantity: number; order_count: number }[]; asks: { limit_price: Money; quantity: number; order_count: number }[] };

const ranges = ["1h", "1d", "1w", "1m", "all"] as const;
const rangeNames: Record<string, string> = { "1h": "1시간", "1d": "1일", "1w": "1주", "1m": "1개월", all: "전체" };
const intervals: [number, string][] = [[60, "1분봉"], [300, "5분봉"], [600, "10분봉"], [1800, "30분봉"], [3600, "1시간봉"], [21600, "6시간봉"], [43200, "12시간봉"], [86400, "일봉"]];
const STATUS: Record<string, string> = { open: "미체결", partial: "부분 체결", scheduled: "예약", filled: "체결", cancelled: "취소", expired: "만료" };
const money = formatMoney;
const compactMoney = formatMoneyNumeric;
const ORDER_TYPES = [["limit", "지정가"], ["market", "시장가"], ["current", "현재가"]] as const;
// A missing or invalid time shows as a dash; one bad row must never blank the whole page.
const date = (epoch: number) => Number.isFinite(epoch * 1000) ? new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" }).format(epoch * 1000) : "—";
const KYC_STATUS: Record<KycProfile["status"], string> = { pending: "확인 대기", verified: "확인 완료", rejected: "확인 거절", expired: "만료" };
const KYC_RISK: Record<KycProfile["risk_tier"], string> = { standard: "일반", enhanced: "강화 심사", restricted: "송금 제한" };
const fundCategoryName: Record<string, string> = { equity: "주식형", bond: "채권형", mixed: "혼합형", other: "기타형" };
const instrumentType = (item: Instrument) => item.type === "depositary_receipt" ? "주식예탁증서 (DR)" : fundCategoryName[item.fund_category || ""] || item.type;
// Search tabs shown while the search box is empty. "추천" is the starting tab.
type SearchTab = "recommended" | "stock" | "fund" | "bond" | "commodity" | "other";
const SEARCH_TABS: [SearchTab, string][] = [["recommended", "실시간 추천"], ["stock", "주식"], ["fund", "ETF·펀드"], ["bond", "채권"], ["commodity", "원자재·외환"], ["other", "기타"]];
const searchTab = (item: Instrument): Exclude<SearchTab, "recommended"> =>
  item.type === "stock" || item.type === "preferred" || item.type === "depositary_receipt" ? "stock"
    : ["etf", "etn", "mutual_fund", "index_fund", "leveraged_etf", "inverse_etf", "reit"].includes(item.type) ? "fund"
      : item.type === "국채" || item.type.includes("bond") ? "bond"
        : item.type === "commodity" || item.type === "forex" ? "commodity" : "other";
// "실시간 추천" rankings over the last 24 hours, top 20 each.
type RecommendSort = "value" | "volume" | "rising" | "falling";
const RECOMMEND_SORTS: [RecommendSort, string, string][] = [
  ["value", "거래대금", "최근 24시간 거래대금이 많은 순"],
  ["volume", "거래량", "최근 24시간 거래 수량이 많은 순"],
  ["rising", "급상승", "최근 24시간 상승률이 높은 순 (상승 종목만)"],
  ["falling", "급하락", "최근 24시간 하락률이 큰 순 (하락 종목만)"],
];
const recommended = (items: Instrument[], sort: RecommendSort) => {
  const change = (item: Instrument) => item.change24h ?? 0;
  const ranked = sort === "rising" ? items.filter((item) => change(item) > 0).sort((a, b) => change(b) - change(a))
    : sort === "falling" ? items.filter((item) => change(item) < 0).sort((a, b) => change(a) - change(b))
      : [...items].sort(sort === "volume"
        ? (a, b) => (b.volume24h ?? 0) - (a.volume24h ?? 0) || moneyCompare(b.tradeValue24h ?? 0, a.tradeValue24h ?? 0)
        : (a, b) => moneyCompare(b.tradeValue24h ?? 0, a.tradeValue24h ?? 0) || Math.abs(change(b)) - Math.abs(change(a)));
  return ranked.slice(0, 20);
};
/** The board is its own page (/board); "종목 토론" opens it filtered to one instrument. */
const boardUrl = (asset = "") => asset ? `/board?asset=${encodeURIComponent(asset)}` : "/board";
const errorText = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;
/** 32 hex characters. getRandomValues also works on plain-http LAN addresses, where crypto.randomUUID does not. */
const requestKey = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");

const ROLE_LABEL: Record<string, string> = { user: "개인 계정", admin: "관리자", nation: "국가 계정", company: "법인 계정" };
const INDICATOR_KEY = "inazina.hts.chart-indicators.v2";

const WATCH_KEY = "inazina.hts.watchlist";
const USER_KEY = "inazina.hts.user";
const GUEST_USER: ApiUser = { userId: "", nickname: "", nationality: "", role: "user", cash: 0, creditScore: 0 };
const USER_ROLES = ["admin", "user", "nation", "company"];
/** The cached user is only trusted when it has the shape the pages rely on; the session check still confirms it. */
function cachedUser(): ApiUser | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(USER_KEY) ?? "null");
    if (value && typeof value === "object" && typeof value.userId === "string" && value.userId && typeof value.nickname === "string"
      && typeof value.nationality === "string" && USER_ROLES.includes(value.role) && ["string", "number"].includes(typeof value.cash)) return value as ApiUser;
  } catch { /* unreadable cache */ }
  return null;
}
type Notice = { text: string; kind: "error" | "success"; expired?: boolean };
/** A definitive refusal (the server read the request and said no); network errors and 5xx may have succeeded. */
const definitiveRefusal = (cause: unknown) => cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408 && cause.status !== 429;
const wholeNumber = (value: string) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value));

export default function App() {
  // The last signed-in user of this tab draws the page at once; the session check confirms it.
  const [user, setUserState] = useState<ApiUser | null>(cachedUser);
  const [checked, setChecked] = useState(() => cachedUser() !== null);
  const setUser = useCallback((next: ApiUser | null) => {
    setUserState(next);
    try { if (next) sessionStorage.setItem(USER_KEY, JSON.stringify(next)); else sessionStorage.removeItem(USER_KEY); } catch { /* not cached */ }
  }, []);
  const [notice, setNoticeState] = useState<Notice | null>(null);
  // Whether the sign-in gate (register, reset, a sign-in error) was asked for is decided once, on arrival: the gate cleans the
  // address up, and the page must not fall back to the guest view in the middle of showing its message.
  const [gateOpen, setGateOpen] = useState(() => loginPanelRequested(window.location.search));
  // Guest mode: the page opens without a session; any login-only part sends the visitor to the login server and back to this page.
  const requireLogin = useCallback(() => window.location.assign(ssoLoginUrl(currentReturnTo(window.location))), []);
  const [busy, setBusy] = useState(false);
  const submitLock = useRef(false);
  // Idempotency key of the order being confirmed. The same order content keeps the key, so a retry after a timeout
  // is replayed by the server instead of placed twice; changed content (a new price) gets a new key.
  const orderRequest = useRef<{ signature: string; key: string } | null>(null);
  const userRef = useRef(user);
  userRef.current = user;
  // An error stays until dismissed; the same error twice in a row is not announced again.
  const setNotice = useCallback((text: string) => {
    if (!text) { setNoticeState(null); return; }
    setNoticeState((current) => current?.kind === "error" && current.text === text && !current.expired ? current : { text, kind: "error" });
  }, []);
  const notify = useCallback((text: string) => setNoticeState({ text, kind: "success" }), []);
  const lastPollError = useRef(new Map<string, string>());
  const expireSession = useCallback(() => {
    if (!userRef.current) return;
    setUser(null);
    setNoticeState({ text: "세션이 만료되었습니다. 다시 로그인해 주세요.", kind: "error", expired: true });
  }, [setUser]);
  // A background refresh failing again with the same message stays quiet; success re-arms it.
  const reportPollError = useCallback((key: string, cause: unknown, fallback: string) => {
    if ((cause as { status?: number })?.status === 401) { expireSession(); return; }
    const text = errorText(cause, fallback);
    if (lastPollError.current.get(key) === text) return;
    lastPollError.current.set(key, text);
    setNotice(text);
  }, [expireSession, setNotice]);
  const [health, setHealth] = useState("확인 중");
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [heatmapExchangeOverride, setHeatmapExchangeOverride] = useState<string | undefined>(() => new URLSearchParams(location.search).get("exchange") || undefined);
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchTabValue, setSearchTabValue] = useState<SearchTab>("recommended");
  const [recommendSort, setRecommendSort] = useState<RecommendSort>("value");
  const [htsId, setHtsId] = useState("");
  const [htsIdInput, setHtsIdInput] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [settingsMessage, setSettingsMessage] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [settingsBusy, setSettingsBusy] = useState(false);
  // 세무거주지는 국적과 동일하게 자동 적용하고 화면에서 바꾸지 않습니다(서버 기능은 그대로).
  const [, setTaxResidency] = useState<TaxResidency | null>(null);
  const [ownershipGraph, setOwnershipGraph] = useState<OwnershipGraphData>();
  const [ownershipLoading, setOwnershipLoading] = useState(() => ["/ownership", "/ownership/"].includes(location.pathname));
  const [, setTaxResidencyValue] = useState("");
  const [range, setRange] = useState<string>("1d");
  const [chartMode, setChartMode] = useState<"candle" | "line">("candle");
  const [interval, setIntervalSeconds] = useState<number | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [candleInterval, setCandleInterval] = useState(600);
  // Same 상단·하단 지표 as Discord /그래프; the choice is a per-viewer convenience kept in this browser.
  const [indicators, setIndicators] = useState<IndicatorChoice>(() => {
    try { const saved = JSON.parse(localStorage.getItem(INDICATOR_KEY) ?? "null"); if (Array.isArray(saved?.upper) && Array.isArray(saved?.lower)) return saved; } catch { /* default */ }
    return DEFAULT_INDICATORS;
  });
  const [indicatorData, setIndicatorData] = useState<IndicatorData>({ upper: {}, lower: {} });
  const [indicatorCatalog, setIndicatorCatalog] = useState<IndicatorCatalog>(INDICATOR_CATALOG);
  const indicatorQuery = `${indicators.upper.join(",")}|${indicators.lower.join(",")}`;
  // 관심종목: a per-viewer convenience kept in this browser.
  const [watch, setWatch] = useState<string[]>(() => { try { const saved = JSON.parse(localStorage.getItem(WATCH_KEY) ?? "[]"); return Array.isArray(saved) ? saved : []; } catch { return []; } });
  const toggleWatch = (code: string) => setWatch((current) => {
    const next = current.includes(code) ? current.filter((item) => item !== code) : [...current, code];
    try { localStorage.setItem(WATCH_KEY, JSON.stringify(next)); } catch { /* not remembered */ }
    return next;
  });
  const indicatorRef = useRef(indicators);
  const catalogLoaded = useRef(false);
  indicatorRef.current = indicators;
  function chooseIndicators(next: IndicatorChoice) {
    setIndicators(next);
    try { localStorage.setItem(INDICATOR_KEY, JSON.stringify(next)); } catch { /* not remembered */ }
  }
  const [points, setPoints] = useState<Point[]>([]);
  const [quote, setQuote] = useState<{ code: string; price: Money; at: number } | null>(null);
  const [pricedAt, setPricedAt] = useState(0);
  const marketSeq = useRef(0);
  const pathname = location.pathname;
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [drMovements, setDrMovements] = useState<DrMovement[]>([]);
  const [drCustodyInstructions, setDrCustodyInstructions] = useState<DrCustodyInstruction[]>([]);
  const [drDelegations, setDrDelegations] = useState<DrVotingDelegation[]>([]);
  const [receivedDrDelegations, setReceivedDrDelegations] = useState<ReceivedDrVotingDelegation[]>([]);
  const [drVotingAgenda, setDrVotingAgenda] = useState<DrVotingAgenda[]>([]);
  const [loanSchedules, setLoanSchedules] = useState<LoanScheduleView[]>([]);
  const [credit, setCredit] = useState<CreditReport | null>(null);
  const [orderFilter, setOrderFilter] = useState<"open" | "all">("open");
  const [book, setBook] = useState<Book>({ bids: [], asks: [] });
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("1");
  const [drQuantity, setDrQuantity] = useState("1");
  const [drVoteQuantity, setDrVoteQuantity] = useState("1");
  const [drProxyUserId, setDrProxyUserId] = useState("");
  const [price, setPrice] = useState("");
  const [orderType, setOrderType] = useState<"limit" | "market" | "current">("limit");
  const [review, setReview] = useState(false);
  const [tool, setTool] = useState<Tool>("select");
  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const drRequestKeys = useRef(new Map<string, string>());
  // Drawings are saved per user and instrument once the server copy is loaded.
  const drawingsLoadedFor = useRef("");
  const savedDrawings = useRef("[]");

  const selected = instruments.find((item) => item.code === selectedCode) ?? null;
  const defaultHeatmapExchange = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    const exchanges = [...new Set(instruments.map((item) => item.exchange_id).filter((item): item is string => Boolean(item)))];
    return exchanges.find((item) => term && item.toLocaleLowerCase() === term) ?? selected?.exchange_id ?? exchanges[0];
  }, [instruments, search, selected?.exchange_id]);
  const heatmapExchange = heatmapExchangeOverride ?? defaultHeatmapExchange;
  const filtered = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    // Exchange rates show as rate cards only; they never open the live chart.
    const tradable = instruments.filter((item) => item.type !== "forex");
    if (!term) return searchTabValue === "recommended" ? recommended(tradable, recommendSort) : tradable.filter((item) => searchTab(item) === searchTabValue);
    return tradable.filter((item) => `${item.code} ${item.name} ${item.type} ${instrumentType(item)}`.toLocaleLowerCase().includes(term))
      .sort((a, b) => Number(b.code.toLocaleLowerCase() === term) - Number(a.code.toLocaleLowerCase() === term));
  }, [instruments, search, searchTabValue, recommendSort]);
  const tabCounts = useMemo(() => {
    const counts: Partial<Record<SearchTab, number>> = { recommended: Math.min(20, instruments.length) };
    for (const item of instruments) if (item.type !== "forex") counts[searchTab(item)] = (counts[searchTab(item)] ?? 0) + 1;
    return counts;
  }, [instruments]);

  // Search combobox: the arrow keys move through the results, Enter picks the highlighted one.
  const [activeIndex, setActiveIndex] = useState(0);
  const visibleResults = filtered.slice(0, 200);
  const activeResult = visibleResults[Math.min(activeIndex, visibleResults.length - 1)];
  useEffect(() => { setActiveIndex(0); }, [search, searchTabValue, recommendSort]);
  useEffect(() => {
    if (searchOpen && activeResult) document.getElementById(`search-opt-${activeResult.code}`)?.scrollIntoView({ block: "nearest" });
  }, [activeResult?.code, searchOpen]);

  // The old /commands/<name> links forward to the matching page.
  const legacyTarget = user && pathname.startsWith("/commands") ? legacyCommandPath(decodeURIComponent(pathname.split("/").filter(Boolean)[1] ?? "")) : null;
  useEffect(() => { if (legacyTarget) navigate(legacyTarget, true); }, [legacyTarget]);

  // Only a refused session signs out; a slow or unreachable server keeps the user and tries again.
  const authTimer = useRef<number | undefined>(undefined);
  const authAlive = useRef(true);
  const authenticate = useCallback(async (): Promise<void> => {
    try {
      const { user: next } = await api<{ user: ApiUser }>("/api/auth/session");
      if (!authAlive.current) return;
      setUser(next);
      setChecked(true);
    } catch (cause) {
      if (!authAlive.current) return;
      if (signedOut(cause)) { setUser(null); setChecked(true); return; }
      authTimer.current = window.setTimeout(() => void authenticate(), 3000);
    }
  }, [setUser]);

  const userRole = user?.role;
  const refreshAccount = useCallback(async () => {
    type Part<T> = { status: number; body: T & { error?: string } };
    const bundle = await api<{
      portfolio: Part<{ portfolio: Portfolio }>; orders: Part<{ orders: Order[] }>;
      loans: Part<{ loans: LoanScheduleView[] }>; conversions: Part<{ instructions: DrCustodyInstruction[] }>;
      credit: Part<Partial<CreditReport>>; taxResidency: Part<TaxResidency>;
    }>("/api/me/account-bundle");
    const required = [bundle.portfolio, bundle.orders, bundle.loans, bundle.conversions];
    const failed = required.find((part) => part.status !== 200);
    if (failed) throw apiError(failed.status, failed.body);
    // Tax residency exists only for ordinary user accounts (the server refuses admin, nation and
    // company accounts); like the credit panel, it must never block the wallet or the instrument list.
    if (userRole === "user" && bundle.taxResidency.status === 200) {
      setTaxResidency(bundle.taxResidency.body);
      setTaxResidencyValue(bundle.taxResidency.body.taxResidency ?? "");
    } else setTaxResidency(null);
    // The credit panel is optional; a failed report must not hide the wallet.
    const report = bundle.credit.body;
    setCredit(bundle.credit.status === 200 && typeof report.score === "number" ? report as CreditReport : null);
    setPortfolio(bundle.portfolio.body.portfolio);
    setOrders(bundle.orders.body.orders);
    setLoanSchedules(bundle.loans.body.loans);
    setDrCustodyInstructions(bundle.conversions.body.instructions);
  }, [userRole]);
  // Figures in the citizen's own currency use the exchange rate the server read when the account was loaded.
  const [ratesBusy, setRatesBusy] = useState(false);
  const refreshRates = useCallback(async () => {
    setRatesBusy(true);
    try { await refreshAccount(); lastPollError.current.delete("account"); } catch (cause) { reportPollError("account", cause, "계좌 정보를 불러오지 못했습니다."); } finally { setRatesBusy(false); }
  }, [refreshAccount, reportPollError]);
  /** Background and after-action refresh of the account: auth errors end the session, other errors show once. */
  const refreshAccountQuiet = useCallback(async () => {
    try { await refreshAccount(); lastPollError.current.delete("account"); } catch (cause) { reportPollError("account", cause, "계좌 정보를 불러오지 못했습니다."); }
  }, [refreshAccount, reportPollError]);

  useEffect(() => {
    authAlive.current = true;
    let live = true;
    void fetch("/api/health")
      .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
      .then((result) => { if (live) setHealth(result.python === "connected" ? "연결됨" : "Python 연결 끊김"); })
      .catch(() => { if (live) setHealth("연결 끊김"); });
    void authenticate();
    return () => { live = false; authAlive.current = false; window.clearTimeout(authTimer.current); };
  }, [authenticate]);

  // Any 401 after sign-in means the session is gone: drop to the login screen and stop polling (polls need `user`).
  useEffect(() => onUnauthorized(expireSession), [expireSession]);

  // A success notice clears itself after 4 seconds; an error stays until dismissed.
  useEffect(() => {
    if (notice?.kind !== "success") return;
    const timer = window.setTimeout(() => setNoticeState(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // The instrument list (prices) refreshes every 5s while the tab is visible; the first answer also picks the instrument.
  const loadInstruments = useCallback(async () => {
    try {
      const market = await api<{ instruments: Instrument[] }>("/api/market/instruments");
      lastPollError.current.delete("instruments");
      setInstruments(market.instruments);
      setPricedAt(Date.now());
      const linked = new URLSearchParams(location.search).get("code");
      setSelectedCode((current) => current || market.instruments.find((item) => item.code === linked)?.code || market.instruments[0]?.code || "");
    } catch (cause) { reportPollError("instruments", cause, "종목 목록을 불러오지 못했습니다."); }
  }, [reportPollError]);
  usePolling(loadInstruments, 5000, true);
  usePolling(refreshAccountQuiet, 15000, Boolean(user));

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    // DR extras load once; a failure here only leaves those panels empty.
    void api<{ movements: DrMovement[] }>("/api/me/dr/movements")
      .then(({ movements }) => { if (!cancelled) setDrMovements(movements); }).catch(() => undefined);
    void api<{ delegations: DrVotingDelegation[] }>("/api/me/dr/voting-delegations")
      .then(({ delegations }) => { if (!cancelled) setDrDelegations(delegations); }).catch(() => undefined);
    void api<{ delegations: ReceivedDrVotingDelegation[] }>("/api/me/dr/proxy-voting-delegations")
      .then(({ delegations }) => { if (!cancelled) setReceivedDrDelegations(delegations); }).catch(() => undefined);
    void api<{ agenda: DrVotingAgenda[] }>("/api/me/dr/voting-agenda")
      .then(({ agenda }) => { if (!cancelled) setDrVotingAgenda(agenda); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [user]);

  useEffect(() => {
    if (!user) return;
    void api<{ htsId: string | null }>("/api/hts/account")
      .then(({ htsId: current }) => { setHtsId(current ?? ""); setHtsIdInput(current ?? ""); })
      .catch((cause) => setNotice(errorText(cause, "나루 계정 정보를 불러오지 못했습니다.")));
  }, [user]);

  useEffect(() => {
    if (!user || !["/account", "/account/", "/ownership", "/ownership/"].includes(pathname)) return;
    let active = true;
    setOwnershipLoading(true);
    void api<{ graph: OwnershipGraphData }>("/api/me/ownership-graph")
      .then(({ graph }) => { if (active) setOwnershipGraph(graph); })
      .catch(() => { if (active) setOwnershipGraph(undefined); })
      .finally(() => { if (active) setOwnershipLoading(false); });
    return () => { active = false; };
  }, [user, pathname]);

  // The search drops down under the top-bar field, like a portal's: Escape or a click elsewhere closes it.
  const searchBoxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!searchOpen) return;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") { setSearchOpen(false); searchInputRef.current?.blur(); } };
    const onDown = (event: MouseEvent) => { if (!searchBoxRef.current?.contains(event.target as Node)) setSearchOpen(false); };
    window.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onDown);
    return () => { window.removeEventListener("keydown", onKeyDown); document.removeEventListener("mousedown", onDown); };
  }, [searchOpen]);

  const refreshMarket = useCallback(async (code: string, timeframe: string, candleSeconds: number | null) => {
    if (!code) return;
    // Only the newest request may write: a slower answer for the previous instrument or range is dropped.
    const seq = ++marketSeq.current;
    try {
      const query = new URLSearchParams({ range: timeframe });
      if (candleSeconds) query.set("interval", String(candleSeconds));
      if (indicatorRef.current.upper.length) query.set("upper", indicatorRef.current.upper.join(","));
      if (indicatorRef.current.lower.length) query.set("lower", indicatorRef.current.lower.join(","));
      if (!catalogLoaded.current) query.set("catalog", "1");
      const [chart, orderBook] = await Promise.all([
        api<{ points: Point[]; candles?: Candle[]; interval?: number; indicators?: IndicatorData; catalog?: IndicatorCatalog }>(`/api/market/chart/${encodeURIComponent(code)}?${query}`),
        api<Book>(`/api/market/book/${encodeURIComponent(code)}`),
      ]);
      if (seq !== marketSeq.current) return;
      lastPollError.current.delete("market");
      const nextPoints = chart.points.map((point) => ({ ...point, price: Number(point.price) }));
      setPoints(nextPoints);
      setCandles((chart.candles ?? []).map((candle) => ({ ...candle, open: Number(candle.open), high: Number(candle.high), low: Number(candle.low), close: Number(candle.close) })));
      if (chart.interval) setCandleInterval(chart.interval);
      setIndicatorData(chart.indicators ?? { upper: {}, lower: {} });
      if (chart.catalog) { setIndicatorCatalog(chart.catalog); catalogLoaded.current = true; }
      setBook(orderBook);
    } catch (cause) {
      if (seq === marketSeq.current) reportPollError("market", cause, "시세를 불러오지 못했습니다.");
    }
  }, [reportPollError]);

  // Runs now and every 5s while the tab is visible; a new instrument, range or indicator choice restarts it.
  usePolling(() => refreshMarket(selectedCode, range, interval), 5000, Boolean(selectedCode), [selectedCode, range, interval, indicatorQuery, refreshMarket]);
  // The selected instrument's latest price, straight from the server every second (a one-row read, unlike the
  // instrument list). This is the price a "현재가" order is made at, and the server checks it again on arrival.
  const pollQuote = useCallback(async () => {
    const code = selectedCode;
    if (!code) return;
    try {
      const fresh = await api<{ price: Money }>(`/api/market/quote/${encodeURIComponent(code)}`);
      lastPollError.current.delete("quote");
      setQuote({ code, price: fresh.price, at: Date.now() });
    } catch (cause) { reportPollError("quote", cause, "현재가를 불러오지 못했습니다."); }
  }, [selectedCode, reportPollError]);
  usePolling(pollQuote, 1000, Boolean(selectedCode), [selectedCode]);
  useEffect(() => { if (selected) setPrice(String(selected.price)); }, [selected?.code]);

  useEffect(() => {
    if (!selectedCode || !user) return;
    let cancelled = false;
    drawingsLoadedFor.current = "";
    setDrawings([]);
    api<{ drawings: Drawing[] }>(`/api/market/drawings/${encodeURIComponent(selectedCode)}`)
      .then((result) => {
        if (cancelled) return;
        savedDrawings.current = JSON.stringify(result.drawings);
        drawingsLoadedFor.current = selectedCode;
        setDrawings(result.drawings);
      })
      .catch((cause) => { if (!cancelled) setNotice(errorText(cause, "차트 선을 불러오지 못했습니다.")); });
    return () => { cancelled = true; };
  }, [selectedCode, user]);

  useEffect(() => {
    const code = drawingsLoadedFor.current;
    if (!code || code !== selectedCode) return;
    const serialized = JSON.stringify(drawings);
    if (serialized === savedDrawings.current) return;
    const timer = window.setTimeout(() => {
      api<{ drawings: Drawing[] }>(`/api/market/drawings/${encodeURIComponent(code)}`, { method: "POST", body: JSON.stringify({ drawings }) })
        .then(() => { savedDrawings.current = serialized; })
        .catch((cause) => setNotice(errorText(cause, "차트 선을 저장하지 못했습니다.")));
    }, 600);
    return () => window.clearTimeout(timer);
  }, [drawings, selectedCode]);

  async function submitOrder() {
    // A double click is stopped here, before the request; the clientOrderId covers a retry after a lost response.
    if (submitLock.current || !orderReady) return;
    submitLock.current = true;
    setBusy(true);
    try {
      const order = { assetCode: selectedCode, side, quantity: Number(quantity), orderType: orderType === "limit" ? "limit" : orderType, limitPrice: exactMoney(orderPrice) };
      const signature = JSON.stringify(order);
      if (orderRequest.current?.signature !== signature) orderRequest.current = { signature, key: requestKey() };
      const result = await api<{ message: string }>("/api/market/orders", {
        method: "POST", body: JSON.stringify({ ...order, clientOrderId: orderRequest.current.key }),
      });
      orderRequest.current = null;
      setReview(false);
      notify(result.message);
      setOrderFilter("open");
      await Promise.all([refreshAccountQuiet(), refreshMarket(selectedCode, range, interval), loadInstruments()]);
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "price_changed") {
        // The server holds the latest price and refused the old one. Take it now; the dialog announces the new price.
        await pollQuote();
      } else if ((cause as { status?: number })?.status !== 401) setNotice(errorText(cause, "주문을 제출하지 못했습니다."));
    } finally {
      submitLock.current = false;
      setBusy(false);
    }
  }

  async function convertDr(action: "issue" | "redeem") {
    if (!selected || selected.type !== "depositary_receipt") return;
    const qty = Number(drQuantity);
    if (!Number.isSafeInteger(qty) || qty < 1) { setNotice("DR 수량은 1 이상의 정수여야 합니다."); return; }
    const verb = action === "issue" ? "원주를 예탁해 DR을 발행" : "보유 DR을 원주로 상환";
    if (!window.confirm(`${selected.code} ${qty} DR을 ${verb}할까요?`)) return;
    if (submitLock.current) return;
    const requestKey = `${user?.userId ?? "session"}:${selected.code}:${action}:${qty}`;
    const storageKey = `inazina.dr.request.${requestKey}`;
    let referenceKey = drRequestKeys.current.get(requestKey);
    if (!referenceKey) {
      try { referenceKey = window.sessionStorage.getItem(storageKey) ?? undefined; } catch { /* session storage may be unavailable */ }
    }
    if (!referenceKey) {
      referenceKey = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      drRequestKeys.current.set(requestKey, referenceKey);
      try { window.sessionStorage.setItem(storageKey, referenceKey); } catch { /* in-memory retry still works */ }
    }
    submitLock.current = true;
    setBusy(true);
    try {
      const result = await api<{ success: boolean; result: { status: string; message: string } }>("/api/dr/convert", { method: "POST", body: JSON.stringify({
        drCode: selected.code, action, quantity: qty,
        referenceKey,
      }) });
      notify(result.result.message);
      drRequestKeys.current.delete(requestKey);
      try { window.sessionStorage.removeItem(storageKey); } catch { /* ignore unavailable session storage */ }
      try {
        const [, , history] = await Promise.all([
          refreshAccount(), refreshMarket(selectedCode, range, interval),
          api<{ movements: DrMovement[] }>("/api/me/dr/movements"),
        ]);
        setDrMovements(history.movements);
      } catch {
        setNotice(`${result.result.message} 화면 새로고침에 실패했습니다. 다시 불러와 잔액과 이력을 확인해주세요.`);
      }
    } catch (cause) {
      if (definitiveRefusal(cause)) {
        // The server refused this request outright, so the next attempt must be a new one.
        drRequestKeys.current.delete(requestKey);
        try { window.sessionStorage.removeItem(storageKey); } catch { /* ignore unavailable session storage */ }
        if ((cause as ApiError).status !== 401) setNotice(errorText(cause, "DR 전환 요청이 거절되었습니다."));
      } else setNotice(`${errorText(cause, "DR 전환 요청 결과를 확인하지 못했습니다.")} 같은 수량과 방향으로 재시도하면 중복 전환되지 않습니다.`);
    } finally { submitLock.current = false; setBusy(false); }
  }

  async function cancelDrConversion(item: DrCustodyInstruction) {
    if (!window.confirm(`${item.reference_key} DR 전환 요청을 취소할까요? 예약된 입력 수량이 해제됩니다.`)) return;
    setBusy(true);
    try {
      const result = await api<{ success: boolean; result: { status: string } }>(
        "/api/dr/convert/cancel", { method: "POST", body: JSON.stringify({ referenceKey: item.reference_key }) },
      );
      notify(result.result.status === "cancelled" ? "DR 전환 요청을 취소하고 예약 수량을 해제했습니다." : "취소 처리했습니다.");
      await refreshAccountQuiet();
    } catch (cause) {
      setNotice(errorText(cause, "DR 전환 요청을 취소하지 못했습니다."));
    } finally { setBusy(false); }
  }

  async function delegateDrVotes() {
    if (!selected || selected.type !== "depositary_receipt") return;
    const quantityValue = Number(drVoteQuantity);
    const proxyUserId = drProxyUserId.trim();
    if (!Number.isSafeInteger(quantityValue) || quantityValue < 1 || !proxyUserId) {
      setNotice("대리 사용자 ID와 1 이상의 정수 DR 수량을 입력해주세요.");
      return;
    }
    setBusy(true);
    try {
      await api("/api/dr/voting-delegations", { method: "POST", body: JSON.stringify({
        drCode: selected.code, proxyUserId, quantity: quantityValue,
      }) });
      const { delegations } = await api<{ delegations: DrVotingDelegation[] }>(
        "/api/me/dr/voting-delegations");
      setDrDelegations(delegations);
      notify(`${selected.code} 의결권 ${quantityValue} DR을 ${proxyUserId}에게 위임했습니다.`);
    } catch (cause) {
      setNotice(errorText(cause, "의결권 위임을 처리하지 못했습니다."));
    } finally { setBusy(false); }
  }

  async function revokeDrVotes(delegation: DrVotingDelegation) {
    if (!window.confirm(`${delegation.dr_code} ${delegation.dr_quantity} DR 의결권 위임을 철회할까요?`)) return;
    setBusy(true);
    try {
      await api(`/api/dr/voting-delegations/${delegation.delegation_id}/revoke`, {
        method: "POST", body: JSON.stringify({ delegationId: delegation.delegation_id }),
      });
      const { delegations } = await api<{ delegations: DrVotingDelegation[] }>(
        "/api/me/dr/voting-delegations");
      setDrDelegations(delegations);
      notify("의결권 위임을 철회했습니다.");
    } catch (cause) {
      setNotice(errorText(cause, "의결권 위임을 철회하지 못했습니다."));
    } finally { setBusy(false); }
  }

  async function castDrVote(item: DrVotingAgenda, choice: string) {
    setBusy(true);
    try {
      await api("/api/dr/votes", { method: "POST", body: JSON.stringify({
        meetingId: item.meetingId, agendaItemId: item.agendaItemId, choice,
      }) });
      const { agenda } = await api<{ agenda: DrVotingAgenda[] }>("/api/me/dr/voting-agenda");
      setDrVotingAgenda(agenda);
      notify(`${item.agendaTitle}: ${choice}로 투표했습니다. 기준일 의결권 ${money(item.votingPower)}표가 반영됩니다.`);
    } catch (cause) {
      setNotice(errorText(cause, "의결권 투표를 처리하지 못했습니다."));
    } finally { setBusy(false); }
  }

  async function cancelOrder(order: Order) {
    if (!window.confirm(`주문 #${order.order_id} (${order.asset_code} ${order.side === "buy" ? "매수" : "매도"} 잔량 ${money(order.remaining_quantity, 3)})을 취소할까요?\n매수 예치금이나 매도 수량은 바로 돌려받습니다.`)) return;
    setBusy(true);
    try {
      const result = await api<{ message: string }>(`/api/market/orders/${order.order_id}/cancel`, { method: "POST", body: "{}" });
      notify(result.message);
      await Promise.all([refreshAccountQuiet(), refreshMarket(selectedCode, range, interval)]);
    } catch (cause) {
      setNotice(errorText(cause, "주문을 취소하지 못했습니다."));
    } finally {
      setBusy(false);
    }
  }

  async function saveHtsId(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSettingsBusy(true);
    setSettingsError("");
    setSettingsMessage("");
    try {
      const result = await api<{ htsId: string }>("/api/hts/account", { method: "POST", body: JSON.stringify({ htsId: htsIdInput, password: currentPassword }) });
      setHtsId(result.htsId);
      setHtsIdInput(result.htsId);
      setCurrentPassword("");
      setSettingsMessage("아이디를 저장했습니다.");
    } catch (cause) {
      const reason = errorText(cause, "");
      setSettingsError(reason === "invalid_credentials" ? "현재 비밀번호가 일치하지 않습니다." : reason || "아이디를 저장하지 못했습니다.");
    } finally {
      setSettingsBusy(false);
    }
  }

  function chooseInstrument(code: string) {
    setSearchOpen(false);
    if (location.pathname !== "/") navigate(`/?code=${encodeURIComponent(code)}`);
    const instrument = instruments.find((item) => item.code === code);
    if (!instrument) { setNotice(`${code}은(는) 지금 거래 중인 종목이 아닙니다.`); return; }
    setSelectedCode(instrument.code);
    setPrice(String(instrument.price));
    setSearchOpen(false);
  }

  if (!user && !checked) return <div className="stage"><Backdrop /><p className="page-loading" role="status">불러오는 중…</p></div>;
  // After the session expires the login screen carries the notice; "다시 로그인" dismisses it and focuses the form.
  const relogin = () => { setNoticeState(null); requireLogin(); };
  const toast = notice && <div className={`toast ${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}><span>{notice.text}</span>{notice.expired && <button type="button" onClick={relogin}>다시 로그인</button>}<button type="button" onClick={() => setNoticeState(null)}>닫기</button></div>;
  // Visitors without a session see the page; the sign-in gate opens only when asked for (register, reset, a sign-in error).
  if (!user && gateOpen) return <div className="stage"><Backdrop />{toast}<SsoGate autoRedirect={false} onBrowse={() => setGateOpen(false)} /></div>;
  const guest = !user;
  const me: ApiUser = user ?? GUEST_USER;
  // A login-only part of the page stays visible but blurred; any click or Enter on it asks the visitor to sign in.
  const lock = (label: string) => guest ? {
    "data-login-label": label,
    onClickCapture: (event: React.SyntheticEvent) => { event.preventDefault(); event.stopPropagation(); requireLogin(); },
    onKeyDownCapture: (event: React.KeyboardEvent) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); requireLogin(); } },
  } : {};
  const locked = guest ? " login-locked" : "";

  // The latest known price is whichever of the chart's last trade and the refreshed instrument list was read last.
  const quoteFresh = quote?.code === selectedCode && (!selected || quote.at >= pricedAt);
  const current = quoteFresh ? quote!.price : selected?.price ?? quote?.price ?? 0;
  const currentAt = quoteFresh ? quote!.at : selected ? pricedAt || null : null;
  // Market orders are priced by the server against the live book; this estimate uses the visible levels.
  const marketEstimate = (() => {
    const levels = (side === "buy" ? book.asks : book.bids) ?? [];
    let needed = Number(quantity) || 0;
    for (const level of levels) { needed -= Number(level.quantity); if (needed <= 0) return { price: String(level.limit_price), enough: true }; }
    return { price: levels.length ? String(levels[levels.length - 1].limit_price) : "", enough: false };
  })();
  const orderPrice = orderType === "market" ? marketEstimate.price : orderType === "current" ? String(current || "") : price;
  const orderReady = Boolean(selected) && validMoney(orderPrice, true) && wholeNumber(quantity) && Number(quantity) > 0 && (orderType !== "market" || marketEstimate.enough);
  const orderAmount = validMoney(orderPrice) && validMoney(quantity) ? moneyMultiply(orderPrice, quantity) : "0";
  // Amounts follow the account's nationality; instrument prices keep their trading currency.
  const homeCurrency = portfolio?.national_currency || me.currency || "";
  const fxTerm = search.trim().toLocaleLowerCase();
  const fxPrice = (code: string, base: string) => code === base ? 1 : instruments.find((item) => item.type === "forex" && item.currency_code === code)?.price;
  // "기준화폐:검색화폐" — how much of the searched currency one unit of the user's currency buys.
  const fxRates = fxTerm && homeCurrency ? instruments.filter((item) => item.type === "forex" && item.currency_code && item.currency_code !== homeCurrency
    && `${item.currency_code} ${item.name}`.toLocaleLowerCase().includes(fxTerm)).flatMap((item) => {
    const home = fxPrice(homeCurrency, item.quote_currency || "");
    return home && Number(item.price) ? [{ code: item.currency_code!, name: item.name, rate: moneyDivide(home, item.price, 6), inverse: moneyDivide(item.price, home, 4) }] : [];
  }) : [];
  const homeDigits = portfolio?.home_decimal_places ?? 0;
  const homeRate = portfolio?.home_rate_nwx || 1;
  // Portfolio totals are 기준 통화; the Discord wallet shows them in the home currency.
  const inHome = (nwx: Money) => moneyDivide(nwx, homeRate, homeDigits);
  const holdings = (portfolio?.stocks ?? []).filter((item) => item.qty);
  const visibleOrders = orders.filter((order) => orderFilter === "all" || OPEN.has(order.status));
  const openCount = orders.filter((order) => OPEN.has(order.status)).length;

  // Leaving a country or company account ($login) returns to the administrator's own session.
  // The administrator comes back here, in the HTS, as themselves; the wallet and orders reload for them.
  const logout = async () => {
    const delegated = Boolean(user?.delegatedBy);
    let result: { returnedTo?: ApiUser; logoutUrl?: string };
    try {
      result = await api<{ returnedTo?: ApiUser; logoutUrl?: string }>("/api/auth/logout", { method: "POST" });
    } catch (cause) {
      if (delegated) { setNotice(errorText(cause, "관리자 계정으로 돌아가지 못했습니다. 잠시 후 다시 시도해주세요.")); return; }
      result = {};
    }
    clearToken();
    if (result.returnedTo) {
      setPortfolio(null); setOrders([]); setCredit(null);
      setUser(result.returnedTo);
      notify(`관리자 ${result.returnedTo.nickname} 계정으로 돌아왔습니다.`);
      return;
    }
    setUser(null);
    const leave = logoutRedirect(result); // ends the single sign-on session too
    if (leave) { window.location.assign(leave); return; }
    if (location.pathname !== "/") navigate("/");
  };
  const topbar = (here: "home" | "account" | "services") => <><header className="hts-topbar">
    <a className="brand" href="/" aria-label="나루 홈"><Logo className="brand-logo" /></a>
    <div className="top-search" ref={searchBoxRef}>
      <label className="top-search-field"><svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M20 20l-4-4" /></svg>
        <input ref={searchInputRef} value={search} role="combobox" aria-autocomplete="list" aria-label="종목·상품코드·금리·지수 검색" aria-expanded={searchOpen} aria-controls="top-search-list" aria-activedescendant={searchOpen && activeResult ? `search-opt-${activeResult.code}` : undefined} placeholder="종목명·상품코드·국가·거래소 검색" onFocus={() => setSearchOpen(true)} onChange={(event) => { setSearch(event.target.value); setSearchOpen(true); }} onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            if (!searchOpen) { setSearchOpen(true); return; }
            if (visibleResults.length) setActiveIndex((index) => (Math.min(index, visibleResults.length - 1) + (event.key === "ArrowDown" ? 1 : -1) + visibleResults.length) % visibleResults.length);
          } else if (event.key === "Enter" && searchOpen && activeResult) { event.preventDefault(); chooseInstrument(activeResult.code); }
        }} />
        {search && <button type="button" className="top-search-clear" aria-label="검색어 지우기" onClick={() => { setSearch(""); searchInputRef.current?.focus(); }}>×</button>}
      </label>
      {searchOpen && <section id="top-search-pop" className="search-pop" aria-label="종목 검색 결과">{!search.trim() && <div className="search-tabs" role="tablist" aria-label="상품 종류" onKeyDown={tabListKeys}>{SEARCH_TABS.filter(([id]) => id === "recommended" || tabCounts[id]).map(([id, label]) => <button key={id} type="button" role="tab" tabIndex={searchTabValue === id ? 0 : -1} aria-selected={searchTabValue === id} className={searchTabValue === id ? "active" : ""} onClick={() => setSearchTabValue(id)}>{label}<span>{tabCounts[id] ?? 0}</span></button>)}</div>}{!search.trim() && searchTabValue === "recommended" && <><div className="recommend-sorts" role="group" aria-label="추천 기준">{RECOMMEND_SORTS.map(([id, label]) => <button key={id} type="button" aria-pressed={recommendSort === id} className={recommendSort === id ? "active" : ""} onClick={() => setRecommendSort(id)}>{label}</button>)}</div><p className="search-caption">{RECOMMEND_SORTS.find(([id]) => id === recommendSort)![2]}</p></>}<div className="search-results" id="top-search-list" role="listbox" aria-label="종목 검색 결과">{fxRates.map((fx) => <div key={`fx-${fx.code}`} role="group" className="search-result fx-rate" aria-label={`${homeCurrency}:${fx.code} 환율`}><span><span className="search-name"><b>{homeCurrency}:{fx.code}</b><small>{fx.name} · 환율</small></span></span><span className="search-price"><strong>1 : {money(fx.rate, 6)}</strong><small>1 {fx.code} = {money(fx.inverse, 4)} {homeCurrency}</small></span></div>)}{visibleResults.map((item, index) => <button type="button" role="option" id={`search-opt-${item.code}`} aria-selected={activeResult?.code === item.code} tabIndex={-1} key={item.code} className={`search-result${selectedCode === item.code ? " active" : ""}${activeResult?.code === item.code ? " kbd" : ""}`} onClick={() => chooseInstrument(item.code)}><span>{!search.trim() && searchTabValue === "recommended" && <em className="search-rank">{index + 1}</em>}<span className="search-name"><b>{item.name} <WarningBadge warning={item.warning} /></b><small>{item.code} · {instrumentType(item)}{!search.trim() && searchTabValue === "recommended" && recommendSort === "volume" ? (item.volume24h ? ` · 24시간 ${money(item.volume24h, 2)}주` : "") : item.trades24h ? ` · 24시간 ${money(item.trades24h)}건` : ""}</small></span></span><span className="search-price"><strong>{money(item.price, 2)} {item.quote_currency || homeCurrency}</strong>{item.change24h !== null && item.change24h !== undefined && <small className={item.change24h > 0 ? "up" : item.change24h < 0 ? "down" : undefined}>{item.change24h > 0 ? "+" : ""}{Number(item.change24h).toFixed(2)}%</small>}</span></button>)}{!filtered.length && !fxRates.length && <p className="empty">{search.trim() ? "거래 종목 결과가 없습니다. 상품 코드·금리·지수는 아래 관련 결과를 확인하세요." : searchTabValue === "recommended" ? "최근 24시간 동안 해당하는 종목이 없습니다." : "이 종류의 상품이 없습니다."}</p>}</div>{search.trim() && <SearchIndicators query={search} onExchangeSelect={setHeatmapExchangeOverride} />}</section>}
    </div>
    <nav className="top-nav" aria-label="서비스">{SERVICES.slice(0, 4).map((item) => <a key={item.path} href={item.path} aria-current={here === "services" && location.pathname.startsWith(item.path) ? "page" : undefined}>{item.label}</a>)}<a href={boardUrl()}>게시판</a></nav>
    <div className="top-actions">
      {here === "home" && <span className={`connection ${health === "연결됨" ? "ok" : health === "확인 중" ? "pending" : "bad"}`} role="img" aria-label={`API ${health}`} title={`API ${health}`}><i /></span>}
      {guest ? <button type="button" className="icon-button" onClick={requireLogin}>로그인</button> : <>
      <a className="header-action account-action" href="/account" aria-current={here === "account" ? "page" : undefined}>{me.nickname} <small>{htsId || "아이디 만들기"}</small></a>
      <NotificationBell />
      <button className="icon-button" title={me.delegatedBy ? "관리자 계정으로 돌아갑니다" : "로그아웃"} onClick={() => void logout()}>{me.delegatedBy ? "관리자로 돌아가기" : "로그아웃"}</button></>}
    </div>
    {me.delegatedBy && <p className="delegation-bar" role="status">관리자 {me.delegatedByName ?? me.delegatedBy}님이 <b>{ROLE_LABEL[me.role]} {me.nickname}</b>(으)로 로그인 중 · 모든 거래는 이 계정 명의로 처리됩니다.</p>}
  </header>{toast}</>;

  if (legacyTarget) return null;
  const service = servicePath(location.pathname);
  if (guest && !guestCanView(location.pathname)) return <div className="hts-shell stage">
    <Backdrop />
    {topbar("home")}
    <main className="guest-gate"><LoginRedirect go={requireLogin} /><a className="soft" href="/">시장 홈으로</a></main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;
  const payoutRoute = location.pathname.match(/^\/payouts\/(\d+)\/?$/);
  if (payoutRoute) return <div className="hts-shell stage payout-shell">
    <Backdrop />
    {topbar("account")}
    <main><PayoutPage payoutId={Number(payoutRoute[1])} /></main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;
  if (location.pathname === "/heatmap" || location.pathname === "/heatmap/") return <div className="hts-shell stage heatmap-page-shell">
    <Backdrop />
    {topbar("home")}
    <main><HeatmapPage instruments={instruments} exchangeId={heatmapExchange} onExchangeChange={setHeatmapExchangeOverride} onSelect={chooseInstrument} /></main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;
  if (location.pathname === "/credit" || location.pathname === "/credit/") return <div className="hts-shell stage portfolio-page-shell">
    <Backdrop />
    {topbar("account")}
    <main>{credit ? <CreditPage credit={credit} /> : <p className="page-loading" role="status">신용점수를 불러오는 중…</p>}</main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;
  if (location.pathname === "/portfolio" || location.pathname === "/portfolio/") return <div className="hts-shell stage portfolio-page-shell">
    <Backdrop />
    {topbar("home")}
    <main><PortfolioPage holdings={holdings} instruments={instruments} homeCurrency={homeCurrency} cash={portfolio?.cash ?? me.cash} totalAssets={portfolio?.grand_total ?? portfolio?.net_worth ?? 0} onRefreshRates={() => void refreshRates()} ratesBusy={ratesBusy} onSelect={(code) => { window.location.href = `/?code=${encodeURIComponent(code)}`; }} /></main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;
  if (service) return <div className="hts-shell stage service-shell">
    <Backdrop />
    {topbar("services")}
    <Suspense fallback={<p className="svc-empty">불러오는 중…</p>}>
      <ServicesApp path={service} instruments={instruments} portfolio={portfolio} orders={orders} userId={me.userId} homeCurrency={homeCurrency} homeDigits={homeDigits} cash={portfolio?.cash ?? me.cash} creditScore={credit?.score ?? me.creditScore} accounts={portfolio?.accounts ?? []} onDone={() => void refreshAccountQuiet()} />
    </Suspense>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;

  if (location.pathname === "/ownership" || location.pathname === "/ownership/") return <div className="hts-shell stage ownership-window-shell">
    <Backdrop />
    <main className="ownership-window">
      <header><div><p className="eyebrow">NARU · 나루</p><h1>보유 기업 지배구조</h1><p className="sub">보유 주식의 의결권과 지배하는 자회사 관계입니다.</p></div><a className="soft" href="/account">계정 페이지로</a></header>
      {ownershipLoading ? <div className="ownership-loading" role="status"><span className="ownership-spinner" aria-hidden="true" /><strong>보유 기업 지배구조를 불러오는 중…</strong></div> : <Suspense fallback={null}><OwnershipGraph graph={ownershipGraph} fullScreen /></Suspense>}
    </main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;

  if (location.pathname === "/account" || location.pathname === "/account/") return <div className="hts-shell stage account-page-shell">
    <Backdrop />
    {topbar("account")}
    <main className="account-page">
      <aside className="account-card" aria-label="프로필">
        <span className="account-avatar" aria-hidden="true">{me.nickname.slice(0, 1)}</span>
        <h1>{me.nickname}</h1>
        <p>{ROLE_LABEL[me.role] ?? me.role} · {me.nationality}</p>
        <dl>
          <div><dt>로그인 아이디</dt><dd>{htsId || "미설정"}</dd></div>
          <div><dt>지갑</dt><dd>{money(portfolio?.cash ?? me.cash, homeDigits)} {homeCurrency}</dd></div>
          <div><dt>순자산 <RefreshRateButton busy={ratesBusy} onRefresh={() => void refreshRates()} /></dt><dd>{money(totalInHome(portfolio?.net_worth ?? 0, portfolio, inHome), homeDigits)} {homeCurrency}</dd></div>
          {credit && <div><dt>신용점수</dt><dd>{credit.score}점</dd></div>}
        </dl>
        <button type="button" onClick={() => void logout()}>{me.delegatedBy ? "관리자로 돌아가기" : "로그아웃"}</button>
      </aside>
      <div className="account-sections">
        <PendingPayouts />
        <section className="account-section">
          <header><h2>로그인 아이디</h2><p>나루와 대시보드 로그인에 쓰는 아이디입니다. 영문, 숫자, 점, 밑줄, 하이픈으로 3~32자.</p></header>
          <form className="account-form" onSubmit={saveHtsId}>
            <label>아이디<input autoComplete="username" minLength={3} maxLength={32} pattern="[A-Za-z0-9_.-]{3,32}" value={htsIdInput} onChange={(event) => setHtsIdInput(event.target.value)} placeholder="예: inazina_user" required /></label>
            <label>현재 비밀번호<input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /></label>
            {settingsMessage && <p className="account-success" role="status">{settingsMessage}</p>}{settingsError && <p className="account-error" role="alert">{settingsError}</p>}
            <button className="account-save" disabled={settingsBusy || !htsIdInput}>{settingsBusy ? "저장 중…" : htsId ? "아이디 변경" : "아이디 설정"}</button>
          </form>
        </section>
        {!me.delegatedBy && <PasswordSection />}
        {portfolio?.kyc_profile && <section className="account-section">
          <header><h2>본인 확인 (KYC)</h2><p>국적별 확인 상태입니다.</p></header>
          <dl className="account-facts"><div><dt>상태</dt><dd>{KYC_STATUS[portfolio.kyc_profile.status]}</dd></div><div><dt>위험 등급</dt><dd>{KYC_RISK[portfolio.kyc_profile.risk_tier]}</dd></div><div><dt>만료</dt><dd>{portfolio.kyc_profile.expires_epoch ? date(portfolio.kyc_profile.expires_epoch) : "—"}</dd></div></dl>
        </section>}
        <section className="account-section">
          <header><h2>보유 기업 지배구조</h2><p>보유 주식의 의결권과 지배하는 자회사 관계를 넓은 화면에서 확인합니다.</p></header>
          <a className="account-save ownership-open" href="/ownership" target="_blank" rel="noreferrer">조직도 새 창에서 보기 ↗</a>
        </section>
        <section className="account-section">
          <header><h2>바로가기</h2></header>
          <nav className="account-links">{SERVICES.map((item) => <a key={item.path} href={item.path}><b>{item.label}</b><small>{item.description}</small></a>)}</nav>
        </section>
        <AccountWithdrawal user={me} />
      </div>
    </main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;

  return <div className="hts-shell stage">
    <Backdrop />
    {topbar("home")}
    <main className="hts-main">
      {watch.length > 0 && <nav className="watch-strip" aria-label="관심종목">{watch.map((code) => instruments.find((item) => item.code === code)).filter((item): item is Instrument => Boolean(item)).map((item) => <button type="button" key={item.code} className={item.code === selectedCode ? "active" : ""} onClick={() => chooseInstrument(item.code)}>
        <b>{item.name}</b><span>{money(item.price, 2)}</span>{item.change24h != null && <small className={item.change24h > 0 ? "up" : item.change24h < 0 ? "down" : undefined}>{item.change24h > 0 ? "+" : ""}{Number(item.change24h).toFixed(2)}%</small>}
      </button>)}</nav>}
      <div className="quote-strip"><div><p className="eyebrow">{selected?.exchange_id || "MARKET"} · {selected ? instrumentType(selected) : "—"}</p><h1>{selected?.name || "종목을 선택하세요"}<small>{selected?.code}</small>{selected && <button type="button" className={watch.includes(selected.code) ? "watch-star on" : "watch-star"} aria-pressed={watch.includes(selected.code)} title={watch.includes(selected.code) ? "관심종목에서 빼기" : "관심종목에 추가"} onClick={() => toggleWatch(selected.code)}>{watch.includes(selected.code) ? "★" : "☆"}</button>}<WarningBadge warning={selected?.warning} /></h1>{selected?.drProgram && <><p className="orders-hint">{selected.drProgram.receiptType} · 원주 {selected.drProgram.underlyingCode} · 1 DR = {money(selected.drProgram.underlyingPerReceipt, 6)}주 · 원주 거래소 {selected.drProgram.homeExchangeId || "—"} · 예탁 {selected.drProgram.depositaryBank || "—"} · 수탁 {selected.drProgram.custodian || "—"}</p>{selected.drProgram.referenceValue !== undefined && <p className="orders-hint">단순 원주 환산 참고가 {money(selected.drProgram.referenceValue, 4)} {selected.quote_currency || ""}{selected.drProgram.referencePremiumPercent !== undefined && ` · 현재가 괴리 ${selected.drProgram.referencePremiumPercent >= 0 ? "+" : ""}${Number(selected.drProgram.referencePremiumPercent).toFixed(2)}%`}{selected.drProgram.referenceDeviationAlert && " · ⚠ 괴리 주의 (±10% 이상)"} · 수수료·세금·유동성 미반영</p>}</>}{selected?.bondRisk && <p className="orders-hint">액면 {money(selected.bondRisk.faceValue)} 기준 · Macaulay {Number(selected.bondRisk.macaulay_duration).toFixed(3)}년 · 수정듀레이션 {Number(selected.bondRisk.modified_duration).toFixed(3)} · 컨벡서티 {Number(selected.bondRisk.convexity).toFixed(3)} · DV01 {money(selected.bondRisk.dv01, 4)} {selected.quote_currency || homeCurrency} / 1bp. 현 국채 곡선의 평행이동 민감도이며 거래가격 예측이나 규제지표가 아닙니다.</p>}{selected?.warning?.reason && <p className="warn-reason">⚠️ {selected.warning.label} · {selected.warning.reason}</p>}</div>
        <div className="quote-side"><div className="quote-value"><strong>{money(current)}</strong><span>{selected?.quote_currency || homeCurrency}</span>{selected && (selected.change24h != null
          ? <em className={`quote-change ${selected.change24h > 0 ? "up" : selected.change24h < 0 ? "down" : ""}`} title="최근 24시간 등락률">{selected.change24h > 0 ? "▲" : selected.change24h < 0 ? "▼" : ""} {selected.change24h > 0 ? "+" : ""}{Number(selected.change24h).toFixed(2)}%</em>
          : <em className="quote-change" title="최근 24시간 거래가 없어 등락률이 없습니다">—</em>)}</div><button className="quote-switch" onClick={() => { setSearchOpen(true); searchInputRef.current?.focus(); window.scrollTo({ top: 0, behavior: "smooth" }); }}>종목 바꾸기</button>{selected && <a className="quote-switch" href={boardUrl(selected.code)}>종목 토론</a>}</div></div>
      <div className="trade-row">
        <div className="chart-col">
          <ErrorBoundary label="차트" resetKey={`${selectedCode}-${range}-${interval}-${chartMode}`}>
          <PriceChart points={points} candles={candles} candleInterval={candleInterval} mode={chartMode} fallbackPrice={Number(selected?.price ?? 1)}
            drawings={drawings} onDrawingsChange={setDrawings} tool={tool} onToolChange={setTool} resetKey={`${selectedCode}-${range}-${interval}-${chartMode}`} label={selected?.name || "가격"}
            indicators={indicators} data={indicatorData} catalog={indicatorCatalog} onIndicatorsChange={chooseIndicators}
            controls={<>{ranges.map((item) => <button key={item} className={range === item ? "selected" : ""} onClick={() => setRange(item)}>{rangeNames[item]}</button>)}
              <span className="divider" aria-hidden="true" />
              <button className={chartMode === "candle" ? "selected" : ""} onClick={() => setChartMode("candle")}>캔들</button>
              <button className={chartMode === "line" ? "selected" : ""} onClick={() => setChartMode("line")}>선</button>
              {chartMode === "candle" && <select className="interval-select" aria-label="봉 주기" value={interval ?? ""} onChange={(event) => setIntervalSeconds(event.target.value ? Number(event.target.value) : null)}><option value="">자동 ({intervals.find(([seconds]) => seconds === candleInterval)?.[1] ?? "봉"})</option>{intervals.map(([seconds, name]) => <option key={seconds} value={seconds}>{name}</option>)}</select>}<button className="chart-refresh" onClick={() => { lastPollError.current.delete("account"); void refreshAccountQuiet(); }}>새로고침</button></>} />
          </ErrorBoundary>
          <ExchangeHeatmap instruments={instruments} exchangeId={heatmapExchange} onExchangeChange={setHeatmapExchangeOverride} onSelect={chooseInstrument} />
        </div>
        <div className="ticket-col">
          <section className={`data-panel order-entry${locked}`} {...lock("로그인하고 주문하기")}><div className="panel-title"><h2>주문 입력</h2><span>{ORDER_TYPES.find(([id]) => id === orderType)![1]}</span></div>
            <div className="order-types" role="group" aria-label="주문 유형">{ORDER_TYPES.map(([id, label]) => <button key={id} type="button" aria-pressed={orderType === id} className={orderType === id ? "active" : ""} onClick={() => setOrderType(id)}>{label}</button>)}</div>
            <div className="order-sides"><button className={side === "buy" ? "buy active" : "buy"} onClick={() => setSide("buy")}>매수</button><button className={side === "sell" ? "sell active" : "sell"} onClick={() => setSide("sell")}>매도</button></div>
            {orderType === "limit" && <label>지정가 ({selected?.quote_currency || homeCurrency})<input inputMode="numeric" value={price} onChange={(event) => setPrice(event.target.value.replace(/[^\d.]/g, ""))} /></label>}
            {orderType === "current" && <label>현재가 ({selected?.quote_currency || homeCurrency})<input value={orderPrice ? money(orderPrice, 2) : "—"} readOnly aria-readonly="true" /></label>}
            {orderType === "market" && <label>예상 체결가 ({selected?.quote_currency || homeCurrency})<input value={orderPrice ? money(orderPrice, 2) : "—"} readOnly aria-readonly="true" /></label>}
            {orderType === "market" && Number(quantity) > 0 && !marketEstimate.enough && <p className="risk-note">상대 호가 수량이 부족해 시장가로 모두 체결할 수 없습니다.</p>}
            <label>주문 수량<input inputMode="numeric" value={quantity} onChange={(event) => setQuantity(event.target.value.replace(/\D/g, ""))} /></label>
            <div className="form-total"><span>주문 금액</span><b>{money(orderAmount, 2)} {selected?.quote_currency || homeCurrency}</b></div>
            <button className={side === "buy" ? "submit buy-submit" : "submit sell-submit"} disabled={!orderReady || busy} onClick={() => setReview(true)}>{side === "buy" ? "매수 주문 검토" : "매도 주문 검토"}</button>
            <p className="risk-note">{orderType === "market" ? "시장가는 상대 호가를 순서대로 체결하며, 실제 체결가는 제출 시점 호가로 정해집니다. " : orderType === "current" ? "현재가는 마지막 체결가로 지정가 주문을 냅니다. " : ""}호가를 누르면 그 가격이 지정가로 들어갑니다. 펀드 가입·환매와 소수점 수량은 명령어의 매수·매도를 사용하세요.</p>
          </section>
          {selected?.type === "depositary_receipt" && <section className="data-panel order-entry dr-conversion"><div className="panel-title"><h2>DR 발행 · 상환</h2><span>{selected.drProgram?.programStatus === "active" && selected.status === "active" ? "기초주식 예탁 방식" : "프로그램 중지"}</span></div>
            <p className="orders-hint">{selected.drProgram?.programStatus === "active" && selected.status === "active" ? "DR 수량을 입력하세요. 발행은 보유 원주를 잠그고 DR을 지급하며, 상환은 보유 DR을 소각하고 보관 원주를 지급합니다." : "중지되거나 종료된 DR 프로그램은 새 전환을 할 수 없습니다."}</p>
            <label>전환할 DR 수량<input inputMode="numeric" value={drQuantity} onChange={(event) => setDrQuantity(event.target.value.replace(/\D/g, ""))} /></label>
            <div className="actions"><button className="submit buy-submit" disabled={busy || !Number(drQuantity) || selected.drProgram?.programStatus !== "active" || selected.status !== "active"} onClick={() => void convertDr("issue")}>원주 예탁 · DR 발행</button><button className="submit sell-submit" disabled={busy || !Number(drQuantity) || selected.drProgram?.programStatus !== "active" || selected.status !== "active"} onClick={() => void convertDr("redeem")}>DR 반납 · 원주 상환</button></div>
            <hr />
            <h3>DR 의결권 위임</h3>
            <p className="orders-hint">시뮬레이터 내부 위임 기록입니다. 위임된 수량은 매도·상환할 수 없으며, 기준일 의결권은 주주총회 투표에서 대리인에게 합산됩니다. 실제 수탁기관 지시는 포함하지 않습니다.</p>
            <label>대리 사용자 ID<input value={drProxyUserId} onChange={(event) => setDrProxyUserId(event.target.value)} placeholder="예: user-123" /></label>
            <label>위임 수량 (DR)<input inputMode="numeric" value={drVoteQuantity} onChange={(event) => setDrVoteQuantity(event.target.value.replace(/\D/g, ""))} /></label>
            <button className="submit buy-submit" disabled={busy || !drProxyUserId.trim() || !Number(drVoteQuantity)} onClick={() => void delegateDrVotes()}>의결권 위임</button>
            {drDelegations.filter((item) => item.dr_code === selected.code).map((item) => <div className="holding" key={item.delegation_id}>
              <span><b>{item.proxy_user_id} · {item.dr_quantity} DR · {item.status === "active" ? "위임 중" : "철회됨"}</b><small>위임 {date(item.created_epoch)}{item.revoked_epoch ? ` · 철회 ${date(item.revoked_epoch)}` : ""}</small></span>
              {item.status === "active" && <button className="cancel-button" disabled={busy} onClick={() => void revokeDrVotes(item)}>철회</button>}
            </div>)}
          </section>}
        </div>
      </div>
      <div className="board-row">
        <section className="data-panel orderbook-panel"><div className="panel-title"><h2>호가</h2><span>수량 · 가격</span></div><div className="book-head"><span>수량</span><span>가격</span></div>
          <div className="book-side asks">{[...book.asks].reverse().map((item, index) => <button type="button" className="book-row" key={`${item.limit_price}-${index}`} title="이 가격으로 주문" onClick={() => { setOrderType("limit"); setPrice(String(item.limit_price)); }}><span>{money(item.quantity, 3)}</span><b>{money(item.limit_price, 2)}</b></button>)}</div>
          <div className="book-current"><span>현재가</span><strong>{money(current)}</strong></div>
          <div className="book-side bids">{book.bids.map((item, index) => <button type="button" className="book-row" key={`${item.limit_price}-${index}`} title="이 가격으로 주문" onClick={() => { setOrderType("limit"); setPrice(String(item.limit_price)); }}><span>{money(item.quantity, 3)}</span><b>{money(item.limit_price, 2)}</b></button>)}</div>
        </section>
        <section className={`data-panel orders-panel${locked}`} {...lock("로그인하고 주문 보기")}>
          <div className="panel-title"><h2>주문 · 체결</h2><div className="order-tabs" role="tablist" aria-label="주문 필터" onKeyDown={tabListKeys}><button type="button" role="tab" id="order-tab-open" aria-controls="orders-panel" tabIndex={orderFilter === "open" ? 0 : -1} aria-selected={orderFilter === "open"} className={orderFilter === "open" ? "selected" : ""} onClick={() => setOrderFilter("open")}>미체결 {openCount}</button><button type="button" role="tab" id="order-tab-all" aria-controls="orders-panel" tabIndex={orderFilter === "all" ? 0 : -1} aria-selected={orderFilter === "all"} className={orderFilter === "all" ? "selected" : ""} onClick={() => setOrderFilter("all")}>전체</button></div></div>
          <p className="orders-hint">미체결·부분 체결·예약 주문은 오른쪽 <b>취소</b> 버튼으로 바로 취소할 수 있어요. 남은 예치금과 수량은 즉시 돌려받습니다.</p>
          <div className="table-scroll" id="orders-panel" role="tabpanel" aria-labelledby={`order-tab-${orderFilter}`}><table><thead><tr><th>주문</th><th>종목</th><th>구분</th><th className="r">가격</th><th className="r">수량</th><th className="r">체결</th><th className="r">잔량</th><th>상태</th><th /></tr></thead><tbody>{visibleOrders.map((order) => <tr key={order.order_id}>
            <td className="mono">#{order.order_id}</td><td><button className="link" onClick={() => chooseInstrument(order.asset_code)}>{order.asset_code}</button></td>
            <td className={order.side === "buy" ? "buy-text" : "sell-text"}>{order.side === "buy" ? "매수" : "매도"}</td><td className="r">{money(order.limit_price, 2)}</td><td className="r">{money(order.quantity, 3)}</td><td className="r">{money(order.filled_quantity, 3)}</td><td className="r">{money(order.remaining_quantity, 3)}</td>
            <td><span className={OPEN.has(order.status) ? "status open" : "status"}>{STATUS[order.status] ?? order.status}</span></td>
            <td>{OPEN.has(order.status) && <button className="cancel-button" disabled={busy} onClick={() => void cancelOrder(order)}>취소</button>}</td>
          </tr>)}</tbody></table>{!visibleOrders.length && <p className="empty">{orderFilter === "open" ? "미체결 주문이 없습니다." : "최근 주문이 없습니다."}</p>}</div>
        </section>
        <section className={`wallet-tile${locked}`} aria-label="내 지갑" {...lock("로그인하고 내 지갑 보기")}>
          <section className="wallet-summary">
            <span>내 순자산 · {portfolio?.nationality ?? me.nationality} · {homeCurrency}<RefreshRateButton busy={ratesBusy} onRefresh={() => void refreshRates()} /></span>
            <strong>{compactMoney(totalInHome(portfolio?.net_worth ?? 0, portfolio, inHome))} <small>{homeCurrency}</small></strong>
            <p>총자산 {compactMoney(totalInHome(portfolio?.grand_total ?? 0, portfolio, inHome))} · 현금 {compactMoney(portfolio?.cash ?? me.cash)} {homeCurrency}</p>
            {portfolio?.kyc_profile && <p>KYC · {KYC_STATUS[portfolio.kyc_profile.status]} · {KYC_RISK[portfolio.kyc_profile.risk_tier]}{portfolio.kyc_profile.expires_epoch ? ` · 만료 ${date(portfolio.kyc_profile.expires_epoch)}` : ""}</p>}
      {/* 세무거주지는 지금 국적과 동일하게 자동 적용합니다. 직접 바꾸는 화면은 숨겼습니다(서버 기능은 그대로). */}
          </section>
          <div className="wallet-actions">
            <a href={`/commands/${encodeURIComponent("송금")}`}>송금</a>
            <a href={`/commands/${encodeURIComponent("계좌개설")}`}>계좌개설</a>
            <a href={`/commands/${encodeURIComponent("대출신청")}`}>대출</a>
            <a href="/portfolio" target="_blank" rel="noreferrer">내 주식</a>
          </div>
          <section className="data-panel wallet-panel">
            <div className="wallet-row"><span>현금</span><b>{compactMoney(portfolio?.cash || 0)} {homeCurrency}</b></div>
            {(portfolio?.wallets ?? []).filter((wallet) => wallet.is_foreign && wallet.balance_units).map((wallet) => <div className="wallet-row" key={wallet.currency_code}><span>외화 · {wallet.currency_code}</span><b>{compactMoney(minorMoney(wallet.balance_units, 6))} {wallet.currency_code}</b></div>)}
            <div className="wallet-row"><span>통장</span><b>{compactMoney(portfolio?.account_total || 0)} {homeCurrency}</b></div>
            <div className="wallet-row"><span>투자 평가액</span><b>{compactMoney(inHome(portfolio?.stock_total || 0))} {homeCurrency}</b></div>
            <div className="wallet-row"><span>매수 예치금</span><b>{compactMoney(inHome(portfolio?.order_reserved_total || 0))} {homeCurrency}</b></div>
            <div className="wallet-row"><span>대출</span><b>{compactMoney(inHome(portfolio?.loan_total || 0))} {homeCurrency}</b></div>
            <div className="wallet-row net-worth"><span>순자산</span><b>{compactMoney(totalInHome(portfolio?.net_worth || 0, portfolio, inHome))} {homeCurrency}</b></div>
            {(portfolio?.accounts ?? []).filter((account) => account.resolution_status === "active").map((account) => <div className="wallet-row account-detail" key={account.number}><span>{account.product_name} · {account.currency_code}<small>{account.number}</small>{account.rate_type === "floating" && account.next_repricing_epoch != null && <small>현재 연 {(account.contract_rate == null ? undefined : Number(account.contract_rate).toFixed(4)) ?? "—"}% · 지표 관측 기준일 {date(Math.max(0, account.next_repricing_epoch - account.repricing_lag_seconds))} · 재산정 예정 {date(account.next_repricing_epoch)} · {RATE_BENCHMARK_LABELS[account.benchmark_key || ""] || account.benchmark_key || "기준금리"} {account.benchmark_tenor_days}일 · 새 금리는 재산정 시 확정</small>}{account.rate_resets.map((reset, index) => <small key={`${reset.applied_epoch}-${index}`}>이전 재산정 {date(reset.applied_epoch)} · {RATE_BENCHMARK_LABELS[reset.benchmark_key] || reset.benchmark_key} {reset.benchmark_tenor_days}일 · 연 {reset.old_rate}% → {reset.new_rate}%</small>)}</span><b>{money(account.balance, account.decimal_places)} {account.currency_code}</b></div>)}
          </section>
          {credit && <section className="data-panel wallet-credit" aria-label="신용점수">
            <div className="panel-title"><h2>신용점수<a className="credit-link" href="/credit" aria-label="신용점수 자세히 보기">바로가기 ↗</a></h2><span>기준 {credit.anchor}점</span></div>
            <div className="credit-score"><b>{credit.score}</b><span>/ 1000</span></div>
            <div className="credit-meter" role="meter" aria-label="신용점수" aria-valuemin={0} aria-valuemax={1000} aria-valuenow={credit.score}><i style={{ width: `${Math.min(100, Math.max(0, credit.score / 10))}%` }} /><em style={{ left: `${Math.min(100, Math.max(0, credit.anchor / 10))}%` }} title={`기준점수 ${credit.anchor}점`} /></div>
            <p className="credit-note">기준점수에 요인을 더한 점수입니다. 연체 영향은 시간이 지나며 줄고, 정상 납부·완납은 점수를 올립니다. 실제 신용평가사 점수가 아닙니다.</p>
          </section>}

        </section>
      </div>
      <div className="extras">
        {(portfolio?.deposit_insurance_claims?.length ?? 0) > 0 && <section className="data-panel holdings-panel">
          <div className="panel-title"><h2>예금보험 정리 이력</h2><span>{portfolio?.deposit_insurance_claims?.length}건</span></div>
          <div className="holdings">{portfolio?.deposit_insurance_claims?.map((claim) => <div className="holding" key={claim.claim_id}>
            <span><b>{claim.issuer} · {date(claim.resolved_epoch)}</b><small>원예금 통화 기준 보험금 {money(claim.covered_amount, claim.decimal_places)} · 미보장채권 {money(claim.uninsured_amount, claim.decimal_places)} {claim.currency_code}</small>{claim.payout_currency && claim.payout_amount != null && <small>실제 지급 기록 {money(claim.payout_amount, claim.payout_decimal_places ?? 0)} {claim.payout_currency}</small>}</span>
          </div>)}</div>
          <p className="orders-hint">보험금 지급은 고객 국적 통화로 환전될 수 있습니다. 이 내역은 예금 원통화 기준의 시뮬레이터 정리 결과이며 실제 예금보험 청구가 아닙니다.</p>
        </section>}
        {loanSchedules.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>대출 상환 일정</h2><span>{loanSchedules.length}건</span></div>
          <div className="holdings">{loanSchedules.map((loan) => <div className="holding loan-schedule" key={loan.loan_id}>
            <span><b>#{loan.loan_id} · {loan.product_code} · 잔액 {money(loan.outstanding_balance, loan.decimal_places)} {loan.quote_currency}</b>
              {loan.underwriting_stress_dsr != null && <small>심사 당시 DSR {(loan.underwriting_dsr == null ? undefined : Number(loan.underwriting_dsr).toFixed(1)) ?? "—"}% · 금리 +{loan.underwriting_stress_shock_bps}bp / 외화부채 환율 +{loan.underwriting_fx_stress_shock_percent}% 스트레스 DSR {Number(loan.underwriting_stress_dsr).toFixed(1)}%</small>}
              {loan.origination_pd_12m != null && <small>취급 시 추정 12개월 부도확률(PD) {(loan.origination_pd_12m * 100).toFixed(2)}% · 현재 ECL 평가의 Stage 2 비교 기준</small>}
              {loan.current_ecl_stage != null && loan.current_ecl_pd_12m != null && <small>최근 ECL 평가 · Stage {loan.current_ecl_stage} · 12개월 PD {(loan.current_ecl_pd_12m * 100).toFixed(2)}%</small>}
              {loan.stage_cure_since_epoch != null && <small>위험 개선 관찰 시작 {date(loan.stage_cure_since_epoch)} · 90일 경과 후 Stage 2 조건이 없으면 Stage 1 재평가</small>}
              {loan.rate_type === "floating" && loan.next_repricing_epoch != null && <small>현재 연 {(loan.contract_rate == null ? undefined : Number(loan.contract_rate).toFixed(4)) ?? "—"}% · 지표 관측 기준일 {date(Math.max(0, loan.next_repricing_epoch - loan.repricing_lag_seconds))} · 다음 재산정 {date(loan.next_repricing_epoch)} · {RATE_BENCHMARK_LABELS[loan.benchmark_key || ""] || loan.benchmark_key || "기준금리"} {loan.benchmark_tenor_days}일 · 새 금리는 재산정 시 확정됩니다.</small>}
              {loan.installments.slice(0, 4).map((item) => <small key={item.schedule_id}>회차 {item.installment_number} · {date(item.due_epoch)} · 잔액 원금 {money(moneyAdd(item.principal_due, item.principal_paid, true), loan.decimal_places)} / 이자 {money(moneyAdd(item.interest_due, item.interest_paid, true), loan.decimal_places)} / 수수료 {money(moneyAdd(item.fee_due, item.fee_paid, true), loan.decimal_places)} {loan.quote_currency}</small>)}
              {!loan.installments.length && <small>예정된 미납 회차가 없습니다.</small>}
              {loan.rate_resets.map((reset, index) => <small key={`${reset.applied_epoch}-${index}`}>금리 재산정 {date(reset.applied_epoch)} · {RATE_BENCHMARK_LABELS[reset.benchmark_key] || reset.benchmark_key} {reset.benchmark_tenor_days}일 · 연 {reset.old_rate}% → {reset.new_rate}%</small>)}
            </span>
          </div>)}</div>
          <p className="orders-hint">회차별 계약상 배분 내역입니다. 회차 잔액을 넘는 초과상환 및 경과·연체이자 별도 원장은 표시하지 않습니다.</p>
        </section>}
        {drMovements.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>DR 전환 이력</h2><span>최근 {drMovements.length}건</span></div>
          <div className="holdings">{drMovements.map((item) => <div className="holding" key={item.movement_id}>
            <span><b>{item.movement_type === "issue" ? "DR 발행" : "원주 상환"} · {item.dr_code}</b><small>{date(item.recorded_epoch)} · {money(item.dr_quantity, 0)} DR ↔ {money(item.underlying_quantity, 0)}주 {item.underlying_code} · 원주 평균원가 {groupAmount(item.underlying_average_cost_text)} {item.underlying_currency}</small></span>
          </div>)}</div>
          <p className="orders-hint">게임 내 예탁 이동 기록이며 실제 수탁기관 보관 증빙은 아닙니다.</p>
        </section>}
        {drCustodyInstructions.some((item) => item.status === "requested" || item.status === "verified") && <section className="data-panel holdings-panel"><div className="panel-title"><h2>대기 중인 DR 전환</h2><span>{drCustodyInstructions.filter((item) => item.status === "requested" || item.status === "verified").length}건</span></div>
          <div className="holdings">{drCustodyInstructions.filter((item) => item.status === "requested" || item.status === "verified").map((item) => <div className="holding" key={item.reference_key}>
            <span><b>{item.movement_type === "issue" ? "DR 발행" : "원주 상환"} · {item.dr_code}</b><small>{money(item.dr_quantity, 0)} DR ↔ {money(item.underlying_quantity, 0)}주 {item.underlying_code} · 접수 {date(item.requested_epoch)} · 정산 가능 {item.settle_after_epoch ? date(item.settle_after_epoch) : "즉시"} · 수량 예약 중</small></span>
            {item.status === "requested" && <button className="cancel-button" disabled={busy} onClick={() => void cancelDrConversion(item)}>요청 취소</button>}
          </div>)}</div>
          <p className="orders-hint">대기 중인 입력 수량은 매도·담보·다른 DR 전환에 사용할 수 없습니다. 정산은 내부 시뮬레이터 관리 흐름에서 처리됩니다.</p>
        </section>}
        {receivedDrDelegations.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>위임받은 DR 의결권</h2><span>{receivedDrDelegations.length}건</span></div>
          <div className="holdings">{receivedDrDelegations.map((item) => <div className="holding" key={item.delegation_id}>
            <span><b>{item.dr_code} · {item.dr_quantity} DR</b><small>{item.owner_user_id}에게서 위임 · {item.underlying_code} 의결권 · {date(item.created_epoch)}</small></span>
          </div>)}</div>
          <p className="orders-hint">위임받은 권리는 기준일 스냅샷에서 대리인의 표수에 합산됩니다.</p>
        </section>}
        {drVotingAgenda.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>주주총회 의결권</h2><span>{drVotingAgenda.length}개 안건</span></div>
          <div className="holdings">{drVotingAgenda.map((item) => <div className="holding" key={`${item.meetingId}-${item.agendaItemId}`}>
            <span><b>{item.underlyingCode} · {item.meetingTitle} — {item.agendaTitle}</b><small>기준일 의결권 {money(item.votingPower)}표 · {item.state === "open" ? `투표 마감 ${item.closesEpoch ? date(item.closesEpoch) : "—"}` : item.state === "upcoming" ? `투표 시작 ${item.opensEpoch ? date(item.opensEpoch) : "—"}` : item.state === "closed" ? "투표 종료" : "투표 일정 미설정"}{item.myChoice ? ` · 내 선택 ${item.myChoice}` : ""}</small></span>
            {item.state === "open" && !item.myChoice && <div className="actions">{item.choices.map((choice) => <button key={choice} className="cancel-button" disabled={busy} onClick={() => void castDrVote(item, choice)}>{choice}</button>)}</div>}
          </div>)}</div>
          <p className="orders-hint">기준일 현재 직접·DR 및 위임 권리를 합산합니다. 투표는 기간 중 안건마다 한 번 가능하고 변경·철회할 수 없습니다.</p>
        </section>}
      </div>
    </main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
    {review && selected && <OrderReviewModal side={side} kind={orderType} kindLabel={ORDER_TYPES.find(([id]) => id === orderType)![1]} priceLabel={orderType === "market" ? "예상 체결가" : orderType === "current" ? "현재가" : "지정가"}
      price={orderPrice || "0"} priceAt={orderType === "limit" ? null : orderType === "current" ? currentAt : quote?.at ?? null} currency={selected.quote_currency || homeCurrency}
      nameLine={<>{selected.name} ({selectedCode}) <WarningBadge warning={selected.warning} /></>} quantity={quantity} amount={orderAmount} busy={busy} format={money}
      onClose={() => setReview(false)} onConfirm={() => void submitOrder()} />}
  </div>;
}

function groupAmount(value: string) {
  const [whole, fraction] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}

/** Sends a visitor of a login-only page straight to the login (the identity provider, or the account panel without SSO). */
function LoginRedirect({ go }: { go: () => void }) {
  useEffect(() => { go(); }, [go]);
  return <p className="loading">로그인 화면으로 이동하는 중…</p>;
}
