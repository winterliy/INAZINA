/** "방금", "12분 전", "3시간 전", "어제", then the date. */
export function ago(epoch: number, now = Date.now() / 1000): string {
  const seconds = Math.max(0, now - epoch);
  if (seconds < 60) return "방금";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}분 전`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}시간 전`;
  if (seconds < 172800) return "어제";
  return new Date(epoch * 1000).toLocaleDateString("ko-KR");
}

/** "1250000" -> "1,250,000"; keeps the decimals a currency has. */
export function formatAmount(value: string): string {
  const [whole, fraction] = value.split(".");
  return Number(whole).toLocaleString("ko-KR") + (fraction ? `.${fraction}` : "");
}

/** Whole days left until an epoch (never negative). */
export function daysLeft(epoch: number, now = Date.now() / 1000): number {
  return Math.max(0, Math.ceil((epoch - now) / 86400));
}

/** A payment waiting for its receiver (see PayoutManager.view on the server). */
export type Payout = {
  payout_id: number;
  status: "pending" | "converted" | "temp_open" | "withdrawn" | "forfeited";
  kind: string;
  currency: string;
  decimal_places: number;
  amount: string;
  withdraw_fee: string;
  withdraw_net: string;
  created_epoch: number;
  expires_epoch: number;
  temp_number: string | null;
  payer_id: string;
};
