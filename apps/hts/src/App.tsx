import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, clearToken, type ApiUser } from "../../shared/api";
import { AuthPanel } from "../../shared/AuthPanel";
import { BoardPanel } from "./BoardPanel";
import { PriceChart, type Candle, type Drawing, type Point, type Tool } from "./PriceChart";
import { CommandPanel } from "./CommandPanel";

type Warning = { level: "caution" | "warning" | "danger"; label: string; reason: string; expiresEpoch: number | null };
type DrMovement = { movement_id: number; dr_code: string; underlying_code: string; underlying_currency: string; movement_type: "issue" | "redeem"; dr_quantity: number; underlying_quantity: number; underlying_average_cost: number; underlying_average_cost_text: string; decimal_places: number; recorded_epoch: number };
type Instrument = { code: string; name: string; type: string; price: number; status: string; exchange_id?: string; quote_currency?: string; fund_category?: string | null; drProgram?: { underlyingCode: string; underlyingPerReceipt: number; receiptType: string; programStatus: string; depositaryBank?: string | null; custodian?: string | null; homeExchangeId?: string | null; referenceValue?: number; referencePremiumPercent?: number; referenceDeviationAlert?: boolean }; warning?: Warning; change24h?: number | null; tradeValue24h?: number; trades24h?: number };

