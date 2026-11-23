import { formatMoney, formatMoneyNumeric, moneyCompare, type Money } from "../../shared/money";
import { ApiError, type ApiUser } from "../../shared/api";
import type { HTMLAttributes } from "react";
import type { Instrument, KycProfile, Warning } from "./types";

export type DrMovement = { movement_id: number; dr_code: string; underlying_code: string; underlying_currency: string; movement_type: "issue" | "redeem"; dr_quantity: number; underlying_quantity: number; underlying_average_cost: number; underlying_average_cost_text: string; decimal_places: number; recorded_epoch: number };
export type DrCustodyInstruction = { reference_key: string; dr_code: string; underlying_code: string; movement_type: "issue" | "redeem"; dr_quantity: number; underlying_quantity: number; status: "requested" | "verified" | "settled" | "rejected" | "cancelled"; requested_epoch: number; settle_after_epoch: number | null };
export type DrVotingDelegation = { delegation_id: number; dr_code: string; underlying_code: string; proxy_user_id: string; dr_quantity: number; status: "active" | "revoked"; created_epoch: number; revoked_epoch: number | null; events: { event_type: string; recorded_epoch: number }[] };
export type ReceivedDrVotingDelegation = { delegation_id: number; dr_code: string; underlying_code: string; owner_user_id: string; dr_quantity: number; created_epoch: number };
export type DrVotingAgenda = { meetingId: number; agendaItemId: number; underlyingCode: string; meetingTitle: string; agendaTitle: string; choices: string[]; recordEpoch: number; opensEpoch: number | null; closesEpoch: number | null; quorumPercent: number; votingPower: number; totalEligible: number; myChoice: string | null; state: "unscheduled" | "upcoming" | "open" | "closed" };

// Administrator trading warnings (투자주의·경고·위험).
export const WarningBadge = ({ warning }: { warning?: Warning }) => warning ? <span className={`warn-badge ${warning.level}`} title={warning.reason || warning.label}>{warning.label}</span> : null;
export type LoanSchedule = { schedule_id: number; installment_number: number; due_epoch: number; principal_due: Money; interest_due: Money; fee_due: Money; principal_paid: Money; interest_paid: Money; fee_paid: Money; amount_paid: Money; status: string };
export type LoanRateReset = { old_rate: number; new_rate: number; scheduled_epoch: number; applied_epoch: number; benchmark_key: string; benchmark_tenor_days: number };
export const RATE_BENCHMARK_LABELS: Record<string, string> = { policy: "정책금리", funding: "조달금리", bank_bond: "은행채 수익률", treasury: "국채 수익률" };
export type LoanScheduleView = { loan_id: number; product_code: string; quote_currency: string; decimal_places: number; outstanding_balance: Money; status: string; rate_type: string; contract_rate: number | null; next_repricing_epoch: number | null; benchmark_key: string | null; benchmark_tenor_days: number; repricing_lag_seconds: number; collection_status: string; maturity_epoch: number | null; stage_cure_since_epoch: number | null; underwriting_dsr: number | null; underwriting_stress_dsr: number | null; underwriting_stress_shock_bps: number; underwriting_fx_stress_shock_percent: number; origination_pd_12m: number | null; current_ecl_stage: number | null; current_ecl_pd_12m: number | null; installments: LoanSchedule[]; rate_resets: LoanRateReset[] };
export type TaxResidency = { nationality: string | null; taxResidency: string | null; countries: string[] };
export type Book = { bids: { limit_price: Money; quantity: number; order_count: number }[]; asks: { limit_price: Money; quantity: number; order_count: number }[] };

