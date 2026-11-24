import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, clearToken, onUnauthorized, signedOut, type ApiUser } from "../../shared/api";
import { moneyDivide, type Money } from "../../shared/money";
import { Logo } from "../../shared/Logo";
import { navigate } from "./nav";
import { ConsentGate } from "../../shared/ConsentGate";
import { SsoGate } from "../../shared/SsoGate";
import { logoutRedirect, currentReturnTo, ssoLoginUrl } from "../../shared/sso";
import { guestCanView, loginPanelRequested } from "./guest";
import "./guest.css";
import type { Tool } from "./PriceChart";
import type { Order } from "./types";
import { confirmDialog } from "../../shared/dialogs";
import { legacyCommandPath, servicePath } from "./services/paths";
import { SERVICES } from "./services/ServiceKit";
import { Backdrop } from "./Backdrop";
import { HeatmapPage } from "./HeatmapPage";
import { PortfolioPage } from "./PortfolioPage";
import { CreditPage } from "./CreditPage";
import { NotificationBell } from "./NotificationBell";
import { PayoutPage } from "./PayoutPage";
import { OrderReviewModal } from "./OrderReviewModal";
import { AccountPage } from "./AccountPage";
import { ChartPanel } from "./ChartPanel";
import { DrPanel } from "./DrPanel";
import { ExtrasPanels } from "./ExtrasPanels";
import { OrderBookPanel } from "./OrderBookPanel";
import { OrderEntry } from "./OrderEntry";
import { OrdersPanel } from "./OrdersPanel";
import { QuoteStrip } from "./QuoteStrip";
import { TopSearch } from "./TopSearch";
import { WalletTile } from "./WalletTile";
import { GUEST_USER, ORDER_TYPES, ROLE_LABEL, WATCH_KEY, USER_KEY, WarningBadge, boardUrl, cachedUser, errorText, money, type Lock, type Notice } from "./appShared";
import { useAccountData } from "./useAccountData";
import { useAccountSettings } from "./useAccountSettings";
import { useDrActions } from "./useDrActions";
import { useDrawings } from "./useDrawings";
import { useInstrumentSearch } from "./useInstrumentSearch";
import { useMarketData } from "./useMarketData";
import { useOrderSubmit } from "./useOrderSubmit";
import "./pages.css";
import "./notifications.css";

