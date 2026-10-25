import { useCommands, type Instrument, type Order, type Portfolio } from "../commandKit";
import { BankPage } from "./BankPage";
import { CasinoPage } from "./CasinoPage";
import { InfoPage } from "./InfoPage";
import { FinancialsPage } from "./FinancialsPage";
import { HistoryPage } from "./HistoryPage";
import { MarketInfoPage } from "./MarketInfoPage";
import { InvestPage } from "./InvestPage";
import { SERVICES, ServiceNav, type Account } from "./ServiceKit";
import { TransferPage } from "./TransferPage";

const PAGES = { "/transfer": TransferPage, "/bank": BankPage, "/invest": InvestPage, "/casino": CasinoPage, "/history": HistoryPage, "/market": MarketInfoPage, "/financials": FinancialsPage, "/info": InfoPage };
export const servicePath = (path: string) => (Object.keys(PAGES) as (keyof typeof PAGES)[]).find((key) => path === key || path === `${key}/`);

// Old /commands/<name> links land on the page that now offers that feature.
const LEGACY: Record<string, string> = { 송금: "/transfer", 도박: "/casino", 복권구매: "/casino", 내복권: "/casino", 복권결과: "/casino", 복권규칙: "/casino",
  계좌개설: "/bank", 계좌해지: "/bank", 대출신청: "/bank", 대출상환: "/bank", 대출연장: "/bank", 법정최고이자: "/info", 재무: "/financials", 파산: "/info", 주총소집: "/info" };
export const legacyCommandPath = (name: string) => LEGACY[name] ?? (["매수", "매도", "주문취소", "내주문", "호가"].includes(name) ? "/" : "/invest");

/** The HTS's banking, product and game pages, one URL each, sharing one command session. */
export function ServicesApp({ path, instruments, portfolio, orders, homeCurrency, homeDigits, cash, creditScore, accounts, onDone }: {
  path: keyof typeof PAGES; instruments: Instrument[]; portfolio: Portfolio | null; orders: Order[]; homeCurrency: string; homeDigits: number;
  cash: number; creditScore: number; accounts: Account[]; onDone: () => void;
}) {
  const kit = useCommands({ instruments, portfolio, orders, homeCurrency, onDone });
  const Page = PAGES[path];
  const service = SERVICES.find((item) => item.path === path)!;
  return <main className={`svc-page svc-${path.slice(1)}-page`}>
    <ServiceNav here={path} />
    <header className="svc-head"><h1>{service.label}</h1><p>{service.description}</p></header>
    {kit.error && <p className="account-error" role="alert">{kit.error}</p>}
    {!kit.loaded ? <p className="svc-empty">불러오는 중…</p> : <Page kit={kit} cash={cash} homeCurrency={homeCurrency} homeDigits={homeDigits} creditScore={creditScore} accounts={accounts} orders={orders} />}
  </main>;
}
