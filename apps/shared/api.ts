import type { Money } from "./money";
const TOKEN_KEY = "inazina.web.token";
const UNAUTHORIZED_EVENT = "inazina:unauthorized";

/** A revised document (terms, privacy policy) now in force that this member was asked to agree to and has not yet. */
export type ConsentRequirement = { campaignId: number; documentId: string; title: string; summary: string; effectiveEpoch: number };

export type ApiUser = {
  userId: string;
  nickname: string;
  nationality: string;
  /** Home currency of the nationality; wallet amounts are shown in it. */
  currency?: string;
  role: "admin" | "user" | "nation" | "company";
  cash: Money;
  creditScore: number;
  /** Set when an administrator signed in as this country or company account ($login). */
  delegatedBy?: string;
  delegatedByName?: string;
  /** Set while the member is held until they agree to (or leave over) a revised document. */
  consentRequired?: ConsentRequirement[];
};

/** The HTS and the dashboard run on neighbouring ports of the same host. */
export const appUrl = (app: "hts" | "dashboard", path = "/") => `${location.protocol}//${location.hostname}:${app === "hts" ? 4311 : 4310}${path}`;

/** True when the server said the session is gone (not when it was merely unreachable or slow). */
export const signedOut = (cause: unknown) => [401, 403].includes((cause as { status?: number })?.status ?? 0);

/** Cookie auth replaced the old sessionStorage token; this only wipes a leftover one. */
export function clearToken(): void {
  try { sessionStorage.removeItem(TOKEN_KEY); } catch { /* storage blocked */ }
}

/** Runs when any api() call (outside /api/auth/) comes back 401, so a page can drop to its login screen. */
export function onUnauthorized(handler: () => void): () => void {
  window.addEventListener(UNAUTHORIZED_EVENT, handler);
  return () => window.removeEventListener(UNAUTHORIZED_EVENT, handler);
}

/** The error api() throws: a readable message, the HTTP status and the server's machine code. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message);
    this.name = "ApiError";
  }
}

/** Builds the ApiError for a failed response. Callers throw the result. */
export function apiError(status: number, body: { error?: string; message?: string; approvalRequestId?: number }): ApiError {
  const messages: Record<string, string> = {
    authentication_required: "인증 토큰을 확인해주세요.",
    web_auth_not_configured: "서버에 웹 인증 토큰이 설정되지 않았습니다.",
    discord_oauth_not_configured: "Discord OAuth 설정이 필요합니다. .env 설정을 확인해주세요.",
    discord_verification_required: "Discord 본인확인을 먼저 완료해주세요.",
    discord_authorization_failed: "Discord 인증이 취소되었거나 실패했습니다.",
    discord_verification_failed: "Discord 계정을 확인하지 못했습니다.",
    discord_state_invalid: "인증 요청이 만료되었습니다. 다시 시도해주세요.",
    login_rate_limited: "로그인 시도가 많습니다. 15분 후 다시 시도해주세요.",
    country_not_found: "사용 가능한 국가 ID를 선택해주세요.",
    country_account_exists: "이미 해당 국가 계정이 있습니다.",
    country_account_conflict: "국가 계정을 생성할 수 없습니다. 관리자에게 문의해주세요.",
    password_policy: "비밀번호는 영문과 숫자를 포함해 12~128자로 입력해주세요.",
    hts_id_policy: "아이디는 영문 소문자, 숫자, 점, 밑줄, 하이픈으로 3~32자 입력해주세요. 숫자만 17~20자리로는 만들 수 없습니다(디스코드 ID와 같은 형태).",
    hts_id_taken: "이미 사용 중인 아이디입니다.",
    active_user_required: "활성 사용자 계정이 아닙니다.",
    admin_required: "관리자 권한이 필요합니다.",
    python_service_unavailable: "Python 데이터 서비스에 연결할 수 없습니다.",
    database_unavailable: "데이터베이스를 읽지 못했습니다.",
    role_reason_required: "관리자 권한을 바꾸는 사유를 입력해주세요(2인 승인이 켜져 있을 때 필수).",
  };
  if (body.error === "approval_required" && body.approvalRequestId) {
    return new ApiError(`승인 요청 번호 ${body.approvalRequestId} — 통제 · 복구에서 다른 관리자의 승인이 필요합니다. 승인된 뒤 같은 작업을 다시 실행하세요.`, status, body.error);
  }
  const text = body.error === "database_unavailable" && body.message ? body.message : (body.error ? messages[body.error] : undefined) ?? body.message ?? body.error ?? `HTTP ${status}`;
  return new ApiError(text, status, body.error);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  // JSON text by default; a File/Blob body (board uploads) keeps its own type.
  if (typeof init.body === "string") headers.set("Content-Type", "application/json");

  const response = await fetch(path, { ...init, headers, cache: "no-store", credentials: "include" });
  const raw = await response.text();
  let body: { error?: string; message?: string } = {};
  let isJson = true;
  if (raw.trim()) {
    try { body = JSON.parse(raw) ?? {}; } catch { isJson = false; }
  }
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/api/auth/")) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw apiError(response.status, isJson ? body : {});
  }
  // A 200 that is not JSON is a proxy or gateway page, not our server.
  if (!isJson) throw new ApiError("서버 응답을 해석하지 못했습니다. 잠시 후 다시 시도해주세요.", response.status, "invalid_response");
  return body as T;
}
