import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../../shared/api";
import { SsoGate } from "../../shared/SsoGate";
import { errorText, type Ctx } from "./common";
import type { Audit, Event, Flow, Instrument, Overview } from "./appTypes";
import { CommandsTab } from "./CommandsTab";
import { EventsSection, useEventForm } from "./EventsSection";
import { JumpDialog, type JumpTarget } from "./JumpDialog";
import { Footer } from "./Footer";
import { HomePage } from "./HomePage";
import * as Lazy from "./lazyPages";
import { NoticeBar, useNotice } from "./useNotice";
import { OverviewSection } from "./OverviewSection";
import { PageHead } from "./PageHead";
import { pages, readRoute, subtitles, type Page } from "./pages";
import { TopBar } from "./TopBar";
import { UserShell } from "./UserShell";
import { categoryOf, useCommandTab } from "./useCommandTab";
import { useBadges } from "./useBadges";
import { useSession } from "./useSession";

const SEARCHABLE: Page[] = ["instruments", "finance", "exchanges", "companies", "users", "court", "feedback", "board", "database", "accounting"];
const NAV_PAGES = pages.filter((item) => item.id !== "account" && item.id !== "ownership");
const initialRoute = readRoute();

export default function App() {
  const { notice, say, clear, markPageChange, dropStale } = useNotice();
  const [page, setPage] = useState<Page>(initialRoute.page);
  const [connection, setConnection] = useState<"checking" | "ok" | "down">("checking");
  const [busy, setBusy] = useState(false);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [flows, setFlows] = useState<Flow[]>([]);
  const [query, setQuery] = useState("");
  const [jumpOpen, setJumpOpen] = useState(false);
  const tab = useCommandTab(initialRoute);

  // Every page change goes through here, so a notice raised in the same handler (after the change) survives it.
  const go = useCallback((next: Page) => { markPageChange(); setPage(next); }, [markPageChange]);
  const onPage = (next: string) => go(next as Page);

  const resetData = useCallback(() => {
    setOverview(null); setInstruments([]); setAudit([]); setEvents([]); setFlows([]);
    tab.reset();
    setQuery(""); clear(); markPageChange(); setPage("home");
  }, [tab.reset, clear, markPageChange]); // eslint-disable-line react-hooks/exhaustive-deps
  const { user, checked, sessionError, retry, logout } = useSession({ say, reset: () => { resetData(); badges.reset(); }, go });
  const badges = useBadges(user, page, say);

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
      if (page === "commands") await tab.load();
    } catch (cause) {
      say(errorText(cause, "요청을 처리하지 못했습니다."), true);
    }
  }, [page, user, tab.load, say]);

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

  const openCommand = useCallback((name: string, args: string, national = false) => { go("commands"); tab.openCommand(name, args, national); }, [go, tab.openCommand]);
  const ctx: Ctx = useMemo(() => ({ busy, run, say, query, openCommand }), [busy, run, say, query, openCommand]);
  const eventForm = useEventForm(ctx, instruments);

  // ⌘K / Ctrl+K: jump to any page or command category by typing.
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setJumpOpen((open) => !open); }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  useEffect(() => { if (user && page !== "ownership") void refresh(); }, [user, page, refresh]);
  // A page change clears the search box and any notice raised before it; notices raised after it stay.
  useEffect(() => { setQuery(""); dropStale(); }, [page, dropStale]);

  // Keep the address in step with the page; back/forward restore it.
  useEffect(() => {
    const hash = `#${page}${page === "commands" && tab.section ? `/${encodeURIComponent(tab.section)}` : ""}`;
    if (page === "ownership") return;
    if (decodeURIComponent(location.hash) !== decodeURIComponent(hash)) history.pushState(null, "", hash);
  }, [page, tab.section]);
  useEffect(() => {
    const restore = () => {
      const route = readRoute();
      go(route.page);
      if (route.page === "commands") tab.restoreRoute(route);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [go, tab.restoreRoute]);

  const noticeBar = <NoticeBar notice={notice} onClose={clear} />;
  if (!checked) return <Stage><main className="center-shell">
    {sessionError
      ? <div className="notice error" role="alert"><span>{sessionError} 서버가 켜져 있는지 확인한 뒤 다시 시도해주세요.</span><button type="button" className="soft" onClick={retry}>다시 시도</button></div>
      : <p className="loading">불러오는 중…</p>}
  </main></Stage>;
  if (!user) return <Stage>{notice && <div className="center-shell">{noticeBar}</div>}<SsoGate autoRedirect /></Stage>;
  if (page === "ownership" && user.role === "admin") return <Stage><Suspense fallback={<main className="center-shell"><p className="loading">조직도를 여는 중…</p></main>}><Lazy.OrganizationOwnershipWindow route={initialRoute.sub} /></Suspense></Stage>;
  if (user.role !== "admin") return <Stage><UserShell user={user} ctx={ctx} notice={noticeBar} onLogout={logout} /></Stage>;

  const labels = new Map<string, string>(NAV_PAGES.map((item) => [item.id, item.label]));
  const jumpTargets: JumpTarget[] = !jumpOpen ? [] : [
    ...pages.map((item) => ({ key: item.id, label: item.label, hint: subtitles[item.id], go: () => go(item.id) })),
    ...tab.categories.map((category) => ({ key: `commands/${category}`, label: `관리자 명령어 · ${category}`, hint: `${tab.commands.filter((item) => categoryOf(item.name) === category).length}개 명령어`, go: () => { go("commands"); tab.openCategory(category); } })),
    ...tab.commands.map((item) => ({ key: `$${item.name}`, label: `$${item.name}`, hint: item.usage || categoryOf(item.name), go: () => openCommand(item.name, "") })),
  ];
  const changed = () => void badges.reload();

  return <Stage>
    <div className="shell">
      <TopBar user={user} page={page} pages={NAV_PAGES} badges={badges.badges} connection={connection} onPage={onPage} onLogout={logout} />
      <main className="content">
        <PageHead page={page} title={pages.find((item) => item.id === page)!.label} labels={labels} badges={badges.badges} searchable={SEARCHABLE.includes(page)} query={query}
          placeholder={page === "users" ? "이름 또는 ID" : page === "database" ? "모든 열에서 검색" : "이름 또는 코드"} onQuery={setQuery} onPage={onPage} />
        {noticeBar}
        <Suspense fallback={<p className="loading">화면을 불러오는 중…</p>}>
          {page === "home" && <HomePage market={overview?.market} bankDeficits={overview?.bankDeficits ?? []} labels={labels} badges={badges.badges} onPage={onPage} />}
          {page === "overview" && <OverviewSection ctx={ctx} overview={overview} audit={audit} />}
          {page === "instruments" && <><Lazy.ProductsPage group="market" ctx={ctx} /><Lazy.WarningsCard ctx={ctx} /></>}
          {page === "finance" && <Lazy.ProductsPage group="finance" ctx={ctx} />}
          {page === "exchanges" && <Lazy.ExchangesPage ctx={ctx} />}
          {page === "companies" && <Lazy.CompaniesPage ctx={ctx} />}
          {page === "organizations" && <Lazy.OrganizationPage ctx={ctx} />}
          {page === "board" && <Lazy.BoardAdminPage ctx={ctx} />}
          {page === "documents" && <Lazy.DocumentsPage ctx={ctx} />}
          {page === "credit" && <Lazy.CompliancePage ctx={ctx} />}
          {page === "accounting" && <Lazy.BanksPage ctx={ctx} />}
          {page === "court" && <Lazy.CourtPage ctx={ctx} />}
          {page === "feedback" && <Lazy.FeedbackPage ctx={ctx} onChange={changed} />}
          {page === "naturalization" && <Lazy.NaturalizationPage ctx={ctx} onChange={changed} />}
          {page === "liquidity" && <Lazy.LiquidityPage ctx={ctx} />}
          {page === "close" && <Lazy.AccountingClosePage ctx={ctx} />}
          {page === "alm" && <Lazy.AlmPage ctx={ctx} />}
          {page === "rates" && <Lazy.RatesPage ctx={ctx} />}
          {page === "users" && <Lazy.UsersPage ctx={ctx} currentUserId={user.userId} />}
          {page === "events" && <EventsSection ctx={ctx} form={eventForm} events={events} flows={flows} instruments={instruments} onChanged={() => void refresh()} />}
          {page === "commands" && <CommandsTab ctx={ctx} tab={tab} openProducts={(group) => go(group === "market" ? "instruments" : "finance")} onChanged={() => void refresh()} />}
          {page === "database" && <Lazy.DatabasePage ctx={ctx} />}
          {page === "security" && <Lazy.SecurityPage ctx={ctx} />}
          {page === "ops" && <Lazy.OpsPage ctx={ctx} />}
          {page === "controls" && <Lazy.ControlsPage ctx={ctx} />}
          {page === "account" && <Lazy.AccountPage ctx={ctx} user={user} onLogout={logout} />}
        </Suspense>
        <Footer connected={connection === "ok"} />
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
