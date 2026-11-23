import { moneyAdd, type Money } from "../../shared/money";

/** A total (valued in 기준 통화) shown in the home currency. The server turns the cash into whole 기준 통화 units (dropping the
 *  fraction) before adding it to the total, so converting the whole total back loses that fraction and "현금 1,000,000" sits next
 *  to "총자산 999,500". Instead the cash part is taken out, the rest is converted, and the exact cash is added. */
export function totalInHome(total: Money, portfolio: { cash: Money; cash_value_nwx?: Money } | null | undefined, inHome: (nwx: Money) => Money): Money {
  if (!portfolio || portfolio.cash_value_nwx === undefined || portfolio.cash_value_nwx === null) return inHome(total);
  return moneyAdd(inHome(moneyAdd(total, portfolio.cash_value_nwx, true)), portfolio.cash);
}
