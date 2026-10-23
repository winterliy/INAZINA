import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, clearToken, type ApiUser } from "../../shared/api";
import { AuthPanel } from "../../shared/AuthPanel";
import { PriceChart, type Candle, type Drawing, type Point, type Tool } from "./PriceChart";
import { CommandPanel } from "./CommandPanel";
import { Backdrop } from "./Backdrop";

type Warning = { level: "caution" | "warning" | "danger"; label: string; reason: string; expiresEpoch: number | null };
type DrMovement = { movement_id: number; dr_code: string; underlying_code: string; underlying_currency: string; movement_type: "issue" | "redeem"; dr_quantity: number; underlying_quantity: number; underlying_average_cost: number; underlying_average_cost_text: string; decimal_places: number; recorded_epoch: number };
type DrCustodyInstruction = { reference_key: string; dr_code: string; underlying_code: string; movement_type: "issue" | "redeem"; dr_quantity: number; underlying_quantity: number; status: "requested" | "verified" | "settled" | "rejected" | "cancelled"; requested_epoch: number; settle_after_epoch: number | null };
type DrVotingDelegation = { delegation_id: number; dr_code: string; underlying_code: string; proxy_user_id: string; dr_quantity: number; status: "active" | "revoked"; created_epoch: number; revoked_epoch: number | null; events: { event_type: string; recorded_epoch: number }[] };
type ReceivedDrVotingDelegation = { delegation_id: number; dr_code: string; underlying_code: string; owner_user_id: string; dr_quantity: number; created_epoch: number };
type DrVotingAgenda = { meetingId: number; agendaItemId: number; underlyingCode: string; meetingTitle: string; agendaTitle: string; choices: string[]; recordEpoch: number; opensEpoch: number | null; closesEpoch: number | null; quorumPercent: number; votingPower: number; totalEligible: number; myChoice: string | null; state: "unscheduled" | "upcoming" | "open" | "closed" };
type Instrument = { code: string; name: string; type: string; price: number; status: string; exchange_id?: string; quote_currency?: string; fund_category?: string | null; bondRisk?: { faceValue: number; macaulay_duration: number; modified_duration: number; convexity: number; dv01: number } | null; drProgram?: { underlyingCode: string; underlyingPerReceipt: number; receiptType: string; programStatus: string; depositaryBank?: string | null; custodian?: string | null; homeExchangeId?: string | null; referenceValue?: number; referencePremiumPercent?: number; referenceDeviationAlert?: boolean }; warning?: Warning; change24h?: number | null; tradeValue24h?: number; trades24h?: number; volume24h?: number };

// Administrator trading warnings (투자주의·경고·위험).
const WarningBadge = ({ warning }: { warning?: Warning }) => warning ? <span className={`warn-badge ${warning.level}`} title={warning.reason || warning.label}>{warning.label}</span> : null;
type Wallet = { currency_code: string; balance_units: number; decimal_places?: number; is_foreign: boolean };
type AccountRateReset = { old_rate: number; new_rate: number; scheduled_epoch: number; applied_epoch: number; benchmark_key: string; benchmark_tenor_days: number };
type AccountDetail = { number: string; account_type: string; product_name: string; balance: number; currency_code: string; decimal_places: number; resolution_status: string; rate_type: string; contract_rate: number | null; next_repricing_epoch: number | null; benchmark_key: string | null; benchmark_tenor_days: number; repricing_lag_seconds: number; rate_resets: AccountRateReset[]; insurance_paid: number; uninsured_claim: number };
type DepositInsuranceClaim = { claim_id: number; issuer: string; currency_code: string; decimal_places: number; covered_amount: number; uninsured_amount: number; payout_currency?: string | null; payout_amount?: number | null; payout_decimal_places?: number | null; resolved_epoch: number };
type KycProfile = { status: "pending" | "verified" | "rejected" | "expired"; risk_tier: "standard" | "enhanced" | "restricted"; expires_epoch: number | null };
type LoanSchedule = { schedule_id: number; installment_number: number; due_epoch: number; principal_due: number; interest_due: number; fee_due: number; principal_paid: number; interest_paid: number; fee_paid: number; amount_paid: number; status: string };
type LoanRateReset = { old_rate: number; new_rate: number; scheduled_epoch: number; applied_epoch: number; benchmark_key: string; benchmark_tenor_days: number };
const RATE_BENCHMARK_LABELS: Record<string, string> = { policy: "정책금리", funding: "조달금리", bank_bond: "은행채 수익률", treasury: "국채 수익률" };
type LoanScheduleView = { loan_id: number; product_code: string; quote_currency: string; decimal_places: number; outstanding_balance: number; status: string; rate_type: string; contract_rate: number | null; next_repricing_epoch: number | null; benchmark_key: string | null; benchmark_tenor_days: number; repricing_lag_seconds: number; collection_status: string; maturity_epoch: number | null; stage_cure_since_epoch: number | null; underwriting_dsr: number | null; underwriting_stress_dsr: number | null; underwriting_stress_shock_bps: number; underwriting_fx_stress_shock_percent: number; origination_pd_12m: number | null; current_ecl_stage: number | null; current_ecl_pd_12m: number | null; installments: LoanSchedule[]; rate_resets: LoanRateReset[] };
type DividendPayment = { payment_id: number; asset_code: string; dividend_kind: "regular" | "special"; quantity: number; gross_amount: number; tax_amount: number; net_amount_units: number; quote_currency: string; decimal_places: number; created_epoch: number; allocation_key?: string; dr_code?: string | null; rule_id?: number | null; treaty_code?: string | null; tax_rate_percent?: string | null };
type TaxResidency = { nationality: string | null; taxResidency: string | null; countries: string[] };
type Holding = { code: string; name: string; qty: number; price: number; value: number; type: string | null; quote_currency?: string; sell_reserved_qty?: number; is_collateral?: boolean };
type CreditFactor = { key: string; label: string; points: number };
type CreditEvent = { type: string; reference: string; loanId: number | null; occurredEpoch: number; reason: string | null };
type CreditReport = { score: number; anchor: number; factors: CreditFactor[]; events: CreditEvent[]; history: { previous: number; score: number; trigger: string; epoch: number }[] };
type Portfolio = { cash: number; grand_total: number; net_worth: number; loan_total: number; order_reserved_total: number; stock_total: number; account_total: number; bond_total?: number; national_currency?: string; nationality?: string; home_rate_nwx?: number; home_decimal_places?: number; wallets?: Wallet[]; stocks: Holding[]; accounts?: AccountDetail[]; deposit_insurance_claims?: DepositInsuranceClaim[]; kyc_profile?: KycProfile | null };
type Order = { order_id: number; asset_code: string; side: string; limit_price: number; quantity: number; remaining_quantity: number; filled_quantity: number; status: string; quote_currency?: string };
type Book = { bids: { limit_price: number; quantity: number; order_count: number }[]; asks: { limit_price: number; quantity: number; order_count: number }[] };