// Administrator trading warnings (투자주의·경고·위험).
const WarningBadge = ({ warning }: { warning?: Warning }) => warning ? <span className={`warn-badge ${warning.level}`} title={warning.reason || warning.label}>{warning.label}</span> : null;
type Wallet = { currency_code: string; balance_units: number; decimal_places?: number; is_foreign: boolean };
type LoanSchedule = { schedule_id: number; installment_number: number; due_epoch: number; principal_due: number; interest_due: number; fee_due: number; principal_paid: number; interest_paid: number; fee_paid: number; amount_paid: number; status: string };
type LoanRateReset = { old_rate: number; new_rate: number; scheduled_epoch: number; applied_epoch: number; benchmark_key: string; benchmark_tenor_days: number };
type LoanScheduleView = { loan_id: number; product_code: string; quote_currency: string; decimal_places: number; outstanding_balance: number; status: string; collection_status: string; maturity_epoch: number | null; underwriting_dsr: number | null; underwriting_stress_dsr: number | null; installments: LoanSchedule[]; rate_resets: LoanRateReset[] };
type Holding = { code: string; name: string; qty: number; price: number; value: number; type: string | null; quote_currency?: string; sell_reserved_qty?: number; is_collateral?: boolean };
type Portfolio = { cash: number; grand_total: number; net_worth: number; loan_total: number; order_reserved_total: number; stock_total: number; account_total: number; bond_total?: number; national_currency?: string; nationality?: string; home_rate_nwx?: number; home_decimal_places?: number; wallets?: Wallet[]; stocks: Holding[]; accounts?: Record<string, unknown>[] };
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
// Most traded value in 24 hours first, then the biggest movers.
const recommended = (items: Instrument[]) => [...items]
  .sort((a, b) => (b.tradeValue24h ?? 0) - (a.tradeValue24h ?? 0) || Math.abs(b.change24h ?? 0) - Math.abs(a.change24h ?? 0))
  .slice(0, 20);
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
  // null: closed; "" : every board; a code: that instrument's market discussion.
  const [boardAsset, setBoardAsset] = useState<string | null>(null);
  const [accountSettingsOpen, setAccountSettingsOpen] = useState(false);
  const [htsId, setHtsId] = useState("");
  const [htsIdInput, setHtsIdInput] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [settingsMessage, setSettingsMessage] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [range, setRange] = useState<string>("1d");
  const [chartMode, setChartMode] = useState<"candle" | "line">("candle");
  const [interval, setIntervalSeconds] = useState<number | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [candleInterval, setCandleInterval] = useState(600);
  const [points, setPoints] = useState<Point[]>([]);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [drMovements, setDrMovements] = useState<DrMovement[]>([]);
  const [loanSchedules, setLoanSchedules] = useState<LoanScheduleView[]>([]);
  const [orderFilter, setOrderFilter] = useState<"open" | "all">("open");
  const [book, setBook] = useState<Book>({ bids: [], asks: [] });
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("1");
  const [drQuantity, setDrQuantity] = useState("1");
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
    if (!term) return searchTabValue === "recommended" ? recommended(instruments) : instruments.filter((item) => searchTab(item) === searchTabValue);
    return instruments.filter((item) => `${item.code} ${item.name} ${item.type} ${instrumentType(item)}`.toLocaleLowerCase().includes(term));
  }, [instruments, search, searchTabValue]);
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

  const refreshAccount = useCallback(async () => {
    const [wallet, orderData, loanData] = await Promise.all([
      api<{ portfolio: Portfolio }>("/api/me/portfolio"),
      api<{ orders: Order[] }>("/api/market/orders"),
      api<{ loans: LoanScheduleView[] }>("/api/me/loan-schedules"),
    ]);
    setPortfolio(wallet.portfolio);
    setOrders(orderData.orders);
    setLoanSchedules(loanData.loans);
  }, []);

  useEffect(() => {
    void fetch("/api/health").then((response) => response.json()).then((result) => setHealth(result.python === "connected" ? "연결됨" : "Python 연결 끊김")).catch(() => setHealth("연결 끊김"));
    void authenticate();
  }, [authenticate]);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    Promise.all([api<{ instruments: Instrument[] }>("/api/market/instruments"), refreshAccount()])
      .then(([market]) => {
        if (cancelled) return;
        setInstruments(market.instruments);
        setSelectedCode((current) => current || market.instruments[0]?.code || "");
      })
      .catch((cause) => { if (!cancelled) setNotice(errorText(cause, "화면 데이터를 불러오지 못했습니다.")); });
    const timer = window.setInterval(() => void refreshAccount().catch(() => undefined), 15000);
    void api<{ movements: DrMovement[] }>("/api/me/dr/movements")
      .then(({ movements }) => setDrMovements(movements)).catch(() => undefined);
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
      const result = await api<{ message: string }>("/api/dr/convert", { method: "POST", body: JSON.stringify({
        drCode: selected.code, action, quantity: qty,
        referenceKey,
      }) });
      setNotice(result.message);
      drRequestKeys.current.delete(requestKey);
      try { window.sessionStorage.removeItem(storageKey); } catch { /* ignore unavailable session storage */ }
      try {
        const [, , history] = await Promise.all([
          refreshAccount(), refreshMarket(selectedCode, range, interval),
          api<{ movements: DrMovement[] }>("/api/me/dr/movements"),
        ]);
        setDrMovements(history.movements);
      } catch {
        setNotice(`${result.message} 화면 새로고침에 실패했습니다. 다시 불러와 잔액과 이력을 확인해주세요.`);
      }
    } catch (cause) {
      setNotice(`${errorText(cause, "DR 전환 요청 결과를 확인하지 못했습니다.")} 같은 수량과 방향으로 재시도하면 중복 전환되지 않습니다.`);
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

  if (!user) return <div className="stage"><Backdrop /><AuthPanel app="hts" onAuthenticated={setUser} />
    <button type="button" className="board-guest-button" onClick={() => setBoardAsset("")}>로그인 없이 게시판 보기</button>
    {boardAsset !== null && <BoardPanel user={null} initialAsset={boardAsset} onClose={() => setBoardAsset(null)} onLogin={() => setBoardAsset(null)} />}
  </div>;

  const current = points.at(-1)?.price ?? selected?.price ?? 0;
  // Amounts follow the account's nationality; instrument prices keep their trading currency.
  const homeCurrency = portfolio?.national_currency || user.currency || "NWX";
  const homeDigits = portfolio?.home_decimal_places ?? 0;
  const homeRate = portfolio?.home_rate_nwx || 1;
  // Portfolio totals are NWX; the Discord wallet shows them in the home currency.
  const inHome = (nwx: number) => nwx / homeRate;
  const holdings = (portfolio?.stocks ?? []).filter((item) => item.qty);
  const visibleOrders = orders.filter((order) => orderFilter === "all" || OPEN.has(order.status));
  const openCount = orders.filter((order) => OPEN.has(order.status)).length;

  return <div className="hts-shell stage">
    <Backdrop />
    <header className="hts-topbar"><a className="brand" href="/">INAZINA <span>HTS</span></a>
      <div className="top-center"><span className="connection"><i />API {health}</span><span>{user.nickname}</span></div>
      <div className="top-actions">
        <button className="header-action" title="종목 검색 (시장 목록)" onClick={() => setSearchOpen(true)}>종목 검색</button>
        <button className="header-action" title="공지 · 자유 · 시장 토론" onClick={() => setBoardAsset("")}>게시판</button>
        <button className="header-action" title="Discord 사용자 명령어" onClick={() => setCommandsOpen("")}>명령어</button>
        <button className="header-action account-action" onClick={() => { setSettingsMessage(""); setSettingsError(""); setAccountSettingsOpen(true); }}>계정 <small>{htsId || "아이디 만들기"}</small></button>
        <button className="icon-button" title="로그아웃" onClick={() => { clearToken(); void api("/api/auth/logout", { method: "POST" }); setUser(null); }}>로그아웃</button>
      </div>
    </header>
    {notice && <div className="toast" role="status">{notice}<button onClick={() => setNotice("")}>닫기</button></div>}
    <main className="hts-layout">
      <aside className="wallet-rail" aria-label="내 지갑">
        <section className="wallet-summary">
          <span>내 지갑 · {portfolio?.nationality ?? user.nationality} · {homeCurrency}</span>
          <strong>{money(portfolio?.cash ?? user.cash, homeDigits)} <small>{homeCurrency}</small></strong>
          <p>총자산 {money(inHome(portfolio?.grand_total ?? 0), homeDigits)} · 순자산 {money(inHome(portfolio?.net_worth ?? 0), homeDigits)} {homeCurrency}</p>
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
        </section>
        {loanSchedules.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>대출 상환 일정</h2><span>{loanSchedules.length}건</span></div>
          <div className="holdings">{loanSchedules.map((loan) => <div className="holding loan-schedule" key={loan.loan_id}>
            <span><b>#{loan.loan_id} · {loan.product_code} · 잔액 {money(loan.outstanding_balance, loan.decimal_places)} {loan.quote_currency}</b>
              {loan.underwriting_stress_dsr != null && <small>심사 당시 DSR {loan.underwriting_dsr?.toFixed(1) ?? "—"}% · +200bp 스트레스 DSR {loan.underwriting_stress_dsr.toFixed(1)}%</small>}
              {loan.installments.slice(0, 4).map((item) => <small key={item.schedule_id}>회차 {item.installment_number} · {date(item.due_epoch)} · 잔액 원금 {money(item.principal_due-item.principal_paid, loan.decimal_places)} / 이자 {money(item.interest_due-item.interest_paid, loan.decimal_places)} / 수수료 {money(item.fee_due-item.fee_paid, loan.decimal_places)} {loan.quote_currency}</small>)}
              {!loan.installments.length && <small>예정된 미납 회차가 없습니다.</small>}
              {loan.rate_resets.map((reset, index) => <small key={`${reset.applied_epoch}-${index}`}>금리 재산정 {date(reset.applied_epoch)} · {reset.benchmark_key} {reset.benchmark_tenor_days}일 · 연 {reset.old_rate}% → {reset.new_rate}%</small>)}
            </span>
          </div>)}</div>
          <p className="orders-hint">회차별 계약상 배분 내역입니다. 회차 잔액을 넘는 초과상환 및 경과·연체이자 별도 원장은 표시하지 않습니다.</p>
        </section>}
        <section className="data-panel holdings-panel">
          <div className="panel-title"><h2>보유 종목</h2><span>{holdings.length}</span></div>
          <div className="holdings">{holdings.map((item) => <button type="button" key={`${item.code}-${item.is_collateral ? "c" : "h"}`} className={item.code === selectedCode ? "holding active" : "holding"} onClick={() => chooseInstrument(item.code)}>
            <span><b>{item.name}</b><small>{item.code} · {money(item.qty, 3)}주{item.sell_reserved_qty ? ` · 매도 대기 ${money(item.sell_reserved_qty, 3)}` : ""}{item.is_collateral ? " · 담보" : ""}</small></span>
            <strong>{money(inHome(item.value), homeDigits)} <em>{homeCurrency}</em></strong>
          </button>)}{!holdings.length && <p className="empty">보유 종목이 없습니다. 위의 종목 검색으로 시장을 찾아보세요.</p>}</div>
        </section>
        {drMovements.length > 0 && <section className="data-panel holdings-panel"><div className="panel-title"><h2>DR 전환 이력</h2><span>최근 {drMovements.length}건</span></div>
          <div className="holdings">{drMovements.map((item) => <div className="holding" key={item.movement_id}>
            <span><b>{item.movement_type === "issue" ? "DR 발행" : "원주 상환"} · {item.dr_code}</b><small>{date(item.recorded_epoch)} · {money(item.dr_quantity, 0)} DR ↔ {money(item.underlying_quantity, 0)}주 {item.underlying_code} · 원주 평균원가 {groupAmount(item.underlying_average_cost_text)} {item.underlying_currency}</small></span>
          </div>)}</div>
          <p className="orders-hint">게임 내 예탁 이동 기록이며 실제 수탁기관 보관 증빙은 아닙니다.</p>
        </section>}
      </aside>
      <section className="main-market">
        <div className="quote-strip"><div><p className="eyebrow">{selected?.exchange_id || "MARKET"} · {selected ? instrumentType(selected) : "—"}</p><h1>{selected?.name || "종목을 선택하세요"}<small>{selected?.code}</small><WarningBadge warning={selected?.warning} /></h1>{selected?.drProgram && <><p className="orders-hint">{selected.drProgram.receiptType} · 원주 {selected.drProgram.underlyingCode} · 1 DR = {money(selected.drProgram.underlyingPerReceipt, 6)}주 · 원주 거래소 {selected.drProgram.homeExchangeId || "—"} · 예탁 {selected.drProgram.depositaryBank || "—"} · 수탁 {selected.drProgram.custodian || "—"}</p>{selected.drProgram.referenceValue !== undefined && <p className="orders-hint">단순 원주 환산 참고가 {money(selected.drProgram.referenceValue, 4)} {selected.quote_currency || ""}{selected.drProgram.referencePremiumPercent !== undefined && ` · 현재가 괴리 ${selected.drProgram.referencePremiumPercent >= 0 ? "+" : ""}${selected.drProgram.referencePremiumPercent.toFixed(2)}%`}{selected.drProgram.referenceDeviationAlert && " · ⚠ 괴리 주의 (±10% 이상)"} · 수수료·세금·유동성 미반영</p>}</>}{selected?.warning?.reason && <p className="warn-reason">⚠️ {selected.warning.label} · {selected.warning.reason}</p>}</div>
          <div className="quote-side"><div className="quote-value"><strong>{money(current)}</strong><span>{selected?.quote_currency || "NWX"}</span></div><button className="quote-switch" onClick={() => setSearchOpen(true)}>종목 바꾸기</button>{selected && <button className="quote-switch" onClick={() => setBoardAsset(selected.code)}>종목 토론</button>}</div></div>
        <PriceChart points={points} candles={candles} candleInterval={candleInterval} mode={chartMode} fallbackPrice={selected?.price ?? 1}
          drawings={drawings} onDrawingsChange={setDrawings} tool={tool} onToolChange={setTool} resetKey={`${selectedCode}-${range}-${interval}-${chartMode}`} label={selected?.name || "가격"}
          controls={<>{ranges.map((item) => <button key={item} className={range === item ? "selected" : ""} onClick={() => setRange(item)}>{rangeNames[item]}</button>)}
            <span className="divider" aria-hidden="true" />
            <button className={chartMode === "candle" ? "selected" : ""} onClick={() => setChartMode("candle")}>캔들</button>
            <button className={chartMode === "line" ? "selected" : ""} onClick={() => setChartMode("line")}>선</button>
            {chartMode === "candle" && <select className="interval-select" aria-label="봉 주기" value={interval ?? ""} onChange={(event) => setIntervalSeconds(event.target.value ? Number(event.target.value) : null)}><option value="">자동 ({intervals.find(([seconds]) => seconds === candleInterval)?.[1] ?? "봉"})</option>{intervals.map(([seconds, name]) => <option key={seconds} value={seconds}>{name}</option>)}</select>}</>} />
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
      </section>
      <aside className="trade-rail">
        <section className="data-panel orderbook-panel"><div className="panel-title"><h2>호가</h2><span>수량 · 가격</span></div><div className="book-head"><span>수량</span><span>가격</span></div>
          <div className="book-side asks">{[...book.asks].reverse().map((item, index) => <button type="button" className="book-row" key={`${item.limit_price}-${index}`} title="이 가격으로 주문" onClick={() => setPrice(String(item.limit_price))}><span>{money(item.quantity, 3)}</span><b>{money(item.limit_price, 2)}</b></button>)}</div>
          <div className="book-current"><span>현재가</span><strong>{money(current)}</strong></div>
          <div className="book-side bids">{book.bids.map((item, index) => <button type="button" className="book-row" key={`${item.limit_price}-${index}`} title="이 가격으로 주문" onClick={() => setPrice(String(item.limit_price))}><span>{money(item.quantity, 3)}</span><b>{money(item.limit_price, 2)}</b></button>)}</div>
        </section>
        {selected?.type === "depositary_receipt" && <section className="data-panel order-entry dr-conversion"><div className="panel-title"><h2>DR 발행 · 상환</h2><span>{selected.drProgram?.programStatus === "active" && selected.status === "active" ? "기초주식 예탁 방식" : "프로그램 중지"}</span></div>
          <p className="orders-hint">{selected.drProgram?.programStatus === "active" && selected.status === "active" ? "DR 수량을 입력하세요. 발행은 보유 원주를 잠그고 DR을 지급하며, 상환은 보유 DR을 소각하고 보관 원주를 지급합니다." : "중지되거나 종료된 DR 프로그램은 새 전환을 할 수 없습니다."}</p>
          <label>전환할 DR 수량<input inputMode="numeric" value={drQuantity} onChange={(event) => setDrQuantity(event.target.value.replace(/[^\d]/g, ""))} /></label>
          <div className="actions"><button className="submit buy-submit" disabled={busy || !Number(drQuantity) || selected.drProgram?.programStatus !== "active" || selected.status !== "active"} onClick={() => void convertDr("issue")}>원주 예탁 · DR 발행</button><button className="submit sell-submit" disabled={busy || !Number(drQuantity) || selected.drProgram?.programStatus !== "active" || selected.status !== "active"} onClick={() => void convertDr("redeem")}>DR 반납 · 원주 상환</button></div>
        </section>}
        <section className="data-panel order-entry"><div className="panel-title"><h2>주문 입력</h2><span>지정가</span></div>
          <div className="order-sides"><button className={side === "buy" ? "buy active" : "buy"} onClick={() => setSide("buy")}>매수</button><button className={side === "sell" ? "sell active" : "sell"} onClick={() => setSide("sell")}>매도</button></div>
          <label>지정가 ({selected?.quote_currency || "NWX"})<input inputMode="numeric" value={price} onChange={(event) => setPrice(event.target.value.replace(/[^\d]/g, ""))} /></label>
          <label>주문 수량<input inputMode="numeric" value={quantity} onChange={(event) => setQuantity(event.target.value.replace(/[^\d]/g, ""))} /></label>
          <div className="form-total"><span>주문 금액</span><b>{money(Number(price || 0) * Number(quantity || 0), 2)} {selected?.quote_currency || "NWX"}</b></div>
          <button className={side === "buy" ? "submit buy-submit" : "submit sell-submit"} disabled={!selected || !Number(price) || !Number(quantity) || busy} onClick={() => setReview(true)}>{side === "buy" ? "매수 주문 검토" : "매도 주문 검토"}</button>
          <p className="risk-note">호가를 누르면 그 가격이 지정가로 들어갑니다. 펀드 가입·환매와 소수점 수량은 명령어의 매수·매도를 사용하세요.</p>
        </section>
      </aside>
    </main>
    {review && <div className="modal-backdrop"><section className="review-modal" role="dialog" aria-modal="true" aria-labelledby="review-title"><p className="eyebrow">ORDER REVIEW</p><h2 id="review-title">주문을 확인하세요</h2><dl><div><dt>종목</dt><dd>{selected?.name} ({selectedCode}) <WarningBadge warning={selected?.warning} /></dd></div><div><dt>유형</dt><dd className={side === "buy" ? "buy-text" : "sell-text"}>{side === "buy" ? "지정가 매수" : "지정가 매도"}</dd></div><div><dt>지정가</dt><dd>{money(Number(price), 2)} {selected?.quote_currency || "NWX"}</dd></div><div><dt>수량</dt><dd>{money(Number(quantity), 3)}</dd></div><div><dt>예상 금액</dt><dd>{money(Number(price) * Number(quantity), 2)} {selected?.quote_currency || "NWX"}</dd></div></dl><div className="modal-actions"><button disabled={busy} onClick={() => setReview(false)}>돌아가기</button><button className={side === "buy" ? "buy-confirm" : "sell-confirm"} disabled={busy} onClick={() => void submitOrder()}>{busy ? "제출 중" : "주문 확정"}</button></div></section></div>}
    {searchOpen && <div className="modal-backdrop search-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setSearchOpen(false); }}><section className="search-modal" role="dialog" aria-modal="true" aria-labelledby="search-title"><div className="search-modal-head"><h2 id="search-title">종목 검색</h2><button type="button" title="닫기" onClick={() => setSearchOpen(false)}>닫기</button></div><label className="search-field"><span aria-hidden="true">⌕</span><input ref={searchInputRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="종목명 또는 코드" /></label>{!search.trim() && <div className="search-tabs" role="tablist" aria-label="상품 종류">{SEARCH_TABS.filter(([id]) => id === "recommended" || tabCounts[id]).map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={searchTabValue === id} className={searchTabValue === id ? "active" : ""} onClick={() => setSearchTabValue(id)}>{label}<span>{tabCounts[id] ?? 0}</span></button>)}</div>}{!search.trim() && searchTabValue === "recommended" && <p className="search-caption">최근 24시간 거래대금이 많은 순, 같으면 등락폭이 큰 순</p>}<div className="search-results">{filtered.slice(0, 200).map((item, index) => <button type="button" key={item.code} className={selectedCode === item.code ? "search-result active" : "search-result"} onClick={() => chooseInstrument(item.code)}><span>{!search.trim() && searchTabValue === "recommended" && <em className="search-rank">{index + 1}</em>}<span className="search-name"><b>{item.name} <WarningBadge warning={item.warning} /></b><small>{item.code} · {instrumentType(item)}{item.trades24h ? ` · 24시간 ${money(item.trades24h)}건` : ""}</small></span></span><span className="search-price"><strong>{money(item.price, 2)} {item.quote_currency || "NWX"}</strong>{item.change24h !== null && item.change24h !== undefined && <small className={item.change24h > 0 ? "up" : item.change24h < 0 ? "down" : undefined}>{item.change24h > 0 ? "+" : ""}{item.change24h.toFixed(2)}%</small>}</span></button>)}{!filtered.length && <p className="empty">{search.trim() ? "검색 결과가 없습니다." : "이 종류의 상품이 없습니다."}</p>}</div></section></div>}
    {boardAsset !== null && <BoardPanel user={user} initialAsset={boardAsset} onClose={() => setBoardAsset(null)} onLogin={() => setBoardAsset(null)} />}
    {commandsOpen !== null && <CommandPanel initial={commandsOpen} instruments={instruments} portfolio={portfolio} orders={orders} homeCurrency={portfolio?.national_currency || user.currency || "NWX"} onClose={() => setCommandsOpen(null)} onDone={() => void refreshAccount().catch(() => undefined)} />}
    {accountSettingsOpen && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setAccountSettingsOpen(false); }}><section className="account-modal" role="dialog" aria-modal="true" aria-labelledby="account-title"><div className="account-modal-head"><div><p className="eyebrow">HTS ACCOUNT</p><h2 id="account-title">아이디 설정</h2></div><button type="button" title="닫기" onClick={() => setAccountSettingsOpen(false)}>닫기</button></div><p className="account-description">로그인에 사용할 아이디를 설정합니다.</p><form className="account-form" onSubmit={saveHtsId}><label>아이디<input autoComplete="username" minLength={3} maxLength={32} pattern="[A-Za-z0-9_.\-]{3,32}" value={htsIdInput} onChange={(event) => setHtsIdInput(event.target.value)} placeholder="예: inazina_user" required /></label><p className="account-hint">영문, 숫자, 점, 밑줄, 하이픈으로 3~32자</p><label>현재 비밀번호<input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /></label>{settingsMessage && <p className="account-success" role="status">{settingsMessage}</p>}{settingsError && <p className="account-error" role="alert">{settingsError}</p>}<div className="modal-actions"><button type="button" disabled={settingsBusy} onClick={() => setAccountSettingsOpen(false)}>닫기</button><button className="account-save" disabled={settingsBusy || !htsIdInput}>{settingsBusy ? "저장 중…" : htsId ? "아이디 변경" : "아이디 설정"}</button></div></form></section></div>}
  </div>;
}

function groupAmount(value: string) {
  const [whole, fraction] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}

function Backdrop() {
  return <>
    <div className="wall" aria-hidden="true" />
    <div className="blob b1" aria-hidden="true" />
    <div className="blob b2" aria-hidden="true" />
  </>;
}