export const ranges = ["1h", "1d", "1w", "1m", "all"] as const;
export const rangeNames: Record<string, string> = { "1h": "1시간", "1d": "1일", "1w": "1주", "1m": "1개월", all: "전체" };
export const intervals: [number, string][] = [[60, "1분봉"], [300, "5분봉"], [600, "10분봉"], [1800, "30분봉"], [3600, "1시간봉"], [21600, "6시간봉"], [43200, "12시간봉"], [86400, "일봉"]];
export const STATUS: Record<string, string> = { open: "미체결", partial: "부분 체결", scheduled: "예약", filled: "체결", cancelled: "취소", expired: "만료" };
export const money = formatMoney;
export const compactMoney = formatMoneyNumeric;
export const ORDER_TYPES = [["limit", "지정가"], ["market", "시장가"], ["current", "현재가"]] as const;
// A missing or invalid time shows as a dash; one bad row must never blank the whole page.
export const date = (epoch: number) => Number.isFinite(epoch * 1000) ? new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" }).format(epoch * 1000) : "—";
export const KYC_STATUS: Record<KycProfile["status"], string> = { pending: "확인 대기", verified: "확인 완료", rejected: "확인 거절", expired: "만료" };
export const KYC_RISK: Record<KycProfile["risk_tier"], string> = { standard: "일반", enhanced: "강화 심사", restricted: "송금 제한" };
export const fundCategoryName: Record<string, string> = { equity: "주식형", bond: "채권형", mixed: "혼합형", other: "기타형" };
export const instrumentType = (item: Instrument) => item.type === "depositary_receipt" ? "주식예탁증서 (DR)" : fundCategoryName[item.fund_category || ""] || item.type;
// Search tabs shown while the search box is empty. "추천" is the starting tab.
export type SearchTab = "recommended" | "stock" | "fund" | "bond" | "commodity" | "other";
export const SEARCH_TABS: [SearchTab, string][] = [["recommended", "실시간 추천"], ["stock", "주식"], ["fund", "ETF·펀드"], ["bond", "채권"], ["commodity", "원자재·외환"], ["other", "기타"]];
export const searchTab = (item: Instrument): Exclude<SearchTab, "recommended"> =>
  item.type === "stock" || item.type === "preferred" || item.type === "depositary_receipt" ? "stock"
    : ["etf", "etn", "mutual_fund", "index_fund", "leveraged_etf", "inverse_etf", "reit"].includes(item.type) ? "fund"
      : item.type === "국채" || item.type.includes("bond") ? "bond"
        : item.type === "commodity" || item.type === "forex" ? "commodity" : "other";
// "실시간 추천" rankings over the last 24 hours, top 20 each.
export type RecommendSort = "value" | "volume" | "rising" | "falling";
export const RECOMMEND_SORTS: [RecommendSort, string, string][] = [
  ["value", "거래대금", "최근 24시간 거래대금이 많은 순"],
  ["volume", "거래량", "최근 24시간 거래 수량이 많은 순"],
  ["rising", "급상승", "최근 24시간 상승률이 높은 순 (상승 종목만)"],
  ["falling", "급하락", "최근 24시간 하락률이 큰 순 (하락 종목만)"],
];
export const recommended = (items: Instrument[], sort: RecommendSort) => {
  const change = (item: Instrument) => item.change24h ?? 0;
  const ranked = sort === "rising" ? items.filter((item) => change(item) > 0).sort((a, b) => change(b) - change(a))
    : sort === "falling" ? items.filter((item) => change(item) < 0).sort((a, b) => change(a) - change(b))
      : [...items].sort(sort === "volume"
        ? (a, b) => (b.volume24h ?? 0) - (a.volume24h ?? 0) || moneyCompare(b.tradeValue24h ?? 0, a.tradeValue24h ?? 0)
        : (a, b) => moneyCompare(b.tradeValue24h ?? 0, a.tradeValue24h ?? 0) || Math.abs(change(b)) - Math.abs(change(a)));
  return ranked.slice(0, 20);
};
/** The board is its own page (/board); "종목 토론" opens it filtered to one instrument. */
export const boardUrl = (asset = "") => asset ? `/board?asset=${encodeURIComponent(asset)}` : "/board";
export const errorText = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;
/** 32 hex characters. getRandomValues also works on plain-http LAN addresses, where crypto.randomUUID does not. */
export const requestKey = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");

export const ROLE_LABEL: Record<string, string> = { user: "개인 계정", admin: "관리자", nation: "국가 계정", company: "법인 계정" };
export const INDICATOR_KEY = "inazina.hts.chart-indicators.v2";

export const WATCH_KEY = "inazina.hts.watchlist";
export const USER_KEY = "inazina.hts.user";
export const GUEST_USER: ApiUser = { userId: "", nickname: "", nationality: "", role: "user", cash: 0, creditScore: 0 };
export const USER_ROLES = ["admin", "user", "nation", "company"];
/** The cached user is only trusted when it has the shape the pages rely on; the session check still confirms it. */
export function cachedUser(): ApiUser | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(USER_KEY) ?? "null");
    if (value && typeof value === "object" && typeof value.userId === "string" && value.userId && typeof value.nickname === "string"
      && typeof value.nationality === "string" && USER_ROLES.includes(value.role) && ["string", "number"].includes(typeof value.cash)) return value as ApiUser;
  } catch { /* unreadable cache */ }
  return null;
}
export type Notice = { text: string; kind: "error" | "success"; expired?: boolean };
/** A definitive refusal (the server read the request and said no); network errors and 5xx may have succeeded. */
export const definitiveRefusal = (cause: unknown) => cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 408 && cause.status !== 429;
export const wholeNumber = (value: string) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value));


/** Spread onto a login-only part of the page: blurred for visitors, any click or Enter asks them to sign in. */
export type LockProps = HTMLAttributes<HTMLElement> & { "data-login-label"?: string };
export type Lock = (label: string) => LockProps;

export function groupAmount(value: string) {
  const [whole, fraction] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}
