import type { ApiUser } from "../../shared/api";
import { WithdrawalPanel } from "../../shared/WithdrawalPanel";
import { canRequestWithdrawal } from "./withdrawal";

/** The bottom of the account page: self-service withdrawal for ordinary members (not administrators, countries, companies or delegated sessions). */
export function AccountWithdrawal({ user }: { user: ApiUser }) {
  return canRequestWithdrawal(user) ? <WithdrawalPanel /> : null;
}
