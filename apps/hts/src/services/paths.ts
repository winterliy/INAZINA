// Route table kept free of page components so the router can resolve a path without loading them.
export const SERVICE_PATHS = ["/transfer", "/bank", "/invest", "/casino", "/history", "/market", "/financials", "/info"] as const;
export type ServicePath = (typeof SERVICE_PATHS)[number];
export const servicePath = (path: string): ServicePath | undefined => SERVICE_PATHS.find((key) => path === key || path === `${key}/`);

// Old /commands/<name> links land on the page that now offers that feature.
const LEGACY: Record<string, string> = { 송금: "/transfer", 도박: "/casino", 복권구매: "/casino", 내복권: "/casino", 복권결과: "/casino", 복권규칙: "/casino",
  계좌개설: "/bank", 계좌해지: "/bank", 대출신청: "/bank", 대출상환: "/bank", 대출연장: "/bank", 법정최고이자: "/info", 재무: "/financials", 파산: "/info", 주총소집: "/info" };
export const legacyCommandPath = (name: string) => LEGACY[name] ?? (["매수", "매도", "주문취소", "내주문", "호가"].includes(name) ? "/" : "/invest");
