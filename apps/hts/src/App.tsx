import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, clearToken, type ApiUser } from "../../shared/api";
import { AuthPanel } from "../../shared/AuthPanel";

type Instrument = { code: string; name: string; type: string; price: number; status: string; exchange_id?: string; quote_currency?: string; fund_category?: string | null };
type Point = { time: number; price: number };
type Candle = { time: number; open: number; high: number; low: number; close: number; ticks: number };
type Drawing = { id: string; type: "trend" | "horizontal"; anchors: Point[] };
type Wallet = { currency_code: string; balance_units: number; decimal_places?: number; is_foreign: boolean };
type Holding = { code: string; name: string; qty: number; price: number; value: number; type: string | null; quote_currency?: string; sell_reserved_qty?: number; is_collateral?: boolean };
type Portfolio = { cash: number; grand_total: number; net_worth: number; loan_total: number; order_reserved_total: number; stock_total: number; account_total: number; national_currency?: string; home_rate_nwx?: number; home_decimal_places?: number; wallets?: Wallet[]; stocks: Holding[] };
type Order = { order_id: number; asset_code: string; side: string; limit_price: number; quantity: number; remaining_quantity: number; filled_quantity: number; status: string };
type Book = { bids: { limit_price: number; quantity: number; order_count: number }[]; asks: { limit_price: number; quantity: number; order_count: number }[] };

const W = 1000, H = 420, L = 62, R = 925, T = 24, B = 374;
const ranges = ["1h", "1d", "1w", "1m", "all"] as const;
const rangeNames: Record<string, string> = { "1h": "1시간", "1d": "1일", "1w": "1주", "1m": "1개월", all: "전체" };
const intervals: [number, string][] = [[60, "1분봉"], [300, "5분봉"], [600, "10분봉"], [1800, "30분봉"], [3600, "1시간봉"], [21600, "6시간봉"], [43200, "12시간봉"], [86400, "일봉"]];
const UNIT_SCALE = 1_000_000;
const money = (value: number, digits = 0) => new Intl.NumberFormat("ko-KR", { maximumFractionDigits: digits }).format(value || 0);
const fundCategoryName: Record<string, string> = { equity: "주식형", bond: "채권형", mixed: "혼합형", other: "기타형" };
const instrumentType = (item: Instrument) => fundCategoryName[item.fund_category || ""] || item.type;

