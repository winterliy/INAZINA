const TOKEN_KEY = "inazina.web.token";

export type ApiUser = {
  userId: string;
  nickname: string;
  nationality: string;
  role: "admin" | "user";
  cash: number;
  creditScore: number;
};

export function getToken(): string {
  return sessionStorage.getItem(TOKEN_KEY) ?? "";
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");

  const response = await fetch(path, { ...init, headers, cache: "no-store" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const messages: Record<string, string> = {
      authentication_required: "인증 토큰을 확인해주세요.",
      web_auth_not_configured: "서버에 웹 인증 토큰이 설정되지 않았습니다.",
      active_user_required: "활성 사용자 계정이 아닙니다.",
      admin_required: "관리자 권한이 필요합니다.",
      python_service_unavailable: "Python 데이터 서비스에 연결할 수 없습니다.",
      database_unavailable: "데이터베이스를 읽지 못했습니다.",
    };
    throw new Error(messages[body.error] ?? body.message ?? body.error ?? `HTTP ${response.status}`);
  }
  return body as T;
}

export async function signIn(token: string): Promise<ApiUser> {
  sessionStorage.setItem(TOKEN_KEY, token.trim());
  try {
    const result = await api<{ user: ApiUser }>("/api/auth/session");
    return result.user;
  } catch (error) {
    clearToken();
    throw error;
  }
}
