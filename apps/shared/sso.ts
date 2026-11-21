// Single sign-on in the apps: what to do on arrival (go to the identity provider, open the register or reset view, show
// an error), where to come back to, and where to go after signing out. The server side is apps/api/src/oidc/rp.ts.

export const SSO_LOGIN_PATH = "/api/auth/oidc/login";

type Where = { pathname: string; search: string };
export type SsoAction =
  | { kind: "none" }
  | { kind: "redirect"; url: string }
  | { kind: "register" | "reset-password" }
  | { kind: "error"; message: string };

// Parameters of the sign-in round trips; none of them belongs in the page people come back to.
const transient = ["login_error", "register", "reset", "discordAuth", "authError"];

const loginErrorMessages: Record<string, string> = {
  login_expired: "로그인 시간이 지났습니다. 다시 로그인해 주세요.",
  state_mismatch: "로그인 요청을 확인하지 못했습니다. 다시 로그인해 주세요.",
  login_denied: "로그인이 취소되었습니다.",
  login_failed: "로그인을 완료하지 못했습니다. 다시 시도해 주세요.",
  invalid_token: "로그인 정보를 확인하지 못했습니다. 다시 시도해 주세요.",
  oidc_unavailable: "로그인 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
};

export function loginErrorMessage(code: string | null): string | undefined {
  return code ? loginErrorMessages[code] ?? "로그인하지 못했습니다. 다시 시도해 주세요." : undefined;
}

export const ssoLoginUrl = (returnTo: string): string => `${SSO_LOGIN_PATH}?returnTo=${encodeURIComponent(returnTo)}`;

export function currentReturnTo(where: Where): string {
  const query = new URLSearchParams(where.search);
  transient.forEach((name) => query.delete(name));
  const rest = query.toString();
  return rest ? `${where.pathname}?${rest}` : where.pathname;
}

// True when the URL asks for the account panel (register, password reset, a sign-in error, a Discord round trip).
export function authPanelRequested(search: string): boolean {
  const query = new URLSearchParams(search);
  return transient.some((name) => query.has(name));
}

// `autoRedirect`: the admin console sends signed-out visitors straight to the identity provider; the HTS stays open to guests.
export function ssoAction(oidc: boolean, where: Where, options: { autoRedirect: boolean }): SsoAction {
  if (!oidc) return { kind: "none" };
  const query = new URLSearchParams(where.search);
  if (query.has("login_error")) return { kind: "error", message: loginErrorMessage(query.get("login_error"))! };
  if (query.get("register") === "1") return { kind: "register" };
  if (query.get("reset") === "1") return { kind: "reset-password" };
  if (query.has("discordAuth") || query.has("authError")) return { kind: "none" };
  return options.autoRedirect ? { kind: "redirect", url: ssoLoginUrl(currentReturnTo(where)) } : { kind: "none" };
}

// After /api/auth/logout: the identity provider's end-session address, unless this was only leaving a delegated account.
export function logoutRedirect(result: { logoutUrl?: string; returnedTo?: unknown }): string | undefined {
  if (result.returnedTo || !result.logoutUrl) return undefined;
  try {
    const url = new URL(result.logoutUrl); // the server always sends an absolute address
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : undefined;
  } catch { return undefined; }
}