export default function App() {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [health, setHealth] = useState("확인 중");
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
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
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState(0);
  const [points, setPoints] = useState<Point[]>([]);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [book, setBook] = useState<Book>({ bids: [], asks: [] });
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("1");
  const [price, setPrice] = useState("");
  const [review, setReview] = useState(false);
  const [tool, setTool] = useState<"select" | "trend" | "horizontal">("select");
  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const [selectedDrawing, setSelectedDrawing] = useState("");
  const [draft, setDraft] = useState<Point[] | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const draftStart = useRef<Point | null>(null);
  const moveStart = useRef<{ point: Point; drawing: Drawing } | null>(null);
  const chartGesture = useRef(false);
  const queuedPoints = useRef<{ points: Point[]; candles: Candle[] } | null>(null);
  const previousDrawings = useRef<Drawing[][]>([]);
  // Drawings are saved per user and instrument once the server copy is loaded.
  const drawingsLoadedFor = useRef("");
  const savedDrawings = useRef("[]");

  const selected = instruments.find((item) => item.code === selectedCode) ?? null;
  const filtered = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    return instruments.filter((item) => !term || `${item.code} ${item.name} ${item.type} ${instrumentType(item)}`.toLocaleLowerCase().includes(term));
  }, [instruments, search]);
  const extent = useMemo(() => {
    const useCandles = chartMode === "candle" && candles.length > 0;
    const all = useCandles ? candles.map((item) => ({ time: item.time, price: item.close }))
      : points.length ? points : selected ? [{ time: Date.now() / 1000, price: selected.price }] : [];
    const prices = all.map((item) => item.price).filter((value) => Number.isFinite(value) && value > 0);
    const baseStart = all[0]?.time ?? 0;
    const baseEnd = (all.at(-1)?.time ?? 1) + (useCandles ? candleInterval : 0);
    const span = Math.max(1, baseEnd - baseStart) / zoom;
    let maxTime = baseEnd - pan * span * .5;
    let minTime = maxTime - span;
    if (minTime === maxTime) { minTime -= 1800; maxTime += 1800; }
    const visibleCandles = useCandles ? candles.filter((item) => item.time + candleInterval >= minTime && item.time <= maxTime) : [];
    const visible = all.filter((item) => item.time >= minTime && item.time <= maxTime);
    const visiblePrices = visibleCandles.length ? visibleCandles.flatMap((item) => [item.low, item.high])
      : visible.length ? visible.map((item) => item.price) : prices;
    let minPrice = Math.min(...visiblePrices), maxPrice = Math.max(...visiblePrices);
    if (!Number.isFinite(minPrice) || !Number.isFinite(maxPrice)) { minPrice = 1; maxPrice = 2; }
    if (minPrice === maxPrice) { minPrice *= .99; maxPrice *= 1.01; }
    const gap = (maxPrice - minPrice) * .08;
    return { minTime, maxTime, minPrice: Math.max(0, minPrice - gap), maxPrice: maxPrice + gap, all, visibleCandles };
  }, [points, candles, candleInterval, chartMode, selected, zoom, pan]);
  const X = (time: number) => L + ((time - extent.minTime) / (extent.maxTime - extent.minTime)) * (R - L);
  const Y = (value: number) => B - ((value - extent.minPrice) / (extent.maxPrice - extent.minPrice)) * (B - T);

  const authenticate = useCallback(async () => {
    try {
      const result = await api<{ user: ApiUser }>("/api/auth/session");
      setUser(result.user);
    } catch {
      setUser(null);
    }
  }, []);

  const refreshOrders = useCallback(async () => {
    const result = await api<{ orders: Order[] }>("/api/market/orders");
    setOrders(result.orders);
  }, []);

  useEffect(() => {
    void fetch("/api/health").then((response) => response.json()).then((result) => setHealth(result.python === "connected" ? "연결됨" : "Python 연결 끊김")).catch(() => setHealth("연결 끊김"));
    void authenticate();
  }, [authenticate]);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    Promise.all([
      api<{ instruments: Instrument[] }>("/api/market/instruments"),
      api<{ portfolio: Portfolio }>("/api/me/portfolio"),
      api<{ orders: Order[] }>("/api/market/orders"),
    ]).then(([market, wallet, orderData]) => {
      if (cancelled) return;
      setInstruments(market.instruments);
      setSelectedCode((current) => current || market.instruments[0]?.code || "");
      setPortfolio(wallet.portfolio);
      setOrders(orderData.orders);
      setError("");
    }).catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause.message : "화면 데이터를 불러오지 못했습니다."); });
    return () => { cancelled = true; };
  }, [user, refreshOrders]);

  useEffect(() => {
    if (!user) return;
    void api<{ htsId: string | null }>("/api/hts/account")
      .then(({ htsId: current }) => { setHtsId(current ?? ""); setHtsIdInput(current ?? ""); })
      .catch((cause) => setError(cause instanceof Error ? cause.message : "HTS 계정 정보를 불러오지 못했습니다."));
  }, [user]);

  useEffect(() => {
    if (!searchOpen) { setSearch(""); return; }
    searchInputRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setSearchOpen(false);
    }
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
      const next = { points: chart.points, candles: chart.candles ?? [] };
      if (chartGesture.current) queuedPoints.current = next;
      else { setPoints(next.points); setCandles(next.candles); }
      if (chart.interval) setCandleInterval(chart.interval);
      setBook(orderBook);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "시세를 불러오지 못했습니다.");
    }
  }, []);

  useEffect(() => { void refreshMarket(selectedCode, range, interval); }, [selectedCode, range, interval, refreshMarket]);

  useEffect(() => {
    if (!selectedCode) return;
    const timer = window.setInterval(() => void refreshMarket(selectedCode, range, interval), 5000);
    return () => window.clearInterval(timer);
  }, [selectedCode, range, interval, refreshMarket]);

  useEffect(() => {
    if (selected) setPrice(String(selected.price));
  }, [selected?.code]);

  useEffect(() => {
    if (!selectedCode || !user) return;
    let cancelled = false;
    drawingsLoadedFor.current = "";
    setDrawings([]);
    setSelectedDrawing("");
    previousDrawings.current = [];
    api<{ drawings: Drawing[] }>(`/api/market/drawings/${encodeURIComponent(selectedCode)}`)
      .then((result) => {
        if (cancelled) return;
        savedDrawings.current = JSON.stringify(result.drawings);
        drawingsLoadedFor.current = selectedCode;
        setDrawings(result.drawings);
      })
      .catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause.message : "차트 선을 불러오지 못했습니다."); });
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
        .catch((cause) => setError(cause instanceof Error ? cause.message : "차트 선을 저장하지 못했습니다."));
    }, 600);
    return () => window.clearTimeout(timer);
  }, [drawings, selectedCode]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.key === "Delete" || event.key === "Backspace") && selectedDrawing && !(event.target instanceof HTMLInputElement)) {
        previousDrawings.current.push(drawings);
        setDrawings((current) => current.filter((item) => item.id !== selectedDrawing));
        setSelectedDrawing("");
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [drawings, selectedDrawing]);

  function position(clientX: number, clientY: number): Point {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return { time: extent.minTime, price: extent.maxPrice };
    const x = ((clientX - rect.left) / rect.width) * W;
    const y = ((clientY - rect.top) / rect.height) * H;
    return {
      time: Math.round(extent.minTime + ((x - L) / (R - L)) * (extent.maxTime - extent.minTime)),
      price: Math.max(1, extent.maxPrice - ((y - T) / (B - T)) * (extent.maxPrice - extent.minPrice)),
    };
  }

  function chartDown(event: React.PointerEvent<SVGSVGElement>) {
    if (tool === "select") { setSelectedDrawing(""); return; }
    const anchor = position(event.clientX, event.clientY);
    if (tool === "horizontal") {
      previousDrawings.current.push(drawings);
      setDrawings((current) => [...current, { id: crypto.randomUUID(), type: "horizontal", anchors: [anchor] }]);
      setTool("select");
    } else {
      chartGesture.current = true;
      draftStart.current = anchor;
      setDraft([anchor, anchor]);
      event.currentTarget.setPointerCapture(event.pointerId);
    }
  }

  function chartMove(event: React.PointerEvent<SVGSVGElement>) {
    const anchor = position(event.clientX, event.clientY);
    if (draftStart.current) setDraft([draftStart.current, anchor]);
    if (moveStart.current) {
      const dx = anchor.time - moveStart.current.point.time;
      const dy = anchor.price - moveStart.current.point.price;
      const original = moveStart.current.drawing;
      setDrawings((current) => current.map((item) => item.id === original.id ? {
        ...original, anchors: original.anchors.map((point) => ({ time: point.time + dx, price: Math.max(1, point.price + dy) })),
      } : item));
    }
  }

  function chartUp(event: React.PointerEvent<SVGSVGElement>) {
    if (draftStart.current) {
      const end = position(event.clientX, event.clientY);
      if (Math.abs(end.time - draftStart.current.time) > 1 || Math.abs(end.price - draftStart.current.price) > .001) {
        previousDrawings.current.push(drawings);
        setDrawings((current) => [...current, { id: crypto.randomUUID(), type: "trend", anchors: [draftStart.current!, end] }]);
      }
    }
    draftStart.current = null;
    moveStart.current = null;
    setDraft(null);
    chartGesture.current = false;
    flushQueuedChart();
  }

  function flushQueuedChart() {
    if (queuedPoints.current) { setPoints(queuedPoints.current.points); setCandles(queuedPoints.current.candles); }
    queuedPoints.current = null;
  }

  function chartCancel() {
    draftStart.current = null;
    moveStart.current = null;
    setDraft(null);
    chartGesture.current = false;
    flushQueuedChart();
  }

  function beginMove(event: React.PointerEvent<SVGGElement>, drawing: Drawing) {
    event.stopPropagation();
    setSelectedDrawing(drawing.id);
    if (tool === "select") {
      chartGesture.current = true;
      previousDrawings.current.push(drawings);
      moveStart.current = { point: position(event.clientX, event.clientY), drawing };
      svgRef.current?.setPointerCapture(event.pointerId);
    }
  }

  function undo() {
    const last = previousDrawings.current.pop();
    if (last) setDrawings(last);
  }

  async function submitOrder() {
    setBusy(true);
    try {
      const result = await api<{ message: string }>("/api/market/orders", {
        method: "POST", body: JSON.stringify({ assetCode: selectedCode, side, quantity: Number(quantity), limitPrice: Number(price) }),
      });
      setReview(false);
      setError(result.message);
      await Promise.all([refreshOrders(), refreshMarket(selectedCode, range, interval)]);
      const wallet = await api<{ portfolio: Portfolio }>("/api/me/portfolio");
      setPortfolio(wallet.portfolio);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "주문을 제출하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function cancelOrder(orderId: number) {
    setBusy(true);
    try {
      const result = await api<{ message: string }>(`/api/market/orders/${orderId}/cancel`, { method: "POST", body: "{}" });
      setError(result.message);
      await refreshOrders();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "주문을 취소하지 못했습니다.");
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
      const result = await api<{ htsId: string }>("/api/hts/account", {
        method: "POST", body: JSON.stringify({ htsId: htsIdInput, password: currentPassword }),
      });
      setHtsId(result.htsId);
      setHtsIdInput(result.htsId);
      setCurrentPassword("");
      setSettingsMessage("HTS ID를 저장했습니다.");
    } catch (cause) {
      const reason = cause instanceof Error ? cause.message : "";
      setSettingsError(reason === "invalid_credentials" ? "현재 비밀번호가 일치하지 않습니다." : reason || "HTS ID를 저장하지 못했습니다.");
    } finally {
      setSettingsBusy(false);
    }
  }

  function chooseInstrument(instrument: Instrument) {
    setSelectedCode(instrument.code);
    setPrice(String(instrument.price));
    setSearch("");
    setSearchOpen(false);
  }

  if (!user) return <div className="stage"><Backdrop /><AuthPanel app="hts" onAuthenticated={setUser} /></div>;

  const series = extent.all.filter((point) => point.time >= extent.minTime && point.time <= extent.maxTime);
  const path = series.map((point, index) => `${index ? "L" : "M"}${X(point.time).toFixed(2)},${Y(point.price).toFixed(2)}`).join(" ");
  const current = points.at(-1)?.price ?? selected?.price ?? 0;
  const pathRange = B - T;
  const showCandles = chartMode === "candle" && candles.length > 0;
  const candleWidth = Math.max(1, Math.min(24, (candleInterval / Math.max(1, extent.maxTime - extent.minTime)) * (R - L) * .7));
  const multiDay = extent.maxTime - extent.minTime > 2 * 86400;
  const axisTime = (time: number) => new Date(time * 1000).toLocaleString("ko-KR", multiDay ? { month: "numeric", day: "numeric" } : { hour: "2-digit", minute: "2-digit" });
  const homeCurrency = portfolio?.national_currency || "NWX";
  const homeDigits = portfolio?.home_decimal_places ?? 0;
  const homeRate = portfolio?.home_rate_nwx || 1;
  // Portfolio totals are NWX; the Discord wallet shows them in the home currency.
  const inHome = (nwx: number) => nwx / homeRate;
  const holdings = (portfolio?.stocks ?? []).filter((item) => item.qty);

  return <div className="hts-shell stage">
    <Backdrop />
    <header className="hts-topbar"><a className="brand" href="/">INAZINA <span>HTS</span></a><div className="top-center"><span className="connection"><i />API {health}</span><span>{user.nickname}</span></div><div className="top-actions"><button className="header-action" title="종목 검색" onClick={() => setSearchOpen(true)}>검색</button><button className="header-action account-action" onClick={() => { setSettingsMessage(""); setSettingsError(""); setAccountSettingsOpen(true); }}>계정 설정 <small>{htsId || "HTS ID 만들기"}</small></button><button className="icon-button" title="로그아웃" onClick={() => { clearToken(); void api("/api/auth/logout", { method: "POST" }); setUser(null); }}>로그아웃</button></div></header>
    {error && <div className="toast" role="status">{error}<button onClick={() => setError("")}>닫기</button></div>}
    <main className="hts-layout">
      <aside className="instrument-rail"><div className="rail-head"><strong>시장</strong><span>{instruments.length}</span></div><section className="wallet-summary"><span>사용자 지갑</span><strong>{money(portfolio?.cash ?? user.cash)} <small>{homeCurrency}</small></strong><p>총 평가 자산 {money(inHome(portfolio?.grand_total ?? 0), homeDigits)} {homeCurrency}</p></section><div className="instrument-list">{filtered.map((item) => <button key={item.code} className={selectedCode === item.code ? "instrument active" : "instrument"} onClick={() => chooseInstrument(item)}><span className="instrument-top"><b>{item.name}</b><em>{instrumentType(item)}</em></span><span className="instrument-bottom"><code>{item.code}</code><strong>{money(item.price)}</strong></span></button>)}</div></aside>
      <section className="main-market">
        <div className="quote-strip"><div><p className="eyebrow">{selected?.exchange_id || "MARKET"} · {selected?.type || "—"}</p><h1>{selected?.name || "종목을 선택하세요"}<small>{selected?.code}</small></h1></div><div className="quote-value"><strong>{money(current)}</strong><span>{selected?.quote_currency || "NWX"}</span></div></div>
        <section className="chart-panel">
          <div className="chart-head"><div className="segmented"><button title="이전 구간" onClick={() => setPan((value) => value + 1)}>←</button>{ranges.map((item) => <button key={item} className={range === item ? "selected" : ""} onClick={() => { setRange(item); setZoom(1); setPan(0); }}>{rangeNames[item]}</button>)}<button title="다음 구간" onClick={() => setPan((value) => value - 1)}>→</button><button title="확대" onClick={() => setZoom((value) => Math.min(32, value * 2))}>+</button><button title="축소" onClick={() => setZoom((value) => Math.max(1, value / 2))}>−</button><span className="divider" aria-hidden="true" /><button className={chartMode === "candle" ? "selected" : ""} title="캔들 차트" onClick={() => setChartMode("candle")}>캔들</button><button className={chartMode === "line" ? "selected" : ""} title="선 차트" onClick={() => setChartMode("line")}>선</button>{chartMode === "candle" && <select className="interval-select" aria-label="봉 주기" value={interval ?? ""} onChange={(event) => { setIntervalSeconds(event.target.value ? Number(event.target.value) : null); setZoom(1); setPan(0); }}><option value="">자동 ({intervals.find(([seconds]) => seconds === candleInterval)?.[1] ?? "봉"})</option>{intervals.map(([seconds, label]) => <option key={seconds} value={seconds}>{label}</option>)}</select>}</div><div className="drawing-tools"><button className={tool === "select" ? "selected" : ""} title="선 선택·이동" onClick={() => setTool("select")}>선택</button><button className={tool === "trend" ? "selected" : ""} title="드래그해 추세선" onClick={() => setTool("trend")}>추세선</button><button className={tool === "horizontal" ? "selected" : ""} title="가격 위치에 수평선" onClick={() => setTool("horizontal")}>수평선</button><button title="실행 취소" onClick={undo}>되돌리기</button><button title="선택 선 삭제" disabled={!selectedDrawing} onClick={() => { previousDrawings.current.push(drawings); setDrawings((items) => items.filter((item) => item.id !== selectedDrawing)); setSelectedDrawing(""); }}>삭제</button><button title="모든 선 삭제" onClick={() => { if (drawings.length && window.confirm("이 종목의 차트 선을 모두 삭제할까요?")) { previousDrawings.current.push(drawings); setDrawings([]); setSelectedDrawing(""); } }}>전체 삭제</button></div></div>
          <div className="chart-canvas"><svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" onPointerDown={chartDown} onPointerMove={chartMove} onPointerUp={chartUp} onPointerCancel={chartCancel} className={tool === "select" ? "chart-svg" : "chart-svg drawing-mode"} role="img" aria-label={`${selected?.name || "가격"} 차트`}>
            {Array.from({ length: 5 }, (_, index) => { const y = T + (pathRange / 4) * index; const value = extent.maxPrice - ((extent.maxPrice - extent.minPrice) / 4) * index; return <g key={index}><line x1={L} y1={y} x2={R} y2={y} className="grid-line"/><text x={R + 10} y={y + 4} className="axis-label">{money(value)}</text></g>; })}
            {Array.from({ length: 5 }, (_, index) => { const x = L + ((R - L) / 4) * index; const time = extent.minTime + ((extent.maxTime - extent.minTime) / 4) * index; return <g key={index}><line x1={x} y1={T} x2={x} y2={B} className="grid-line vertical"/><text x={x} y={H - 12} textAnchor="middle" className="axis-label">{axisTime(time)}</text></g>; })}
            {showCandles ? <g className="candles">{extent.visibleCandles.map((item) => {
              const x = X(item.time + candleInterval / 2);
              const top = Y(Math.max(item.open, item.close)), bottom = Y(Math.min(item.open, item.close));
              return <g key={item.time} className={item.close >= item.open ? "candle up" : "candle down"}><title>{`${new Date(item.time * 1000).toLocaleString("ko-KR")} 시 ${money(item.open)} 고 ${money(item.high)} 저 ${money(item.low)} 종 ${money(item.close)}`}</title><line x1={x} x2={x} y1={Y(item.high)} y2={Y(item.low)} /><rect x={x - candleWidth / 2} y={top} width={candleWidth} height={Math.max(1, bottom - top)} /></g>;
            })}</g> : <>
              <path d={path} className="price-path" />
              {series.length > 1 && <path d={`${path} L${X(series.at(-1)!.time)},${B} L${X(series[0].time)},${B} Z`} className="price-area" />}
            </>}
            {drawings.map((drawing) => <g key={drawing.id} className={drawing.id === selectedDrawing ? "drawing selected-drawing" : "drawing"} onPointerDown={(event) => beginMove(event, drawing)}>
              {drawing.type === "horizontal" ? <line x1={L} x2={R} y1={Y(drawing.anchors[0].price)} y2={Y(drawing.anchors[0].price)} /> : <line x1={X(drawing.anchors[0].time)} y1={Y(drawing.anchors[0].price)} x2={X(drawing.anchors[1].time)} y2={Y(drawing.anchors[1].price)} />}
            </g>)}
            {draft && <line className="drawing draft" x1={X(draft[0].time)} y1={Y(draft[0].price)} x2={X(draft[1].time)} y2={Y(draft[1].price)} />}
          </svg>{!points.length && !candles.length && <span className="chart-empty">이 구간에 저장된 가격 기록이 없습니다.</span>}</div>
        </section>
        <div className="lower-market">
          <section className="data-panel"><div className="panel-title"><h2>주문 · 체결</h2><span>최근 주문 및 미체결</span></div><div className="table-scroll"><table><thead><tr><th>종목</th><th>구분</th><th>가격</th><th>수량</th><th>잔량</th><th>상태</th><th></th></tr></thead><tbody>{orders.map((order) => <tr key={order.order_id}><td><b>{order.asset_code}</b><small>#{order.order_id}</small></td><td className={order.side === "buy" ? "buy-text" : "sell-text"}>{order.side === "buy" ? "매수" : "매도"}</td><td>{money(order.limit_price)}</td><td>{money(order.quantity)}</td><td>{money(order.remaining_quantity)}</td><td>{order.status}</td><td>{["open", "partial"].includes(order.status) && <button className="small-button" disabled={busy} onClick={() => void cancelOrder(order.order_id)}>취소</button>}</td></tr>)}</tbody></table>{!orders.length && <p className="empty">최근 주문이 없습니다.</p>}</div></section>
          <section className="data-panel portfolio-panel"><div className="panel-title"><h2>내 자산</h2><span>{user.nickname} · {homeCurrency} 기준</span></div><div className="asset-total"><small>총 평가 자산</small><strong>{money(inHome(portfolio?.grand_total || 0), homeDigits)} <em>{homeCurrency}</em></strong>{homeCurrency !== "NWX" && <small>≈ {money(portfolio?.grand_total || 0)} NWX</small>}</div>
            <div className="wallet-row"><span>현금</span><b>{money(portfolio?.cash || 0)} {homeCurrency}</b></div>
            {(portfolio?.wallets ?? []).filter((wallet) => wallet.is_foreign && wallet.balance_units).map((wallet) => <div className="wallet-row" key={wallet.currency_code}><span>외화 · {wallet.currency_code}</span><b>{money(wallet.balance_units / UNIT_SCALE, wallet.decimal_places ?? 6)} {wallet.currency_code}</b></div>)}
            <div className="wallet-row"><span>통장</span><b>{money(portfolio?.account_total || 0, homeDigits)} {homeCurrency}</b></div>
            <div className="wallet-row"><span>투자 평가액</span><b>{money(inHome(portfolio?.stock_total || 0), homeDigits)} {homeCurrency}</b></div>
            <div className="wallet-row"><span>매수 예치금</span><b>{money(inHome(portfolio?.order_reserved_total || 0), homeDigits)} {homeCurrency}</b></div>
            <div className="wallet-row"><span>대출</span><b>{money(inHome(portfolio?.loan_total || 0), homeDigits)} {homeCurrency}</b></div>
            <div className="wallet-row net-worth"><span>순자산</span><b>{money(inHome(portfolio?.net_worth || 0), homeDigits)} {homeCurrency}</b></div>
            <div className="holdings"><div className="holdings-head"><span>보유 종목</span><span>{holdings.length}</span></div>{holdings.map((item) => <button type="button" key={`${item.code}-${item.is_collateral ? "c" : "h"}`} className="holding" onClick={() => { const match = instruments.find((instrument) => instrument.code === item.code); if (match) chooseInstrument(match); }}><span><b>{item.name}</b><small>{item.code} · {money(item.qty, 6)}주{item.sell_reserved_qty ? ` (매도 대기 ${money(item.sell_reserved_qty, 6)})` : ""}</small></span><strong>{money(item.value)} <em>NWX</em></strong></button>)}{!holdings.length && <p className="empty">보유 종목이 없습니다.</p>}</div></section>
        </div>
      </section>
      <aside className="trade-rail">
        <section className="data-panel orderbook-panel"><div className="panel-title"><h2>호가</h2><span>수량 · 가격</span></div><div className="book-head"><span>수량</span><span>가격</span></div><div className="book-side asks">{[...book.asks].reverse().map((item, index) => <div className="book-row" key={`${item.limit_price}-${index}`}><span>{money(item.quantity)}</span><b>{money(item.limit_price)}</b></div>)}</div><div className="book-current"><span>현재가</span><strong>{money(current)}</strong></div><div className="book-side bids">{book.bids.map((item, index) => <div className="book-row" key={`${item.limit_price}-${index}`}><span>{money(item.quantity)}</span><b>{money(item.limit_price)}</b></div>)}</div></section>
        <section className="data-panel order-entry"><div className="panel-title"><h2>주문 입력</h2><span>지정가</span></div><div className="order-sides"><button className={side === "buy" ? "buy active" : "buy"} onClick={() => setSide("buy")}>매수</button><button className={side === "sell" ? "sell active" : "sell"} onClick={() => setSide("sell")}>매도</button></div><label>지정가 ({selected?.quote_currency || "NWX"})<input inputMode="numeric" value={price} onChange={(event) => setPrice(event.target.value.replace(/[^\d]/g, ""))} /></label><label>주문 수량<input inputMode="numeric" value={quantity} onChange={(event) => setQuantity(event.target.value.replace(/[^\d]/g, ""))} /></label><div className="form-total"><span>주문 금액</span><b>{money(Number(price || 0) * Number(quantity || 0))} {selected?.quote_currency || "NWX"}</b></div><button className={side === "buy" ? "submit buy-submit" : "submit sell-submit"} disabled={!selected || !Number(price) || !Number(quantity) || busy} onClick={() => setReview(true)}>{side === "buy" ? "매수 주문 검토" : "매도 주문 검토"}</button><p className="risk-note">검토 후 확정하면 기존 주문 검증을 거쳐 제출합니다.</p></section>
      </aside>
    </main>
    {review && <div className="modal-backdrop"><section className="review-modal" role="dialog" aria-modal="true" aria-labelledby="review-title"><p className="eyebrow">ORDER REVIEW</p><h2 id="review-title">주문을 확인하세요</h2><dl><div><dt>종목</dt><dd>{selected?.name} ({selectedCode})</dd></div><div><dt>유형</dt><dd className={side === "buy" ? "buy-text" : "sell-text"}>{side === "buy" ? "지정가 매수" : "지정가 매도"}</dd></div><div><dt>지정가</dt><dd>{money(Number(price))} {selected?.quote_currency || "NWX"}</dd></div><div><dt>수량</dt><dd>{money(Number(quantity))}</dd></div><div><dt>예상 금액</dt><dd>{money(Number(price) * Number(quantity))} {selected?.quote_currency || "NWX"}</dd></div></dl><div className="modal-actions"><button disabled={busy} onClick={() => setReview(false)}>돌아가기</button><button className={side === "buy" ? "buy-confirm" : "sell-confirm"} disabled={busy} onClick={() => void submitOrder()}>{busy ? "제출 중" : "주문 확정"}</button></div></section></div>}
    {searchOpen && <div className="modal-backdrop search-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setSearchOpen(false); }}><section className="search-modal" role="dialog" aria-modal="true" aria-labelledby="search-title"><div className="search-modal-head"><h2 id="search-title">종목 검색</h2><button type="button" title="닫기" onClick={() => setSearchOpen(false)}>닫기</button></div><label className="search-field"><span aria-hidden="true">⌕</span><input ref={searchInputRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="종목명 또는 코드" /></label><div className="search-results">{filtered.slice(0, 100).map((item) => <button type="button" key={item.code} className={selectedCode === item.code ? "search-result active" : "search-result"} onClick={() => chooseInstrument(item)}><span><b>{item.name}</b><small>{item.code} · {instrumentType(item)}</small></span><strong>{money(item.price)} {item.quote_currency || "NWX"}</strong></button>)}{!filtered.length && <p className="empty">검색 결과가 없습니다.</p>}</div></section></div>}
    {accountSettingsOpen && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setAccountSettingsOpen(false); }}><section className="account-modal" role="dialog" aria-modal="true" aria-labelledby="account-title"><div className="account-modal-head"><div><p className="eyebrow">HTS ACCOUNT</p><h2 id="account-title">HTS ID 설정</h2></div><button type="button" title="닫기" onClick={() => setAccountSettingsOpen(false)}>닫기</button></div><p className="account-description">Discord ID와 별도로 사용할 로그인 ID를 설정합니다.</p><form className="account-form" onSubmit={saveHtsId}><label>HTS ID<input autoComplete="username" minLength={3} maxLength={32} pattern="[A-Za-z0-9_.-]{3,32}" value={htsIdInput} onChange={(event) => setHtsIdInput(event.target.value)} placeholder="예: inazina_user" required /></label><p className="account-hint">영문, 숫자, 점, 밑줄, 하이픈으로 3~32자</p><label>현재 비밀번호<input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /></label>{settingsMessage && <p className="account-success" role="status">{settingsMessage}</p>}{settingsError && <p className="account-error" role="alert">{settingsError}</p>}<div className="modal-actions"><button type="button" disabled={settingsBusy} onClick={() => setAccountSettingsOpen(false)}>닫기</button><button className="account-save" disabled={settingsBusy || !htsIdInput}>{settingsBusy ? "저장 중…" : htsId ? "HTS ID 변경" : "HTS ID 설정"}</button></div></form></section></div>}
  </div>;
}

function Backdrop() {
  return <>
    <div className="wall" aria-hidden="true" />
    <div className="blob b1" aria-hidden="true" />
    <div className="blob b2" aria-hidden="true" />
  </>;
}
