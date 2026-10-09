import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, clearToken, getToken, signIn, type ApiUser } from "../../shared/api";

type Instrument = { code: string; name: string; type: string; price: number; status: string; exchange_id?: string; quote_currency?: string };
type Point = { time: number; price: number };
type Drawing = { id: string; type: "trend" | "horizontal"; anchors: Point[] };
type Portfolio = { cash: number; grand_total: number; net_worth: number; loan_total: number; order_reserved_total: number; stocks: { code: string; name: string; qty: number; price: number; value: number; type: string }[] };
type Order = { order_id: number; asset_code: string; side: string; limit_price: number; quantity: number; remaining_quantity: number; filled_quantity: number; status: string };
type Book = { bids: { limit_price: number; quantity: number; order_count: number }[]; asks: { limit_price: number; quantity: number; order_count: number }[] };

const W = 1000, H = 420, L = 62, R = 925, T = 24, B = 374;
const ranges = ["1h", "1d", "1w", "1m", "all"] as const;
const rangeNames: Record<string, string> = { "1h": "1시간", "1d": "1일", "1w": "1주", "1m": "1개월", all: "전체" };
const money = (value: number) => new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(value || 0);

export default function App() {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [health, setHealth] = useState("확인 중");
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [search, setSearch] = useState("");
  const [range, setRange] = useState<string>("1d");
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
  const [drawingsReady, setDrawingsReady] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);
  const draftStart = useRef<Point | null>(null);
  const moveStart = useRef<{ point: Point; drawing: Drawing } | null>(null);
  const previousDrawings = useRef<Drawing[][]>([]);

  const selected = instruments.find((item) => item.code === selectedCode) ?? null;
  const filtered = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    return instruments.filter((item) => !term || `${item.code} ${item.name} ${item.type}`.toLocaleLowerCase().includes(term));
  }, [instruments, search]);
  const extent = useMemo(() => {
    const all = points.length ? points : selected ? [{ time: Date.now() / 1000, price: selected.price }] : [];
    const prices = all.map((item) => item.price).filter((value) => Number.isFinite(value) && value > 0);
    let minTime = all[0]?.time ?? 0, maxTime = all.at(-1)?.time ?? 1;
    let minPrice = Math.min(...prices), maxPrice = Math.max(...prices);
    if (minTime === maxTime) { minTime -= 1800; maxTime += 1800; }
    if (minPrice === maxPrice) { minPrice *= .99; maxPrice *= 1.01; }
    const gap = (maxPrice - minPrice) * .08;
    return { minTime, maxTime, minPrice: Math.max(0, minPrice - gap), maxPrice: maxPrice + gap, all };
  }, [points, selected]);
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
    if (getToken()) void authenticate();
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

  const refreshMarket = useCallback(async (code: string, timeframe: string) => {
    if (!code) return;
    try {
      const [chart, orderBook] = await Promise.all([
        api<{ points: Point[] }>(`/api/market/chart/${encodeURIComponent(code)}?range=${timeframe}`),
        api<Book>(`/api/market/book/${encodeURIComponent(code)}`),
      ]);
      setPoints(chart.points);
      setBook(orderBook);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "시세를 불러오지 못했습니다.");
    }
  }, []);

  useEffect(() => { void refreshMarket(selectedCode, range); }, [selectedCode, range, refreshMarket]);

  useEffect(() => {
    if (!user || !selectedCode) return;
    let cancelled = false;
    setDrawingsReady(false);
    setDrawings([]);
    api<{ drawings: Drawing[] }>(`/api/market/drawings/${encodeURIComponent(selectedCode)}`)
      .then((result) => { if (!cancelled) { setDrawings(result.drawings); previousDrawings.current = []; setDrawingsReady(true); } })
      .catch((cause) => { if (!cancelled) { setError(cause instanceof Error ? cause.message : "차트 도구를 불러오지 못했습니다."); setDrawingsReady(true); } });
    return () => { cancelled = true; };
  }, [user, selectedCode]);

  useEffect(() => {
    if (!drawingsReady || !selectedCode || !user) return;
    const timeout = window.setTimeout(() => {
      void api(`/api/market/drawings/${encodeURIComponent(selectedCode)}`, {
        method: "POST", body: JSON.stringify({ drawings }),
      }).catch((cause) => setError(cause instanceof Error ? cause.message : "차트 선을 저장하지 못했습니다."));
    }, 350);
    return () => window.clearTimeout(timeout);
  }, [drawings, drawingsReady, selectedCode, user]);

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

  function position(event: React.PointerEvent<SVGSVGElement>): Point {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * W;
    const y = ((event.clientY - rect.top) / rect.height) * H;
    return {
      time: Math.round(extent.minTime + ((x - L) / (R - L)) * (extent.maxTime - extent.minTime)),
      price: Math.max(1, extent.maxPrice - ((y - T) / (B - T)) * (extent.maxPrice - extent.minPrice)),
    };
  }

  function chartDown(event: React.PointerEvent<SVGSVGElement>) {
    if (tool === "select") { setSelectedDrawing(""); return; }
    const anchor = position(event);
    if (tool === "horizontal") {
      previousDrawings.current.push(drawings);
      setDrawings((current) => [...current, { id: crypto.randomUUID(), type: "horizontal", anchors: [anchor] }]);
      setTool("select");
    } else {
      draftStart.current = anchor;
      setDraft([anchor, anchor]);
      event.currentTarget.setPointerCapture(event.pointerId);
    }
  }

  function chartMove(event: React.PointerEvent<SVGSVGElement>) {
    const anchor = position(event);
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
    if (draftStart.current && draft) {
      const end = position(event);
      if (Math.abs(end.time - draftStart.current.time) > 1 || Math.abs(end.price - draftStart.current.price) > .001) {
        previousDrawings.current.push(drawings);
        setDrawings((current) => [...current, { id: crypto.randomUUID(), type: "trend", anchors: [draftStart.current!, end] }]);
      }
    }
    draftStart.current = null;
    moveStart.current = null;
    setDraft(null);
  }

  function beginMove(event: React.PointerEvent<SVGSVGElement>, drawing: Drawing) {
    event.stopPropagation();
    setSelectedDrawing(drawing.id);
    if (tool === "select") {
      previousDrawings.current.push(drawings);
      moveStart.current = { point: position(event), drawing };
      event.currentTarget.setPointerCapture(event.pointerId);
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
      await Promise.all([refreshOrders(), refreshMarket(selectedCode, range)]);
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

  async function login(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try { setUser(await signIn(token)); setToken(""); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "로그인에 실패했습니다."); }
    finally { setBusy(false); }
  }

  if (!user) return <main className="login-shell"><form className="login-form" onSubmit={login}><p className="eyebrow">INAZINA · LOCAL HTS</p><h1>HTS 로그인</h1><label htmlFor="token">접속 토큰</label><input id="token" type="password" autoComplete="current-password" value={token} onChange={(event) => setToken(event.target.value)} required /><button className="primary" disabled={busy}>{busy ? "확인 중" : "로그인"}</button>{error && <p className="error">{error}</p>}</form></main>;

  const series = extent.all;
  const path = series.map((point, index) => `${index ? "L" : "M"}${X(point.time).toFixed(2)},${Y(point.price).toFixed(2)}`).join(" ");
  const current = points.at(-1)?.price ?? selected?.price ?? 0;
  const pathRange = B - T;

  return <div className="hts-shell">
    <header className="hts-topbar"><a className="brand" href="/">INAZINA <span>HTS</span></a><div className="top-center"><span className="connection"><i />API {health}</span><span>{user.nickname}</span></div><button className="icon-button" title="로그아웃" onClick={() => { clearToken(); setUser(null); }}>나가기</button></header>
    {error && <div className="toast" role="status">{error}<button onClick={() => setError("")}>닫기</button></div>}
    <main className="hts-layout">
      <aside className="instrument-rail"><div className="rail-head"><strong>시장</strong><span>{instruments.length}</span></div><label className="search-wrap"><span>검색</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="종목명 · 코드" /></label><div className="instrument-list">{filtered.map((item) => <button key={item.code} className={selectedCode === item.code ? "instrument active" : "instrument"} onClick={() => { setSelectedCode(item.code); setPrice(String(item.price)); }}><span className="instrument-top"><b>{item.name}</b><em>{item.type}</em></span><span className="instrument-bottom"><code>{item.code}</code><strong>{money(item.price)}</strong></span></button>)}</div></aside>
      <section className="main-market">
        <div className="quote-strip"><div><p className="eyebrow">{selected?.exchange_id || "MARKET"} · {selected?.type || "—"}</p><h1>{selected?.name || "종목을 선택하세요"}<small>{selected?.code}</small></h1></div><div className="quote-value"><strong>{money(current)}</strong><span>{selected?.quote_currency || "NWX"}</span></div></div>
        <section className="chart-panel">
          <div className="chart-head"><div className="segmented">{ranges.map((item) => <button key={item} className={range === item ? "selected" : ""} onClick={() => setRange(item)}>{rangeNames[item]}</button>)}</div><div className="drawing-tools"><button className={tool === "select" ? "selected" : ""} title="선 선택·이동" onClick={() => setTool("select")}>선택</button><button className={tool === "trend" ? "selected" : ""} title="드래그해 추세선" onClick={() => setTool("trend")}>추세선</button><button className={tool === "horizontal" ? "selected" : ""} title="가격 위치에 수평선" onClick={() => setTool("horizontal")}>수평선</button><button title="실행 취소" onClick={undo}>되돌리기</button><button title="선택 선 삭제" disabled={!selectedDrawing} onClick={() => { previousDrawings.current.push(drawings); setDrawings((items) => items.filter((item) => item.id !== selectedDrawing)); setSelectedDrawing(""); }}>삭제</button><button title="모든 선 삭제" onClick={() => { if (drawings.length && window.confirm("이 종목의 차트 선을 모두 삭제할까요?")) { previousDrawings.current.push(drawings); setDrawings([]); setSelectedDrawing(""); } }}>전체 삭제</button></div></div>
          <div className="chart-canvas"><svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" onPointerDown={chartDown} onPointerMove={chartMove} onPointerUp={chartUp} className={tool === "select" ? "chart-svg" : "chart-svg drawing-mode"} role="img" aria-label={`${selected?.name || "가격"} 차트`}>
            {Array.from({ length: 5 }, (_, index) => { const y = T + (pathRange / 4) * index; const value = extent.maxPrice - ((extent.maxPrice - extent.minPrice) / 4) * index; return <g key={index}><line x1={L} y1={y} x2={R} y2={y} className="grid-line"/><text x={R + 10} y={y + 4} className="axis-label">{money(value)}</text></g>; })}
            {Array.from({ length: 5 }, (_, index) => { const x = L + ((R - L) / 4) * index; const time = extent.minTime + ((extent.maxTime - extent.minTime) / 4) * index; return <g key={index}><line x1={x} y1={T} x2={x} y2={B} className="grid-line vertical"/><text x={x} y={H - 12} textAnchor="middle" className="axis-label">{new Date(time * 1000).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })}</text></g>; })}
            <path d={path} className="price-path" />
            {series.length > 1 && <path d={`${path} L${X(series.at(-1)!.time)},${B} L${X(series[0].time)},${B} Z`} className="price-area" />}
            {drawings.map((drawing) => <g key={drawing.id} className={drawing.id === selectedDrawing ? "drawing selected-drawing" : "drawing"} onPointerDown={(event) => beginMove(event, drawing)}>
              {drawing.type === "horizontal" ? <line x1={L} x2={R} y1={Y(drawing.anchors[0].price)} y2={Y(drawing.anchors[0].price)} /> : <line x1={X(drawing.anchors[0].time)} y1={Y(drawing.anchors[0].price)} x2={X(drawing.anchors[1].time)} y2={Y(drawing.anchors[1].price)} />}
            </g>)}
            {draft && <line className="drawing draft" x1={X(draft[0].time)} y1={Y(draft[0].price)} x2={X(draft[1].time)} y2={Y(draft[1].price)} />}
            <rect x={L} y={T} width={R - L} height={B - T} className="chart-hit-area" />
          </svg>{!points.length && <span className="chart-empty">이 구간에 저장된 가격 기록이 없습니다.</span>}</div>
        </section>
        <div className="lower-market">
          <section className="data-panel"><div className="panel-title"><h2>주문 · 체결</h2><span>최근 주문 및 미체결</span></div><div className="table-scroll"><table><thead><tr><th>종목</th><th>구분</th><th>가격</th><th>수량</th><th>잔량</th><th>상태</th><th></th></tr></thead><tbody>{orders.map((order) => <tr key={order.order_id}><td><b>{order.asset_code}</b><small>#{order.order_id}</small></td><td className={order.side === "buy" ? "buy-text" : "sell-text"}>{order.side === "buy" ? "매수" : "매도"}</td><td>{money(order.limit_price)}</td><td>{money(order.quantity)}</td><td>{money(order.remaining_quantity)}</td><td>{order.status}</td><td>{["open", "partial"].includes(order.status) && <button className="small-button" disabled={busy} onClick={() => void cancelOrder(order.order_id)}>취소</button>}</td></tr>)}</tbody></table>{!orders.length && <p className="empty">최근 주문이 없습니다.</p>}</div></section>
          <section className="data-panel portfolio-panel"><div className="panel-title"><h2>내 자산</h2><span>{user.nickname}</span></div><div className="asset-total"><small>총 평가 자산</small><strong>{money(portfolio?.grand_total || 0)} <em>NWX</em></strong></div><div className="wallet-row"><span>현금</span><b>{money(portfolio?.cash || 0)} NWX</b></div><div className="wallet-row"><span>매수 예치금</span><b>{money(portfolio?.order_reserved_total || 0)} NWX</b></div><div className="wallet-row"><span>대출</span><b>{money(portfolio?.loan_total || 0)} NWX</b></div><div className="wallet-row net-worth"><span>순자산</span><b>{money(portfolio?.net_worth || 0)} NWX</b></div></section>
        </div>
      </section>
      <aside className="trade-rail">
        <section className="data-panel orderbook-panel"><div className="panel-title"><h2>호가</h2><span>수량 · 가격</span></div><div className="book-head"><span>수량</span><span>가격</span></div><div className="book-side asks">{[...book.asks].reverse().map((item, index) => <div className="book-row" key={`${item.limit_price}-${index}`}><span>{money(item.quantity)}</span><b>{money(item.limit_price)}</b></div>)}</div><div className="book-current"><span>현재가</span><strong>{money(current)}</strong></div><div className="book-side bids">{book.bids.map((item, index) => <div className="book-row" key={`${item.limit_price}-${index}`}><span>{money(item.quantity)}</span><b>{money(item.limit_price)}</b></div>)}</div></section>
        <section className="data-panel order-entry"><div className="panel-title"><h2>주문 입력</h2><span>지정가</span></div><div className="order-sides"><button className={side === "buy" ? "buy active" : "buy"} onClick={() => setSide("buy")}>매수</button><button className={side === "sell" ? "sell active" : "sell"} onClick={() => setSide("sell")}>매도</button></div><label>지정가 ({selected?.quote_currency || "NWX"})<input inputMode="numeric" value={price} onChange={(event) => setPrice(event.target.value.replace(/[^\d]/g, ""))} /></label><label>주문 수량<input inputMode="numeric" value={quantity} onChange={(event) => setQuantity(event.target.value.replace(/[^\d]/g, ""))} /></label><div className="form-total"><span>주문 금액</span><b>{money(Number(price || 0) * Number(quantity || 0))} {selected?.quote_currency || "NWX"}</b></div><button className={side === "buy" ? "submit buy-submit" : "submit sell-submit"} disabled={!selected || !Number(price) || !Number(quantity) || busy} onClick={() => setReview(true)}>{side === "buy" ? "매수 주문 검토" : "매도 주문 검토"}</button><p className="risk-note">검토 후 확정하면 기존 주문 검증을 거쳐 제출합니다.</p></section>
      </aside>
    </main>
    {review && <div className="modal-backdrop"><section className="review-modal" role="dialog" aria-modal="true" aria-labelledby="review-title"><p className="eyebrow">ORDER REVIEW</p><h2 id="review-title">주문을 확인하세요</h2><dl><div><dt>종목</dt><dd>{selected?.name} ({selectedCode})</dd></div><div><dt>유형</dt><dd className={side === "buy" ? "buy-text" : "sell-text"}>{side === "buy" ? "지정가 매수" : "지정가 매도"}</dd></div><div><dt>지정가</dt><dd>{money(Number(price))} {selected?.quote_currency || "NWX"}</dd></div><div><dt>수량</dt><dd>{money(Number(quantity))}</dd></div><div><dt>예상 금액</dt><dd>{money(Number(price) * Number(quantity))} {selected?.quote_currency || "NWX"}</dd></div></dl><div className="modal-actions"><button disabled={busy} onClick={() => setReview(false)}>돌아가기</button><button className={side === "buy" ? "buy-confirm" : "sell-confirm"} disabled={busy} onClick={() => void submitOrder()}>{busy ? "제출 중" : "주문 확정"}</button></div></section></div>}
  </div>;
}