const ranges = ["1h", "1d", "1w", "1m", "all"] as const;
const rangeNames: Record<string, string> = { "1h": "1시간", "1d": "1일", "1w": "1주", "1m": "1개월", all: "전체" };
const intervals: [number, string][] = [[60, "1분봉"], [300, "5분봉"], [600, "10분봉"], [1800, "30분봉"], [3600, "1시간봉"], [21600, "6시간봉"], [43200, "12시간봉"], [86400, "일봉"]];
const OPEN = new Set(["open", "partial", "scheduled"]);
const STATUS: Record<string, string> = { open: "미체결", partial: "부분 체결", scheduled: "예약", filled: "체결", cancelled: "취소", expired: "만료" };
const UNIT_SCALE = 1_000_000;
const money = (value: number, digits = 0) => new Intl.NumberFormat("ko-KR", { maximumFractionDigits: digits }).format(value || 0);
const date = (epoch: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" }).format(epoch * 1000);
const CREDIT_EVENTS: Record<string, string> = {
  payment_on_time: "정상 납부", payment_missed: "납부 연체", payment_cured: "연체 회차 사후 납부", loan_closed: "대출 완납",
  acceleration: "기한이익상실", default: "부도", manual_adjustment: "관리자 정정", external_adjustment: "점수 정정 반영", legacy_baseline: "신용모델 도입",
};
const CREDIT_TIPS: Record<string, string> = {
  unpaid_installments: "미납 회차를 갚으면 현재 미납 감점이 바로 사라집니다.",
  missed: "연체 기록은 시간이 지나며 줄어들고, 미납 회차를 갚으면 일부 회복됩니다.",
  delinquent_loans: "연체 중인 대출 잔액을 상환하면 연체 상태 감점이 사라집니다.",
  acceleration: "기한이익상실 기록은 시간이 지나며 줄어듭니다. 정상 납부 실적이 회복을 돕습니다.",
  default: "부도 기록은 시간이 지나며 줄어듭니다. 남은 잔액 상환과 정상 납부가 회복을 돕습니다.",
  debt_burden: "대출 잔액을 줄이거나 연소득 정보를 갱신하면 부채수준 요인이 개선됩니다.",
  new_credit: "짧은 기간에 대출을 여러 건 받지 않으면 2주 뒤 회복됩니다.",
  legacy: "도입 전 감점은 시간이 지나며 줄어듭니다.",
};
const signedPoints = (value: number) => `${value > 0 ? "+" : ""}${Math.round(value * 10) / 10}`;
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
        ? (a, b) => (b.volume24h ?? 0) - (a.volume24h ?? 0) || (b.tradeValue24h ?? 0) - (a.tradeValue24h ?? 0)
        : (a, b) => (b.tradeValue24h ?? 0) - (a.tradeValue24h ?? 0) || Math.abs(change(b)) - Math.abs(change(a)));
  return ranked.slice(0, 20);
};
/** The board is its own page (/board); "종목 토론" opens it filtered to one instrument. */
const boardUrl = (asset = "") => asset ? `/board?asset=${encodeURIComponent(asset)}` : "/board";
const errorText = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;

