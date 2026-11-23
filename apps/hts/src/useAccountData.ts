import { useCallback, useState } from "react";
import type { MutableRefObject } from "react";
import { api, apiError } from "../../shared/api";
import type { CreditReport } from "./CreditPage";
import { usePolling } from "./usePolling";
import type { DrCustodyInstruction, LoanScheduleView, TaxResidency } from "./appShared";
import type { Order, Portfolio } from "./types";

type Options = {
  signedIn: boolean;
  userRole: string | undefined;
  reportPollError: (key: string, cause: unknown, fallback: string) => void;
  lastPollError: MutableRefObject<Map<string, string>>;
};

/** The signed-in account's wallet, orders, loans, DR conversions, credit report and tax residency; refreshed every 15s. */
export function useAccountData({ signedIn, userRole, reportPollError, lastPollError }: Options) {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [loanSchedules, setLoanSchedules] = useState<LoanScheduleView[]>([]);
  const [drCustodyInstructions, setDrCustodyInstructions] = useState<DrCustodyInstruction[]>([]);
  const [credit, setCredit] = useState<CreditReport | null>(null);
  const [taxResidency, setTaxResidency] = useState<TaxResidency | null>(null);
  const [taxResidencyValue, setTaxResidencyValue] = useState("");
  const [ratesBusy, setRatesBusy] = useState(false);

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

  /** Refreshes the account; auth errors end the session, other errors show once. Resolves either way. */
  const refreshAccountQuiet = useCallback(async () => {
    try { await refreshAccount(); lastPollError.current.delete("account"); } catch (cause) { reportPollError("account", cause, "계좌 정보를 불러오지 못했습니다."); }
  }, [refreshAccount, reportPollError, lastPollError]);
  // Figures in the citizen's own currency use the exchange rate the server read when the account was loaded.
  const refreshRates = useCallback(async () => {
    setRatesBusy(true);
    try { await refreshAccountQuiet(); } finally { setRatesBusy(false); }
  }, [refreshAccountQuiet]);
  usePolling(refreshAccountQuiet, 15000, signedIn);

  return {
    portfolio, setPortfolio, orders, setOrders, loanSchedules, drCustodyInstructions, credit, setCredit,
    taxResidency, setTaxResidency, taxResidencyValue, setTaxResidencyValue,
    ratesBusy, refreshAccount, refreshAccountQuiet, refreshRates,
  };
}
