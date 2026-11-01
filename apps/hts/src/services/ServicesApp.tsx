import type { Money } from "../../../shared/money";
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
import type { ServicePath } from "./paths";

const PAGES = { "/transfer": TransferPage, "/bank": BankPage, "/invest": InvestPage, "/casino": CasinoPage, "/history": HistoryPage, "/market": MarketInfoPage, "/financials": FinancialsPage, "/info": InfoPage };
/** The HTS's banking, product and game pages, one URL each, sharing one command session. */
export function ServicesApp({ path, instruments, portfolio, orders, homeCurrency, homeDigits, cash, creditScore, accounts, onDone }: {
  path: ServicePath; instruments: Instrument[]; portfolio: Portfolio | null; orders: Order[]; homeCurrency: string; homeDigits: number;
  cash: Money; creditScore: number; accounts: Account[]; onDone: () => void;
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
