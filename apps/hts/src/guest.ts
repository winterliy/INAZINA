import { api } from "../../shared/api";
import { authPanelRequested, currentReturnTo, ssoLoginUrl } from "../../shared/sso";

/** Pages a visitor without an account may open: the market home and the heatmap. Everything else shows a login prompt. */
export const guestCanView = (pathname: string): boolean => pathname === "/" || /^\/heatmap\/?$/.test(pathname);

/** True when the URL asks for the full-screen account panel: register/reset/sign-in error/Discord round trip, or a board visitor
 *  sent here to sign in without SSO ("/?return=/board..."). */
export function loginPanelRequested(search: string): boolean {
  return authPanelRequested(search) || (new URLSearchParams(search).get("return") ?? "").startsWith("/board");
}

/** What "log in" does: with single sign-on go to the identity provider and come back to the same page; otherwise open the panel. */
export function loginAction(oidc: boolean, where: { pathname: string; search: string }): { kind: "redirect"; url: string } | { kind: "panel" } {
  return oidc ? { kind: "redirect", url: ssoLoginUrl(currentReturnTo(where)) } : { kind: "panel" };
}

let oidcPromise: Promise<boolean> | undefined;
/** Whether this host signs in through single sign-on (asked once; false when the answer cannot be read). */
export function oidcEnabled(): Promise<boolean> {
  oidcPromise ??= api<{ oidc?: boolean }>("/api/auth/config").then((config) => config.oidc === true).catch(() => { oidcPromise = undefined; return false; });
  return oidcPromise;
}
