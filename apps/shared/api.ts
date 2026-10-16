const TOKEN_KEY = "inazina.web.token";

export type ApiUser = {
  userId: string;
  nickname: string;
  nationality: string;
  /** Home currency of the nationality; wallet amounts are shown in it. */
  currency?: string;
  role: "admin" | "user" | "nation" | "company";
  cash: number;
  creditScore: number;
};

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");

  const response = await fetch(path, { ...init, headers, cache: "no-store", credentials: "include" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
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
      hts_id_policy: "아이디는 영문 소문자, 숫자, 점, 밑줄, 하이픈으로 3~32자 입력해주세요.",
      hts_id_taken: "이미 사용 중인 아이디입니다.",
      active_user_required: "활성 사용자 계정이 아닙니다.",
      admin_required: "관리자 권한이 필요합니다.",
      python_service_unavailable: "Python 데이터 서비스에 연결할 수 없습니다.",
      database_unavailable: "데이터베이스를 읽지 못했습니다.",
    };
    throw new Error(messages[body.error] ?? body.message ?? body.error ?? `HTTP ${response.status}`);
  }
  return body as T;
}