export default function App() {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [health, setHealth] = useState("확인 중");
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchTabValue, setSearchTabValue] = useState<SearchTab>("recommended");
  const [commandsOpen, setCommandsOpen] = useState<string | null>(null);
  const [recommendSort, setRecommendSort] = useState<RecommendSort>("value");
  const [accountSettingsOpen, setAccountSettingsOpen] = useState(false);
  const [htsId, setHtsId] = useState("");
  const [htsIdInput, setHtsIdInput] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [settingsMessage, setSettingsMessage] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [taxResidency, setTaxResidency] = useState<TaxResidency | null>(null);
  const [taxResidencyOpen, setTaxResidencyOpen] = useState(false);
  const [taxResidencyValue, setTaxResidencyValue] = useState("");
  const [range, setRange] = useState<string>("1d");
  const [chartMode, setChartMode] = useState<"candle" | "line">("candle");
  const [interval, setIntervalSeconds] = useState<number | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [candleInterval, setCandleInterval] = useState(600);
  const [points, setPoints] = useState<Point[]>([]);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [drMovements, setDrMovements] = useState<DrMovement[]>([]);
  const [drCustodyInstructions, setDrCustodyInstructions] = useState<DrCustodyInstruction[]>([]);
  const [drDelegations, setDrDelegations] = useState<DrVotingDelegation[]>([]);
  const [receivedDrDelegations, setReceivedDrDelegations] = useState<ReceivedDrVotingDelegation[]>([]);
  const [drVotingAgenda, setDrVotingAgenda] = useState<DrVotingAgenda[]>([]);
  const [loanSchedules, setLoanSchedules] = useState<LoanScheduleView[]>([]);
  const [credit, setCredit] = useState<CreditReport | null>(null);
  const [dividendPayments, setDividendPayments] = useState<DividendPayment[]>([]);
  const [orderFilter, setOrderFilter] = useState<"open" | "all">("open");
  const [book, setBook] = useState<Book>({ bids: [], asks: [] });
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("1");
  const [drQuantity, setDrQuantity] = useState("1");
  const [drVoteQuantity, setDrVoteQuantity] = useState("1");
  const [drProxyUserId, setDrProxyUserId] = useState("");
  const [price, setPrice] = useState("");
  const [review, setReview] = useState(false);
  const [tool, setTool] = useState<Tool>("select");
  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const drRequestKeys = useRef(new Map<string, string>());
  // Drawings are saved per user and instrument once the server copy is loaded.
  const drawingsLoadedFor = useRef("");
  const savedDrawings = useRef("[]");

  const selected = instruments.find((item) => item.code === selectedCode) ?? null;
  const filtered = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    if (!term) return searchTabValue === "recommended" ? recommended(instruments, recommendSort) : instruments.filter((item) => searchTab(item) === searchTabValue);
    return instruments.filter((item) => `${item.code} ${item.name} ${item.type} ${instrumentType(item)}`.toLocaleLowerCase().includes(term));
  }, [instruments, search, searchTabValue, recommendSort]);
  const tabCounts = useMemo(() => {
    const counts: Partial<Record<SearchTab, number>> = { recommended: Math.min(20, instruments.length) };
    for (const item of instruments) counts[searchTab(item)] = (counts[searchTab(item)] ?? 0) + 1;
    return counts;
  }, [instruments]);

  const authenticate = useCallback(async () => {
    try {
      setUser((await api<{ user: ApiUser }>("/api/auth/session")).user);
    } catch {
      setUser(null);
    }
  }, []);

  const userRole = user?.role;
  const refreshAccount = useCallback(async () => {
    const [wallet, orderData, loanData, dividendData, custodyData] = await Promise.all([
      api<{ portfolio: Portfolio }>("/api/me/portfolio"),
      api<{ orders: Order[] }>("/api/market/orders"),
      api<{ loans: LoanScheduleView[] }>("/api/me/loan-schedules"),
      api<{ payments: DividendPayment[] }>("/api/me/dividend-payments"),
      api<{ instructions: DrCustodyInstruction[] }>("/api/me/dr/conversions"),
    ]);
    // Tax residency exists only for ordinary user accounts (the server refuses admin, nation and
    // company accounts); like the credit panel, it must never block the wallet or the instrument list.
    if (userRole === "user") {
      api<TaxResidency>("/api/me/tax-residency")
        .then((data) => { setTaxResidency(data); setTaxResidencyValue(data.taxResidency ?? ""); })
        .catch(() => setTaxResidency(null));
    } else setTaxResidency(null);
    // The credit panel is optional; a failed report must not hide the wallet.
    api<Partial<CreditReport>>("/api/me/credit")
      .then((report) => setCredit(typeof report.score === "number" ? report as CreditReport : null))
      .catch(() => setCredit(null));
    setPortfolio(wallet.portfolio);
    setOrders(orderData.orders);
    setLoanSchedules(loanData.loans);
    setDividendPayments(dividendData.payments);
    setDrCustodyInstructions(custodyData.instructions);
  }, [userRole]);

  async function saveTaxResidency(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSettingsBusy(true);
    setSettingsError("");
    setSettingsMessage("");
    try {
      const saved = await api<{ taxResidency: string | null }>("/api/me/tax-residency", {
        method: "POST", body: JSON.stringify({ residency: taxResidencyValue || null }),
      });
      setTaxResidency((current) => current ? { ...current, taxResidency: saved.taxResidency } : current);
      setSettingsMessage("세무거주지를 저장했습니다. 이후 확정되는 주주명부부터 적용됩니다.");
    } catch (cause) {
      setSettingsError(errorText(cause, "세무거주지를 저장하지 못했습니다."));
    } finally {
      setSettingsBusy(false);
    }
  }

  useEffect(() => {
    void fetch("/api/health").then((response) => response.json()).then((result) => setHealth(result.python === "connected" ? "연결됨" : "Python 연결 끊김")).catch(() => setHealth("연결 끊김"));
    void authenticate();
  }, [authenticate]);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    // The market list and the account load independently: an account error must not empty 종목검색.
    api<{ instruments: Instrument[] }>("/api/market/instruments")
      .then((market) => {
        if (cancelled) return;
        setInstruments(market.instruments);
        setSelectedCode((current) => current || market.instruments[0]?.code || "");
      })
      .catch((cause) => { if (!cancelled) setNotice(errorText(cause, "종목 목록을 불러오지 못했습니다.")); });
    refreshAccount().catch((cause) => { if (!cancelled) setNotice(errorText(cause, "계좌 정보를 불러오지 못했습니다.")); });
    const timer = window.setInterval(() => void refreshAccount().catch(() => undefined), 15000);
    void api<{ movements: DrMovement[] }>("/api/me/dr/movements")
      .then(({ movements }) => setDrMovements(movements)).catch(() => undefined);
    void api<{ delegations: DrVotingDelegation[] }>("/api/me/dr/voting-delegations")
      .then(({ delegations }) => setDrDelegations(delegations)).catch(() => undefined);
    void api<{ delegations: ReceivedDrVotingDelegation[] }>("/api/me/dr/proxy-voting-delegations")
      .then(({ delegations }) => setReceivedDrDelegations(delegations)).catch(() => undefined);
    void api<{ agenda: DrVotingAgenda[] }>("/api/me/dr/voting-agenda")
      .then(({ agenda }) => setDrVotingAgenda(agenda)).catch(() => undefined);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [user, refreshAccount]);

  useEffect(() => {
    if (!user) return;
    void api<{ htsId: string | null }>("/api/hts/account")
      .then(({ htsId: current }) => { setHtsId(current ?? ""); setHtsIdInput(current ?? ""); })
      .catch((cause) => setNotice(errorText(cause, "HTS 계정 정보를 불러오지 못했습니다.")));
  }, [user]);

  useEffect(() => {
    if (!searchOpen) { setSearch(""); return; }
    setSearchTabValue("recommended");
    searchInputRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") setSearchOpen(false); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [searchOpen]);

  const refreshMarket = useCallback(async (code: string, timeframe: string, candleSeconds: number | null) => {
    if (!code) return;
    try {
      const query = new URLSearchParams({ range: timeframe });
      if (candleSeconds) query.set("interval", String(candleSeconds));
      const [chart, orderBook] = await Promise.all([
        api<{ points: Point[]; candles?: Candle[]; interval?: number }>(`/api/market/chart/${encodeURIComponent(code)}?${query}`),
        api<Book>(`/api/market/book/${encodeURIComponent(code)}`),
      ]);
      setPoints(chart.points);
      setCandles(chart.candles ?? []);
      if (chart.interval) setCandleInterval(chart.interval);
      setBook(orderBook);
    } catch (cause) {
      setNotice(errorText(cause, "시세를 불러오지 못했습니다."));
    }
  }, []);

  useEffect(() => { void refreshMarket(selectedCode, range, interval); }, [selectedCode, range, interval, refreshMarket]);
  useEffect(() => {
    if (!selectedCode) return;
    const timer = window.setInterval(() => void refreshMarket(selectedCode, range, interval), 5000);
    return () => window.clearInterval(timer);
  }, [selectedCode, range, interval, refreshMarket]);
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
    setBusy(true);
    try {
      const result = await api<{ message: string }>("/api/market/orders", {
        method: "POST", body: JSON.stringify({ assetCode: selectedCode, side, quantity: Number(quantity), limitPrice: Number(price) }),
      });
      setReview(false);
      setNotice(result.message);
      setOrderFilter("open");
      await Promise.all([refreshAccount(), refreshMarket(selectedCode, range, interval)]);
    } catch (cause) {
      setNotice(errorText(cause, "주문을 제출하지 못했습니다."));
    } finally {
      setBusy(false);
    }
  }

  async function convertDr(action: "issue" | "redeem") {
    if (!selected || selected.type !== "depositary_receipt") return;
    const qty = Number(drQuantity);
    if (!Number.isSafeInteger(qty) || qty < 1) { setNotice("DR 수량은 1 이상의 정수여야 합니다."); return; }
    const verb = action === "issue" ? "원주를 예탁해 DR을 발행" : "보유 DR을 원주로 상환";
    if (!window.confirm(`${selected.code} ${qty} DR을 ${verb}할까요?`)) return;
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
    setBusy(true);
    try {
      const result = await api<{ success: boolean; result: { status: string; message: string } }>("/api/dr/convert", { method: "POST", body: JSON.stringify({
        drCode: selected.code, action, quantity: qty,
        referenceKey,
      }) });
      setNotice(result.result.message);
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
      setNotice(`${errorText(cause, "DR 전환 요청 결과를 확인하지 못했습니다.")} 같은 수량과 방향으로 재시도하면 중복 전환되지 않습니다.`);
    } finally { setBusy(false); }
  }

  async function cancelDrConversion(item: DrCustodyInstruction) {
    if (!window.confirm(`${item.reference_key} DR 전환 요청을 취소할까요? 예약된 입력 수량이 해제됩니다.`)) return;
    setBusy(true);
    try {
      const result = await api<{ success: boolean; result: { status: string } }>(
        "/api/dr/convert/cancel", { method: "POST", body: JSON.stringify({ referenceKey: item.reference_key }) },
      );
      setNotice(result.result.status === "cancelled" ? "DR 전환 요청을 취소하고 예약 수량을 해제했습니다." : "취소 처리했습니다.");
      await refreshAccount();
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
      setNotice(`${selected.code} 의결권 ${quantityValue} DR을 ${proxyUserId}에게 위임했습니다.`);
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
      setNotice("의결권 위임을 철회했습니다.");
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
      setNotice(`${item.agendaTitle}: ${choice}로 투표했습니다. 기준일 의결권 ${money(item.votingPower)}표가 반영됩니다.`);
    } catch (cause) {
      setNotice(errorText(cause, "의결권 투표를 처리하지 못했습니다."));
    } finally { setBusy(false); }
  }

  async function cancelOrder(order: Order) {
    if (!window.confirm(`주문 #${order.order_id} (${order.asset_code} ${order.side === "buy" ? "매수" : "매도"} 잔량 ${money(order.remaining_quantity, 3)})을 취소할까요?\n매수 예치금이나 매도 수량은 바로 돌려받습니다.`)) return;
    setBusy(true);
    try {
      const result = await api<{ message: string }>(`/api/market/orders/${order.order_id}/cancel`, { method: "POST", body: "{}" });
      setNotice(result.message);
      await Promise.all([refreshAccount(), refreshMarket(selectedCode, range, interval)]);
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
    const instrument = instruments.find((item) => item.code === code);
    if (!instrument) { setNotice(`${code}은(는) 지금 거래 중인 종목이 아닙니다.`); return; }
    setSelectedCode(instrument.code);
    setPrice(String(instrument.price));
    setSearchOpen(false);
  }

  if (!user) return <div className="stage"><Backdrop /><AuthPanel app="hts" onAuthenticated={(next) => { const back = new URLSearchParams(location.search).get("return"); if (back?.startsWith("/board")) location.assign(back); else setUser(next); }} />
    <a className="board-guest-button" href={boardUrl()}>로그인 없이 게시판 보기</a>
  </div>;

  const current = points.at(-1)?.price ?? selected?.price ?? 0;
  // Amounts follow the account's nationality; instrument prices keep their trading currency.
  const homeCurrency = portfolio?.national_currency || user.currency || "";
  const homeDigits = portfolio?.home_decimal_places ?? 0;
  const homeRate = portfolio?.home_rate_nwx || 1;
  // Portfolio totals are 기준 통화; the Discord wallet shows them in the home currency.
  const inHome = (nwx: number) => nwx / homeRate;
  const holdings = (portfolio?.stocks ?? []).filter((item) => item.qty);
  const visibleOrders = orders.filter((order) => orderFilter === "all" || OPEN.has(order.status));
  const openCount = orders.filter((order) => OPEN.has(order.status)).length;

  return <div className="hts-shell stage">
    <Backdrop />
    <header className="hts-topbar">
      <a className="brand" href="/">INAZINA <span>HTS</span></a>
      <button type="button" className="search-trigger" title="종목 검색 (시장 목록)" onClick={() => setSearchOpen(true)}>
        <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M20 20l-4-4" /></svg>
        종목 검색
      </button>
      <div className="top-actions">
        <span className="connection"><i />API {health}</span>
        <span className="top-user">{user.nickname}</span>
        <a className="header-action" title="공지 · 자유 · 시장 토론" href={boardUrl()}>게시판</a>
        <button className="header-action" title="Discord 사용자 명령어" onClick={() => setCommandsOpen("")}>명령어</button>
        <button className="header-action account-action" onClick={() => { setSettingsMessage(""); setSettingsError(""); setAccountSettingsOpen(true); }}>계정 <small>{htsId || "아이디 만들기"}</small></button>
        <button className="icon-button" title="로그아웃" onClick={() => { clearToken(); void api("/api/auth/logout", { method: "POST" }); setUser(null); }}>로그아웃</button>
      </div>
    </header>
    {notice && <div className="toast" role="status">{notice}<button onClick={() => setNotice("")}>닫기</button></div>}
    <main className="hts-main">
      <div className="quote-strip"><div><p className="eyebrow">{selected?.exchange_id || "MARKET"} · {selected ? instrumentType(selected) : "—"}</p><h1>{selected?.name || "종목을 선택하세요"}<small>{selected?.code}</small><WarningBadge warning={selected?.warning} /></h1>{selected?.drProgram && <><p className="orders-hint">{selected.drProgram.receiptType} · 원주 {selected.drProgram.underlyingCode} · 1 DR = {money(selected.drProgram.underlyingPerReceipt, 6)}주 · 원주 거래소 {selected.drProgram.homeExchangeId || "—"} · 예탁 {selected.drProgram.depositaryBank || "—"} · 수탁 {selected.drProgram.custodian || "—"}</p>{selected.drProgram.referenceValue !== undefined && <p className="orders-hint">단순 원주 환산 참고가 {money(selected.drProgram.referenceValue, 4)} {selected.quote_currency || ""}{selected.drProgram.referencePremiumPercent !== undefined && ` · 현재가 괴리 ${selected.drProgram.referencePremiumPercent >= 0 ? "+" : ""}${selected.drProgram.referencePremiumPercent.toFixed(2)}%`}{selected.drProgram.referenceDeviationAlert && " · ⚠ 괴리 주의 (±10% 이상)"} · 수수료·세금·유동성 미반영</p>}</>}{selected?.bondRisk && <p className="orders-hint">액면 {money(selected.bondRisk.faceValue)} 기준 · Macaulay {selected.bondRisk.macaulay_duration.toFixed(3)}년 · 수정듀레이션 {selected.bondRisk.modified_duration.toFixed(3)} · 컨벡서티 {selected.bondRisk.convexity.toFixed(3)} · DV01 {money(selected.bondRisk.dv01, 4)} {selected.quote_currency || homeCurrency} / 1bp. 현 국채 곡선의 평행이동 민감도이며 거래가격 예측이나 규제지표가 아닙니다.</p>}{selected?.warning?.reason && <p className="warn-reason">⚠️ {selected.warning.label} · {selected.warning.reason}</p>}</div>
        <div className="quote-side"><div className="quote-value"><strong>{money(current)}</strong><span>{selected?.quote_currency || homeCurrency}</span></div><button className="quote-switch" onClick={() => setSearchOpen(true)}>종목 바꾸기</button>{selected && <a className="quote-switch" href={boardUrl(selected.code)}>종목 토론</a>}</div></div>
      <div className="trade-row">
        <div className="chart-col">
          <PriceChart points={points} candles={candles} candleInterval={candleInterval} mode={chartMode} fallbackPrice={selected?.price ?? 1}
            drawings={drawings} onDrawingsChange={setDrawings} tool={tool} onToolChange={setTool} resetKey={`${selectedCode}-${range}-${interval}-${chartMode}`} label={selected?.name || "가격"}
            controls={<>{ranges.map((item) => <button key={item} className={range === item ? "selected" : ""} onClick={() => setRange(item)}>{rangeNames[item]}</button>)}
              <span className="divider" aria-hidden="true" />
              <button className={chartMode === "candle" ? "selected" : ""} onClick={() => setChartMode("candle")}>캔들</button>
              <button className={chartMode === "line" ? "selected" : ""} onClick={() => setChartMode("line")}>선</button>
              {chartMode === "candle" && <select className="interval-select" aria-label="봉 주기" value={interval ?? ""} onChange={(event) => setIntervalSeconds(event.target.value ? Number(event.target.value) : null)}><option value="">자동 ({intervals.find(([seconds]) => seconds === candleInterval)?.[1] ?? "봉"})</option>{intervals.map(([seconds, name]) => <option key={seconds} value={seconds}>{name}</option>)}</select>}</>} />
        </div>
        <div className="ticket-col">
          <section className="data-panel order-entry"><div className="panel-title"><h2>주문 입력</h2><span>지정가</span></div>
            <div className="order-sides"><button className={side === "buy" ? "buy active" : "buy"} onClick={() => setSide("buy")}>매수</button><button className={side === "sell" ? "sell active" : "sell"} onClick={() => setSide("sell")}>매도</button></div>
            <label>지정가 ({selected?.quote_currency || homeCurrency})<input inputMode="numeric" value={price} onChange={(event) => setPrice(event.target.value.replace(/[^\d]/g, ""))} /></label>
            <label>주문 수량<input inputMode="numeric" value={quantity} onChange={(event) => setQuantity(event.target.value.replace(/[^\d]/g, ""))} /></label>
            <div className="form-total"><span>주문 금액</span><b>{money(Number(price || 0) * Number(quantity || 0), 2)} {selected?.quote_currency || homeCurrency}</b></div>
            <button className={side === "buy" ? "submit buy-submit" : "submit sell-submit"} disabled={!selected || !Number(price) || !Number(quantity) || busy} onClick={() => setReview(true)}>{side === "buy" ? "매수 주문 검토" : "매도 주문 검토"}</button>
            <p className="risk-note">호가를 누르면 그 가격이 지정가로 들어갑니다. 펀드 가입·환매와 소수점 수량은 명령어의 매수·매도를 사용하세요.</p>
          </section>
          {selected?.type === "depositary_receipt" && <section className="data-panel order-entry dr-conversion"><div className="panel-title"><h2>DR 발행 · 상환</h2><span>{selected.drProgram?.programStatus === "active" && selected.status === "active" ? "기초주식 예탁 방식" : "프로그램 중지"}</span></div>
            <p className="orders-hint">{selected.drProgram?.programStatus === "active" && selected.status === "active" ? "DR 수량을 입력하세요. 발행은 보유 원주를 잠그고 DR을 지급하며, 상환은 보유 DR을 소각하고 보관 원주를 지급합니다." : "중지되거나 종료된 DR 프로그램은 새 전환을 할 수 없습니다."}</p>
            <label>전환할 DR 수량<input inputMode="numeric" value={drQuantity} onChange={(event) => setDrQuantity(event.target.value.replace(/[^\d]/g, ""))} /></label>
            <div className="actions"><button className="submit buy-submit" disabled={busy || !Number(drQuantity) || selected.drProgram?.programStatus !== "active" || selected.status !== "active"} onClick={() => void convertDr("issue")}>원주 예탁 · DR 발행</button><button className="submit sell-submit" disabled={busy || !Number(drQuantity) || selected.drProgram?.programStatus !== "active" || selected.status !== "active"} onClick={() => void convertDr("redeem")}>DR 반납 · 원주 상환</button></div>
            <hr />
            <h3>DR 의결권 위임</h3>
            <p className="orders-hint">시뮬레이터 내부 위임 기록입니다. 위임된 수량은 매도·상환할 수 없으며, 기준일 의결권은 주주총회 투표에서 대리인에게 합산됩니다. 실제 수탁기관 지시는 포함하지 않습니다.</p>
            <label>대리 사용자 ID<input value={drProxyUserId} onChange={(event) => setDrProxyUserId(event.target.value)} placeholder="예: user-123" /></label>
            <label>위임 수량 (DR)<input inputMode="numeric" value={drVoteQuantity} onChange={(event) => setDrVoteQuantity(event.target.value.replace(/[^\d]/g, ""))} /></label>
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
          <div className="book-side asks">{[...book.asks].reverse().map((item, index) => <button type="button" className="book-row" key={`${item.limit_price}-${index}`} title="이 가격으로 주문" onClick={() => setPrice(String(item.limit_price))}><span>{money(item.quantity, 3)}</span><b>{money(item.limit_price, 2)}</b></button>)}</div>
          <div className="book-current"><span>현재가</span><strong>{money(current)}</strong></div>
          <div className="book-side bids">{book.bids.map((item, index) => <button type="button" className="book-row" key={`${item.limit_price}-${index}`} title="이 가격으로 주문" onClick={() => setPrice(String(item.limit_price))}><span>{money(item.quantity, 3)}</span><b>{money(item.limit_price, 2)}</b></button>)}</div>
        </section>
        <section className="data-panel orders-panel">
          <div className="panel-title"><h2>주문 · 체결</h2><div className="order-tabs" role="tablist" aria-label="주문 필터"><button role="tab" aria-selected={orderFilter === "open"} className={orderFilter === "open" ? "selected" : ""} onClick={() => setOrderFilter("open")}>미체결 {openCount}</button><button role="tab" aria-selected={orderFilter === "all"} className={orderFilter === "all" ? "selected" : ""} onClick={() => setOrderFilter("all")}>전체</button></div></div>
          <p className="orders-hint">미체결·부분 체결·예약 주문은 오른쪽 <b>취소</b> 버튼으로 바로 취소할 수 있어요. 남은 예치금과 수량은 즉시 돌려받습니다.</p>
          <div className="table-scroll"><table><thead><tr><th>주문</th><th>종목</th><th>구분</th><th className="r">가격</th><th className="r">수량</th><th className="r">체결</th><th className="r">잔량</th><th>상태</th><th /></tr></thead><tbody>{visibleOrders.map((order) => <tr key={order.order_id}>
            <td className="mono">#{order.order_id}</td><td><button className="link" onClick={() => chooseInstrument(order.asset_code)}>{order.asset_code}</button></td>
            <td className={order.side === "buy" ? "buy-text" : "sell-text"}>{order.side === "buy" ? "매수" : "매도"}</td><td className="r">{money(order.limit_price, 2)}</td><td className="r">{money(order.quantity, 3)}</td><td className="r">{money(order.filled_quantity, 3)}</td><td className="r">{money(order.remaining_quantity, 3)}</td>
            <td><span className={OPEN.has(order.status) ? "status open" : "status"}>{STATUS[order.status] ?? order.status}</span></td>
            <td>{OPEN.has(order.status) && <button className="cancel-button" disabled={busy} onClick={() => void cancelOrder(order)}>취소</button>}</td>
          </tr>)}</tbody></table>{!visibleOrders.length && <p className="empty">{orderFilter === "open" ? "미체결 주문이 없습니다." : "최근 주문이 없습니다."}</p>}</div>
        </section>
        <section className="wallet-tile" aria-label="내 지갑">
          <section className="wallet-summary">
            <span>내 지갑 · {portfolio?.nationality ?? user.nationality} · {homeCurrency}</span>
            <strong>{money(portfolio?.cash ?? user.cash, homeDigits)} <small>{homeCurrency}</small></strong>
            <p>총자산 {money(inHome(portfolio?.grand_total ?? 0), homeDigits)} · 순자산 {money(inHome(portfolio?.net_worth ?? 0), homeDigits)} {homeCurrency}</p>
            {portfolio?.kyc_profile && <p>KYC · {KYC_STATUS[portfolio.kyc_profile.status]} · {KYC_RISK[portfolio.kyc_profile.risk_tier]}{portfolio.kyc_profile.expires_epoch ? ` · 만료 ${date(portfolio.kyc_profile.expires_epoch)}` : ""}</p>}
            {taxResidency && <button type="button" className="text-button" onClick={() => setTaxResidencyOpen((open) => !open)}>세무거주지 · {taxResidency?.taxResidency || `국적(${taxResidency?.nationality ?? user.nationality}) 사용 중`} 변경</button>}
            {taxResidency && taxResidencyOpen && <form className="account-form tax-residency-form" onSubmit={saveTaxResidency}>
              <label>세무거주 국가<select value={taxResidencyValue} onChange={(event) => setTaxResidencyValue(event.target.value)}>
                <option value="">국적 기본값 사용 ({taxResidency?.nationality ?? user.nationality})</option>
                {(taxResidency?.countries ?? []).map((country) => <option key={country} value={country}>{country}</option>)}
              </select></label>
              <p className="account-hint">시뮬레이터 세칙 선택에 사용합니다. 증빙 확인이나 법률상 거주자 판정은 수행하지 않습니다.</p>
              {settingsMessage && <p className="account-success" role="status">{settingsMessage}</p>}{settingsError && <p className="account-error" role="alert">{settingsError}</p>}
              <button className="account-save" disabled={settingsBusy}>{settingsBusy ? "저장 중…" : "세무거주지 저장"}</button>
            </form>}
          </section>
          <div className="wallet-actions">
            <button onClick={() => setCommandsOpen("송금")}>송금</button>
            <button onClick={() => setCommandsOpen("계좌개설")}>계좌개설</button>
            <button onClick={() => setCommandsOpen("대출신청")}>대출</button>
            <button onClick={() => void refreshAccount().catch((cause) => setNotice(errorText(cause, "지갑을 새로 고치지 못했습니다.")))}>새로고침</button>
          </div>
          <section className="data-panel wallet-panel">
            <div className="wallet-row"><span>현금</span><b>{money(portfolio?.cash || 0, homeDigits)} {homeCurrency}</b></div>
            {(portfolio?.wallets ?? []).filter((wallet) => wallet.is_foreign && wallet.balance_units).map((wallet) => <div className="wallet-row" key={wallet.currency_code}><span>외화 · {wallet.currency_code}</span><b>{money(wallet.balance_units / UNIT_SCALE, wallet.decimal_places ?? 6)} {wallet.currency_code}</b></div>)}
            <div className="wallet-row"><span>통장</span><b>{money(portfolio?.account_total || 0, homeDigits)} {homeCurrency}</b></div>
            <div className="wallet-row"><span>투자 평가액</span><b>{money(inHome(portfolio?.stock_total || 0), homeDigits)} {homeCurrency}</b></div>
            <div className="wallet-row"><span>매수 예치금</span><b>{money(inHome(portfolio?.order_reserved_total || 0), homeDigits)} {homeCurrency}</b></div>
            <div className="wallet-row"><span>대출</span><b>{money(inHome(portfolio?.loan_total || 0), homeDigits)} {homeCurrency}</b></div>
            <div className="wallet-row net-worth"><span>순자산</span><b>{money(inHome(portfolio?.net_worth || 0), homeDigits)} {homeCurrency}</b></div>
            {(portfolio?.accounts ?? []).filter((account) => account.resolution_status === "active").map((account) => <div className="wallet-row account-detail" key={account.number}><span>{account.product_name} · {account.currency_code}<small>{account.number}</small>{account.rate_type === "floating" && account.next_repricing_epoch != null && <small>현재 연 {account.contract_rate?.toFixed(4) ?? "—"}% · 지표 관측 기준일 {date(Math.max(0, account.next_repricing_epoch - account.repricing_lag_seconds))} · 재산정 예정 {date(account.next_repricing_epoch)} · {RATE_BENCHMARK_LABELS[account.benchmark_key || ""] || account.benchmark_key || "기준금리"} {account.benchmark_tenor_days}일 · 새 금리는 재산정 시 확정</small>}{account.rate_resets.map((reset, index) => <small key={`${reset.applied_epoch}-${index}`}>이전 재산정 {date(reset.applied_epoch)} · {RATE_BENCHMARK_LABELS[reset.benchmark_key] || reset.benchmark_key} {reset.benchmark_tenor_days}일 · 연 {reset.old_rate}% → {reset.new_rate}%</small>)}</span><b>{money(account.balance, account.decimal_places)} {account.currency_code}</b></div>)}
          </section>
          <section className="data-panel holdings-panel">
            <div className="panel-title"><h2>보유 종목</h2><span>{holdings.length}</span></div>
            <div className="holdings">{holdings.map((item) => <button type="button" key={`${item.code}-${item.is_collateral ? "c" : "h"}`} className={item.code === selectedCode ? "holding active" : "holding"} onClick={() => chooseInstrument(item.code)}>
              <span><b>{item.name}</b><small>{item.code} · {money(item.qty, 3)}주{item.sell_reserved_qty ? ` · 매도 대기 ${money(item.sell_reserved_qty, 3)}` : ""}{item.is_collateral ? " · 담보" : ""}</small></span>
              <strong>{money(inHome(item.value), homeDigits)} <em>{homeCurrency}</em></strong>
            </button>)}{!holdings.length && <p className="empty">보유 종목이 없습니다. 위의 종목 검색으로 시장을 찾아보세요.</p>}</div>
          </section>
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
        {credit && <section className="data-panel holdings-panel"><div className="panel-title"><h2>신용점수</h2><span>{credit.score}점 / 1000</span></div>
          <div className="holdings">
            {credit.factors.map((factor) => <div className="holding" key={factor.key}><span><b>{factor.label} {signedPoints(factor.points)}</b>{factor.points < 0 && CREDIT_TIPS[factor.key] && <small>{CREDIT_TIPS[factor.key]}</small>}</span></div>)}
            {!credit.factors.length && <div className="holding"><span><b>기준점수 {credit.anchor}점</b><small>아직 점수에 반영된 신용 이력이 없습니다.</small></span></div>}
            {credit.events.filter((event) => event.type !== "legacy_baseline").slice(0, 5).map((event) => <div className="holding" key={`${event.type}:${event.reference}`}><span><b>{CREDIT_EVENTS[event.type] ?? event.type}{event.loanId ? ` · 대출 #${event.loanId}` : ""}</b><small>{date(event.occurredEpoch)}{event.reason ? ` · ${event.reason}` : ""}</small></span></div>)}
          </div>
          <p className="orders-hint">기준점수 {credit.anchor}점에 요인을 더한 시뮬레이터 점수입니다. 연체 영향은 시간이 지나며 줄고, 정상 납부·완납은 점수를 올립니다. 실제 신용평가사 점수가 아닙니다.</p>
        </section>}
        {loanSchedules.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>대출 상환 일정</h2><span>{loanSchedules.length}건</span></div>
          <div className="holdings">{loanSchedules.map((loan) => <div className="holding loan-schedule" key={loan.loan_id}>
            <span><b>#{loan.loan_id} · {loan.product_code} · 잔액 {money(loan.outstanding_balance, loan.decimal_places)} {loan.quote_currency}</b>
              {loan.underwriting_stress_dsr != null && <small>심사 당시 DSR {loan.underwriting_dsr?.toFixed(1) ?? "—"}% · 금리 +{loan.underwriting_stress_shock_bps}bp / 외화부채 환율 +{loan.underwriting_fx_stress_shock_percent}% 스트레스 DSR {loan.underwriting_stress_dsr.toFixed(1)}%</small>}
              {loan.origination_pd_12m != null && <small>취급 시 추정 12개월 부도확률(PD) {(loan.origination_pd_12m * 100).toFixed(2)}% · 현재 ECL 평가의 Stage 2 비교 기준</small>}
              {loan.current_ecl_stage != null && loan.current_ecl_pd_12m != null && <small>최근 ECL 평가 · Stage {loan.current_ecl_stage} · 12개월 PD {(loan.current_ecl_pd_12m * 100).toFixed(2)}%</small>}
              {loan.stage_cure_since_epoch != null && <small>위험 개선 관찰 시작 {date(loan.stage_cure_since_epoch)} · 90일 경과 후 Stage 2 조건이 없으면 Stage 1 재평가</small>}
              {loan.rate_type === "floating" && loan.next_repricing_epoch != null && <small>현재 연 {loan.contract_rate?.toFixed(4) ?? "—"}% · 지표 관측 기준일 {date(Math.max(0, loan.next_repricing_epoch - loan.repricing_lag_seconds))} · 다음 재산정 {date(loan.next_repricing_epoch)} · {RATE_BENCHMARK_LABELS[loan.benchmark_key || ""] || loan.benchmark_key || "기준금리"} {loan.benchmark_tenor_days}일 · 새 금리는 재산정 시 확정됩니다.</small>}
              {loan.installments.slice(0, 4).map((item) => <small key={item.schedule_id}>회차 {item.installment_number} · {date(item.due_epoch)} · 잔액 원금 {money(item.principal_due-item.principal_paid, loan.decimal_places)} / 이자 {money(item.interest_due-item.interest_paid, loan.decimal_places)} / 수수료 {money(item.fee_due-item.fee_paid, loan.decimal_places)} {loan.quote_currency}</small>)}
              {!loan.installments.length && <small>예정된 미납 회차가 없습니다.</small>}
              {loan.rate_resets.map((reset, index) => <small key={`${reset.applied_epoch}-${index}`}>금리 재산정 {date(reset.applied_epoch)} · {RATE_BENCHMARK_LABELS[reset.benchmark_key] || reset.benchmark_key} {reset.benchmark_tenor_days}일 · 연 {reset.old_rate}% → {reset.new_rate}%</small>)}
            </span>
          </div>)}</div>
          <p className="orders-hint">회차별 계약상 배분 내역입니다. 회차 잔액을 넘는 초과상환 및 경과·연체이자 별도 원장은 표시하지 않습니다.</p>
        </section>}
        {dividendPayments.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>최근 배당 수령</h2><span>최근 {dividendPayments.length}건</span></div>
          <div className="holdings">{dividendPayments.slice(0, 12).map((payment) => <div className="holding" key={payment.payment_id}>
            <span><b>{payment.asset_code} · {payment.dr_code ? `DR ${payment.dr_code}` : payment.allocation_key === "stock" ? "원주" : "배당"} · {payment.dividend_kind === "special" ? "특별배당" : "정기배당"}</b><small>{date(payment.created_epoch)} · {money(payment.quantity, 3)}주 · 세전 {money(payment.gross_amount, payment.decimal_places)} · 세금 {money(payment.tax_amount, payment.decimal_places)} {payment.quote_currency}{payment.rule_id ? ` · ${payment.tax_rate_percent}% (${payment.treaty_code ?? `규칙 #${payment.rule_id}`})` : ""}</small></span>
            <strong>{money(payment.net_amount_units / UNIT_SCALE, payment.decimal_places)} <em>{payment.quote_currency}</em></strong>
          </div>)}</div>
          <p className="orders-hint">통화별 지급액 기준 최근 100건입니다. DR 교차통화 배당은 실제 배당통화로 환산된 지급을 표시합니다.</p>
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
    {review && <div className="modal-backdrop"><section className="review-modal" role="dialog" aria-modal="true" aria-labelledby="review-title"><p className="eyebrow">ORDER REVIEW</p><h2 id="review-title">주문을 확인하세요</h2><dl><div><dt>종목</dt><dd>{selected?.name} ({selectedCode}) <WarningBadge warning={selected?.warning} /></dd></div><div><dt>유형</dt><dd className={side === "buy" ? "buy-text" : "sell-text"}>{side === "buy" ? "지정가 매수" : "지정가 매도"}</dd></div><div><dt>지정가</dt><dd>{money(Number(price), 2)} {selected?.quote_currency || homeCurrency}</dd></div><div><dt>수량</dt><dd>{money(Number(quantity), 3)}</dd></div><div><dt>예상 금액</dt><dd>{money(Number(price) * Number(quantity), 2)} {selected?.quote_currency || homeCurrency}</dd></div></dl><div className="modal-actions"><button disabled={busy} onClick={() => setReview(false)}>돌아가기</button><button className={side === "buy" ? "buy-confirm" : "sell-confirm"} disabled={busy} onClick={() => void submitOrder()}>{busy ? "제출 중" : "주문 확정"}</button></div></section></div>}
    {searchOpen && <div className="modal-backdrop search-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setSearchOpen(false); }}><section className="search-modal" role="dialog" aria-modal="true" aria-labelledby="search-title"><div className="search-modal-head"><h2 id="search-title">종목 검색</h2><button type="button" title="닫기" onClick={() => setSearchOpen(false)}>닫기</button></div><label className="search-field"><span aria-hidden="true">⌕</span><input ref={searchInputRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="종목명 또는 코드" /></label>{!search.trim() && <div className="search-tabs" role="tablist" aria-label="상품 종류">{SEARCH_TABS.filter(([id]) => id === "recommended" || tabCounts[id]).map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={searchTabValue === id} className={searchTabValue === id ? "active" : ""} onClick={() => setSearchTabValue(id)}>{label}<span>{tabCounts[id] ?? 0}</span></button>)}</div>}{!search.trim() && searchTabValue === "recommended" && <><div className="recommend-sorts" role="group" aria-label="추천 기준">{RECOMMEND_SORTS.map(([id, label]) => <button key={id} type="button" aria-pressed={recommendSort === id} className={recommendSort === id ? "active" : ""} onClick={() => setRecommendSort(id)}>{label}</button>)}</div><p className="search-caption">{RECOMMEND_SORTS.find(([id]) => id === recommendSort)![2]}</p></>}<div className="search-results">{filtered.slice(0, 200).map((item, index) => <button type="button" key={item.code} className={selectedCode === item.code ? "search-result active" : "search-result"} onClick={() => chooseInstrument(item.code)}><span>{!search.trim() && searchTabValue === "recommended" && <em className="search-rank">{index + 1}</em>}<span className="search-name"><b>{item.name} <WarningBadge warning={item.warning} /></b><small>{item.code} · {instrumentType(item)}{!search.trim() && searchTabValue === "recommended" && recommendSort === "volume" ? (item.volume24h ? ` · 24시간 ${money(item.volume24h, 2)}주` : "") : item.trades24h ? ` · 24시간 ${money(item.trades24h)}건` : ""}</small></span></span><span className="search-price"><strong>{money(item.price, 2)} {item.quote_currency || homeCurrency}</strong>{item.change24h !== null && item.change24h !== undefined && <small className={item.change24h > 0 ? "up" : item.change24h < 0 ? "down" : undefined}>{item.change24h > 0 ? "+" : ""}{item.change24h.toFixed(2)}%</small>}</span></button>)}{!filtered.length && <p className="empty">{search.trim() ? "검색 결과가 없습니다." : searchTabValue === "recommended" ? "최근 24시간 동안 해당하는 종목이 없습니다." : "이 종류의 상품이 없습니다."}</p>}</div></section></div>}
    {commandsOpen !== null && <CommandPanel initial={commandsOpen} instruments={instruments} portfolio={portfolio} orders={orders} homeCurrency={homeCurrency} onClose={() => setCommandsOpen(null)} onDone={() => void refreshAccount().catch(() => undefined)} />}
    {accountSettingsOpen && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setAccountSettingsOpen(false); }}><section className="account-modal" role="dialog" aria-modal="true" aria-labelledby="account-title"><div className="account-modal-head"><div><p className="eyebrow">HTS ACCOUNT</p><h2 id="account-title">아이디 설정</h2></div><button type="button" title="닫기" onClick={() => setAccountSettingsOpen(false)}>닫기</button></div><p className="account-description">로그인에 사용할 아이디를 설정합니다.</p><form className="account-form" onSubmit={saveHtsId}><label>아이디<input autoComplete="username" minLength={3} maxLength={32} pattern="[A-Za-z0-9_.\-]{3,32}" value={htsIdInput} onChange={(event) => setHtsIdInput(event.target.value)} placeholder="예: inazina_user" required /></label><p className="account-hint">영문, 숫자, 점, 밑줄, 하이픈으로 3~32자</p><label>현재 비밀번호<input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /></label>{settingsMessage && <p className="account-success" role="status">{settingsMessage}</p>}{settingsError && <p className="account-error" role="alert">{settingsError}</p>}<div className="modal-actions"><button type="button" disabled={settingsBusy} onClick={() => setAccountSettingsOpen(false)}>닫기</button><button className="account-save" disabled={settingsBusy || !htsIdInput}>{settingsBusy ? "저장 중…" : htsId ? "아이디 변경" : "아이디 설정"}</button></div></form></section></div>}
  </div>;
}

function groupAmount(value: string) {
  const [whole, fraction] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}

