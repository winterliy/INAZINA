import { moneyAdd, type Money } from "../../shared/money";

/** The server's totals are in 기준 통화 (NWX), and cash, home-currency accounts, foreign-currency accounts and loans each lose the part
 *  below one whole NWX there. Converting such a total back to the home currency therefore shows "현금 1,000,000" next to "총자산 999,500".
 *  The server also sends those NWX parts and their exact home values (grand_floor_nwx / grand_exact_home, loan_floor_nwx / loan_exact_home):
 *  take the NWX parts out, convert the rest, add the exact values. */
type Parts = {
  cash: Money;
  cash_value_nwx?: Money;
  grand_floor_nwx?: Money;
  grand_exact_home?: Money;
  loan_floor_nwx?: Money;
  loan_exact_home?: Money;
};
type Convert = (nwx: Money) => Money;
const has = (value: Money | undefined): value is Money => value !== undefined && value !== null;

/** The total assets in the home currency. */
export function totalInHome(total: Money, portfolio: Parts | null | undefined, inHome: Convert): Money {
  if (!portfolio) return inHome(total);
  if (has(portfolio.grand_floor_nwx) && has(portfolio.grand_exact_home)) return moneyAdd(inHome(moneyAdd(total, portfolio.grand_floor_nwx, true)), portfolio.grand_exact_home);
  if (has(portfolio.cash_value_nwx)) return moneyAdd(inHome(moneyAdd(total, portfolio.cash_value_nwx, true)), portfolio.cash); // an older server: only the cash part
  return inHome(total);
}

/** The net worth (total assets minus loans) in the home currency. */
export function netWorthInHome(net: Money, portfolio: Parts | null | undefined, inHome: Convert): Money {
  if (!portfolio) return inHome(net);
  if (has(portfolio.grand_floor_nwx) && has(portfolio.grand_exact_home) && has(portfolio.loan_floor_nwx) && has(portfolio.loan_exact_home)) {
    const rest = moneyAdd(moneyAdd(net, portfolio.grand_floor_nwx, true), portfolio.loan_floor_nwx);
    return moneyAdd(moneyAdd(inHome(rest), portfolio.grand_exact_home), portfolio.loan_exact_home, true);
  }
  if (has(portfolio.cash_value_nwx)) return moneyAdd(inHome(moneyAdd(net, portfolio.cash_value_nwx, true)), portfolio.cash);
  return inHome(net);
}
