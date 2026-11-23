import { LEGACY_PAGE } from "./nav";

export type Page = "home" | "account" | "overview" | "instruments" | "finance" | "exchanges" | "companies" | "organizations" | "users" | "naturalization" | "court" | "feedback" | "board" | "documents" | "credit" | "accounting" | "close" | "alm" | "rates" | "events" | "liquidity" | "commands" | "database" | "security" | "controls" | "ownership";

export const pages: { id: Page; label: string }[] = [
  { id: "home", label: "메인" },
  { id: "account", label: "내 계정" },
  { id: "overview", label: "운영 현황" },
  { id: "instruments", label: "종목 · 상품" },
  { id: "finance", label: "금융 상품" },
  { id: "exchanges", label: "거래소" },
  { id: "companies", label: "법인 자본" },
  { id: "organizations", label: "기업 구조" },
  { id: "users", label: "계정관리" },
  { id: "naturalization", label: "귀화심사" },
  { id: "court", label: "회생·파산" },
  { id: "feedback", label: "문의·신고" },
  { id: "board", label: "게시판 관리" },
  { id: "credit", label: "신용위험" },
  { id: "accounting", label: "은행 · 원장" },
  { id: "close", label: "회계 마감" },
  { id: "alm", label: "자금 · ALM" },
  { id: "rates", label: "금리 · 한도 · 정리" },
  { id: "documents", label: "문서 · 약관" },
  { id: "events", label: "시장 사건" },
  { id: "liquidity", label: "유동성" },
  { id: "commands", label: "관리자 명령어" },
  { id: "database", label: "DB 편집" },
  { id: "security", label: "보안" },
  { id: "controls", label: "통제 · 복구" },
  { id: "ownership", label: "조직도" },
];

export const subtitles: Record<Page, string> = {
  home: "처리할 일과 시장 상황, 최근 작업을 한눈에 봅니다.",
  account: "관리자 계정의 로그인 정보, 실행 명의와 최근 로그인을 관리합니다.",
  overview: "시장 상태와 서비스 연결을 확인하고 거래소를 제어합니다.",
  instruments: "주식·자산·ETF·선물·채권·펀드·원자재·옵션·ELS를 생성·수정·중지합니다.",
  finance: "저축·대출·통화·외환통장 상품을 생성·수정·중지합니다.",
  exchanges: "거래소별 개장·휴장·서킷브레이커와 소속 국가·지수를 관리합니다.",
  companies: "법인 현금·보유자산·통장·대출을 보고 법인 계정으로 매수·매도·송금·대출을 실행합니다.",
  organizations: "기업 규모와 공공 분류, 주식 종류별 권리, 기업집단 및 지분·의결권 구조를 관리합니다.",
  naturalization: "Discord /귀화신청으로 들어온 신청을 심사합니다. 승인하면 신청자의 자산을 정리하고 국적을 옮깁니다.",
  court: "나루 회생 · 파산 탭으로 들어온 신청을 심리하고 인가·기각합니다. 결정은 신청자에게 Discord DM으로 전달됩니다.",
  feedback: "공식 사이트의 Contact·Report로 들어온 문의와 신고를 확인하고 처리합니다.",
  documents: "약관·처리방침·규칙·도움말과 Discord 등록 안내문을 고치고 확정합니다.",
  board: "나루 게시판의 신고를 처리하고 글을 숨기거나 고정합니다. 익명 글의 실제 작성자와 IP를 확인할 수 있습니다.",
  credit: "기대신용손실(ECL), 부도채권 상각·회수, 담보, 신용등급 정책을 관리합니다.",
  alm: "은행의 자본 비율(CET1·Tier 1·총자본), 레버리지, LCR, NSFR을 계산해 보고 관할별 기준을 조정합니다. 조회 전용이며 실제 자금은 움직이지 않습니다.",
  rates: "국가별 최고금리·APR 표시·세금 규칙을 계약일 기준으로 관리합니다. 등록한 규칙은 이후에 체결하는 계약에만 적용되며 기존 계약은 바뀌지 않습니다.",
  close: "마감 게이트를 확인하고 환전 반올림 잔여를 환전 손익 계정에 정리하거나 국고로 이전합니다.",
  accounting: "은행을 고유 ID로 만들고 관리하며, 은행별 이중분개 시산 잔액·불균형·결과 원장 대사를 조회합니다.",
  users: "사용자와 국가, 국가 계정의 모든 설정을 관리합니다.",
  events: "호재·악재 사건을 등록하고 최근 사건을 확인합니다.",
  liquidity: "알고리즘으로 호가를 올려 유동성을 공급하는 회사를 넣고 빼고, 유동성 성향을 정합니다.",
  commands: "Discord의 모든 $ 명령어와 도움말을 같은 검증과 기록으로 실행합니다.",
  database: "DB Browser에서 하던 설정 편집을 감사 기록과 함께 처리합니다.",
  security: "4312·4313 접속 비밀번호를 바꾸고, 접속 시도와 적용 중인 보호를 확인합니다.",
  controls: "통제 증빙표 상태, 복구 훈련·접근 검토 기록, 스트레스 시험 범위를 확인합니다. 읽기 전용입니다.",
  ownership: "기업 지분과 의결권 관계를 넓은 화면에서 확인합니다.",
};

export type Route = { page: Page; sub: string };
// Each page (and each command category) has its own address: #instruments, #commands/거래소.
export function readRoute(): Route {
  const [head, ...rest] = decodeURIComponent(location.hash.replace(/^#\/?/, "")).replace(/\/+$/, "").split("/");
  const id = LEGACY_PAGE[head] ?? head;
  return { page: pages.some((item) => item.id === id) ? id as Page : "home", sub: rest.join("/") };
}