// Heavy, route-only screens load on first visit instead of with the trading screen.
const ServicesApp = lazy(() => import("./services/ServicesApp").then((m) => ({ default: m.ServicesApp })));
const OwnershipGraph = lazy(() => import("../../shared/OwnershipGraph").then((m) => ({ default: m.OwnershipGraph })));

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
  const [heatmapExchangeOverride, setHeatmapExchangeOverride] = useState<string | undefined>(() => new URLSearchParams(location.search).get("exchange") || undefined);
  const [chartMode, setChartMode] = useState<"candle" | "line">("candle");
  const [tool, setTool] = useState<Tool>("select");
  const [orderFilter, setOrderFilter] = useState<"open" | "all">("open");
  // 관심종목: a per-viewer convenience kept in this browser.
  const [watch, setWatch] = useState<string[]>(() => { try { const saved = JSON.parse(localStorage.getItem(WATCH_KEY) ?? "[]"); return Array.isArray(saved) ? saved : []; } catch { return []; } });
  const toggleWatch = (code: string) => setWatch((current) => {
    const next = current.includes(code) ? current.filter((item) => item !== code) : [...current, code];
    try { localStorage.setItem(WATCH_KEY, JSON.stringify(next)); } catch { /* not remembered */ }
    return next;
  });
  const pathname = location.pathname;
  const guest = !user;
  const me: ApiUser = user ?? GUEST_USER;

  const market = useMarketData({ reportPollError, lastPollError });
  const { instruments, selectedCode, setSelectedCode, selected, quote, reloadMarket, loadInstruments, pollQuote } = market;
  const account = useAccountData({ signedIn: Boolean(user), userRole: user?.role, reportPollError, lastPollError });
  const { portfolio, setPortfolio, orders, setOrders, credit, setCredit, refreshAccount, refreshAccountQuiet, refreshRates } = account;
  const homeCurrency = portfolio?.national_currency || me.currency || "";
  const searchState = useInstrumentSearch(instruments, homeCurrency);
  const settings = useAccountSettings({ user, pathname, setNotice, taxResidencyValue: account.taxResidencyValue, setTaxResidency: account.setTaxResidency });
  const [drawings, setDrawings] = useDrawings(selectedCode, user, setNotice);
  const dr = useDrActions({ user, selected, submitLock, setBusy, setNotice, notify, refreshAccount, refreshAccountQuiet, reloadMarket });
  const order = useOrderSubmit({
    selected, selectedCode, quote, pricedAt: market.pricedAt, book: market.book, submitLock, setBusy, setNotice, notify, pollQuote,
    afterOrder: () => Promise.all([refreshAccountQuiet(), reloadMarket(), loadInstruments()]),
    onPlaced: () => setOrderFilter("open"),
  });

  const defaultHeatmapExchange = useMemo(() => {
    const term = searchState.search.trim().toLocaleLowerCase();
    const exchanges = [...new Set(instruments.map((item) => item.exchange_id).filter((item): item is string => Boolean(item)))];
    return exchanges.find((item) => term && item.toLocaleLowerCase() === term) ?? selected?.exchange_id ?? exchanges[0];
  }, [instruments, searchState.search, selected?.exchange_id]);
  const heatmapExchange = heatmapExchangeOverride ?? defaultHeatmapExchange;

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
  // A review dialog still open when the session ends would offer an order nobody can place.
  useEffect(() => { if (!user) order.setReview(false); }, [user, order.setReview]);

  // A success notice clears itself after 4 seconds; an error stays until dismissed.
  useEffect(() => {
    if (notice?.kind !== "success") return;
    const timer = window.setTimeout(() => setNoticeState(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);



  async function cancelOrder(order: Order) {
    if (!await confirmDialog({ title: "주문 취소", danger: true, message: `주문 #${order.order_id} (${order.asset_code} ${order.side === "buy" ? "매수" : "매도"} 잔량 ${money(order.remaining_quantity, 3)})을 취소할까요?\n매수 예치금이나 매도 수량은 바로 돌려받습니다.` })) return;
    setBusy(true);
    try {
      const result = await api<{ message: string }>(`/api/market/orders/${order.order_id}/cancel`, { method: "POST", body: "{}" });
      notify(result.message);
      await Promise.all([refreshAccountQuiet(), reloadMarket()]);
    } catch (cause) {
      setNotice(errorText(cause, "주문을 취소하지 못했습니다."));
    } finally {
      setBusy(false);
    }
  }

  function chooseInstrument(code: string) {
    searchState.setSearchOpen(false);
    if (location.pathname !== "/") navigate(`/?code=${encodeURIComponent(code)}`);
    const instrument = instruments.find((item) => item.code === code);
    if (!instrument) { setNotice(`${code}은(는) 지금 거래 중인 종목이 아닙니다.`); return; }
    setSelectedCode(instrument.code);
    order.setPrice(String(instrument.price));
  }
  const switchInstrument = () => { searchState.setSearchOpen(true); searchState.inputRef.current?.focus(); window.scrollTo({ top: 0, behavior: "smooth" }); };

  if (!user && !checked) return <div className="stage"><Backdrop /><p className="page-loading" role="status">불러오는 중…</p></div>;
  // After the session expires the login screen carries the notice; "다시 로그인" dismisses it and focuses the form.
  const relogin = () => { setNoticeState(null); requireLogin(); };
  const toast = notice && <div className={`toast ${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}><span>{notice.text}</span>{notice.expired && <button type="button" onClick={relogin}>다시 로그인</button>}<button type="button" onClick={() => setNoticeState(null)}>닫기</button></div>;
  // Visitors without a session see the page; the sign-in gate opens only when asked for (register, reset, a sign-in error).
  if (!user && gateOpen) return <div className="stage"><Backdrop />{toast}<SsoGate autoRedirect={false} onBrowse={() => setGateOpen(false)} /></div>;
  // A login-only part of the page stays visible but blurred; any click or Enter on it asks the visitor to sign in.
  const lock: Lock = (label) => guest ? {
    "data-login-label": label,
    onClickCapture: (event: React.SyntheticEvent) => { event.preventDefault(); event.stopPropagation(); requireLogin(); },
    onKeyDownCapture: (event: React.KeyboardEvent) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); requireLogin(); } },
  } : {};
  const locked = guest ? " login-locked" : "";

  // Amounts follow the account's nationality; instrument prices keep their trading currency.
  const homeDigits = portfolio?.home_decimal_places ?? 0;
  const homeRate = portfolio?.home_rate_nwx || 1;
  // Portfolio totals are 기준 통화; the Discord wallet shows them in the home currency.
  const inHome = (nwx: Money) => moneyDivide(nwx, homeRate, homeDigits);
  const holdings = (portfolio?.stocks ?? []).filter((item) => item.qty);

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
  if (user?.consentRequired?.length) return <div className="stage"><Backdrop />{toast}<ConsentGate held={user.consentRequired} onLogout={() => void logout()} /></div>;
  const topbar = (here: "home" | "account" | "services") => <><header className="hts-topbar">
    <a className="brand" href="/" aria-label="나루 홈"><Logo className="brand-logo" /></a>
    <TopSearch state={searchState} selectedCode={selectedCode} homeCurrency={homeCurrency} onChoose={chooseInstrument} onExchangeSelect={setHeatmapExchangeOverride} />
    <nav className="top-nav" aria-label="서비스">{SERVICES.slice(0, 4).map((item) => <a key={item.path} href={item.path} aria-current={here === "services" && location.pathname.startsWith(item.path) ? "page" : undefined}>{item.label}</a>)}<a href={boardUrl()}>게시판</a></nav>
    <div className="top-actions">
      {here === "home" && <span className={`connection ${health === "연결됨" ? "ok" : health === "확인 중" ? "pending" : "bad"}`} role="img" aria-label={`API ${health}`} title={`API ${health}`}><i /></span>}
      {guest ? <button type="button" className="icon-button" onClick={requireLogin}>로그인</button> : <>
      <a className="header-action account-action" href="/account" aria-current={here === "account" ? "page" : undefined}>{me.nickname} <small>{settings.htsId || "아이디 만들기"}</small></a>
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
    <main><PortfolioPage holdings={holdings} instruments={instruments} homeCurrency={homeCurrency} cash={portfolio?.cash ?? me.cash} totalAssets={portfolio?.grand_total ?? portfolio?.net_worth ?? 0} onRefreshRates={() => void refreshRates()} ratesBusy={account.ratesBusy} onSelect={(code) => { window.location.href = `/?code=${encodeURIComponent(code)}`; }} /></main>
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
      {settings.ownershipLoading ? <div className="ownership-loading" role="status"><span className="ownership-spinner" aria-hidden="true" /><strong>보유 기업 지배구조를 불러오는 중…</strong></div> : <Suspense fallback={null}><OwnershipGraph graph={settings.ownershipGraph} fullScreen /></Suspense>}
    </main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;
  if (location.pathname === "/account" || location.pathname === "/account/") return <div className="hts-shell stage account-page-shell">
    <Backdrop />
    {topbar("account")}
    <AccountPage me={me} portfolio={portfolio} credit={credit} homeCurrency={homeCurrency} homeDigits={homeDigits} inHome={inHome} ratesBusy={account.ratesBusy} onRefreshRates={() => void refreshRates()} onLogout={() => void logout()} settings={settings} />
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
  </div>;

  return <div className="hts-shell stage">
    <Backdrop />
    {topbar("home")}
    <main className="hts-main">
      <QuoteStrip watch={watch} instruments={instruments} selected={selected} selectedCode={selectedCode} current={order.current} homeCurrency={homeCurrency} onToggleWatch={toggleWatch} onChoose={chooseInstrument} onSwitchInstrument={switchInstrument} />
      <div className="trade-row">
        <div className="chart-col">
          <ChartPanel instruments={instruments} selected={selected} selectedCode={selectedCode} points={market.points} candles={market.candles} candleInterval={market.candleInterval}
            chartMode={chartMode} onChartMode={setChartMode} range={market.range} onRange={market.setRange} interval={market.interval} onInterval={market.setIntervalSeconds}
            drawings={drawings} onDrawingsChange={setDrawings} tool={tool} onToolChange={setTool}
            indicators={market.indicators} indicatorData={market.indicatorData} indicatorCatalog={market.indicatorCatalog} onIndicatorsChange={market.chooseIndicators}
            onRefresh={() => { lastPollError.current.delete("account"); void refreshAccountQuiet(); }}
            heatmapExchange={heatmapExchange} onExchangeChange={setHeatmapExchangeOverride} onChoose={chooseInstrument} />
        </div>
        <div className="ticket-col">
          <OrderEntry lock={lock} locked={locked} selected={selected} homeCurrency={homeCurrency} orderType={order.orderType} onOrderType={order.setOrderType} side={order.side} onSide={order.setSide}
            price={order.price} onPrice={order.setPrice} quantity={order.quantity} onQuantity={order.setQuantity} orderPrice={order.orderPrice} marketEnough={order.marketEstimate.enough}
            orderAmount={order.orderAmount} orderReady={order.orderReady} busy={busy} onReview={() => order.setReview(true)} />
          {selected?.type === "depositary_receipt" && <DrPanel selected={selected} busy={busy} drQuantity={dr.drQuantity} onDrQuantity={dr.setDrQuantity} drVoteQuantity={dr.drVoteQuantity} onDrVoteQuantity={dr.setDrVoteQuantity}
            drProxyUserId={dr.drProxyUserId} onDrProxyUserId={dr.setDrProxyUserId} delegations={dr.drDelegations} onConvert={(action) => void dr.convertDr(action)} onDelegate={() => void dr.delegateDrVotes()} onRevoke={(item) => void dr.revokeDrVotes(item)} />}
        </div>
      </div>
      <div className="board-row">
        <OrderBookPanel book={market.book} current={order.current} onPickPrice={(picked) => { order.setOrderType("limit"); order.setPrice(picked); }} />
        <OrdersPanel lock={lock} locked={locked} orders={orders} orderFilter={orderFilter} onOrderFilter={setOrderFilter} busy={busy} onChoose={chooseInstrument} onCancel={(item) => void cancelOrder(item)} />
        <WalletTile lock={lock} locked={locked} me={me} portfolio={portfolio} credit={credit} homeCurrency={homeCurrency} inHome={inHome} ratesBusy={account.ratesBusy} onRefreshRates={() => void refreshRates()}
          taxResidency={account.taxResidency} taxResidencyValue={account.taxResidencyValue} onTaxResidencyValue={account.setTaxResidencyValue} onSaveTaxResidency={(event) => void settings.saveTaxResidency(event)} settings={settings} />
      </div>
      <ExtrasPanels portfolio={portfolio} loanSchedules={account.loanSchedules} drMovements={dr.drMovements} drCustodyInstructions={account.drCustodyInstructions} receivedDrDelegations={dr.receivedDrDelegations}
        drVotingAgenda={dr.drVotingAgenda} busy={busy} onCancelConversion={(item) => void dr.cancelDrConversion(item)} onVote={(item, choice) => void dr.castDrVote(item, choice)} />
    </main>
    <footer className="hts-footer">© INAZINA. All rights reserved.</footer>
    {order.review && selected && <OrderReviewModal side={order.side} kind={order.orderType} kindLabel={ORDER_TYPES.find(([id]) => id === order.orderType)![1]} priceLabel={order.orderType === "market" ? "예상 체결가" : order.orderType === "current" ? "현재가" : "지정가"}
      price={order.orderPrice || "0"} priceAt={order.orderType === "limit" ? null : order.orderType === "current" ? order.currentAt : quote?.at ?? null} currency={selected.quote_currency || homeCurrency}
      nameLine={<>{selected.name} ({selectedCode}) <WarningBadge warning={selected.warning} /></>} quantity={order.quantity} amount={order.orderAmount} busy={busy} format={money}
      onClose={() => order.setReview(false)} onConfirm={() => void order.submitOrder()} />}
  </div>;
}


/** Sends a visitor of a login-only page straight to the login (the identity provider, or the account panel without SSO). */
function LoginRedirect({ go }: { go: () => void }) {
  useEffect(() => { go(); }, [go]);
  return <p className="loading">로그인 화면으로 이동하는 중…</p>;
}