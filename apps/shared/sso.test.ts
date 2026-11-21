import test from "node:test";
import assert from "node:assert/strict";
import { authPanelRequested, currentReturnTo, logoutRedirect, loginErrorMessage, ssoAction, ssoLoginUrl } from "./sso.js";

const at = (pathname: string, search = "") => ({ pathname, search });

test("ssoLoginUrl carries the page to come back to", () => {
  assert.equal(ssoLoginUrl("/portfolio?tab=1"), "/api/auth/oidc/login?returnTo=%2Fportfolio%3Ftab%3D1");
});

test("currentReturnTo drops the transient sign-in parameters but keeps the rest", () => {
  assert.equal(currentReturnTo(at("/portfolio", "?tab=1&login_error=login_failed&register=1")), "/portfolio?tab=1");
  assert.equal(currentReturnTo(at("/", "?login_error=x&reset=1&discordAuth=verified&authError=y")), "/");
  assert.equal(currentReturnTo(at("/market", "")), "/market");
});

test("without single sign-on nothing changes", () => {
  assert.deepEqual(ssoAction(false, at("/"), { autoRedirect: true }), { kind: "none" });
  assert.deepEqual(ssoAction(false, at("/", "?register=1"), { autoRedirect: true }), { kind: "none" });
});

test("a signed-out visitor of an app that auto-redirects is sent to the identity provider", () => {
  assert.deepEqual(ssoAction(true, at("/orders", "?a=1"), { autoRedirect: true }), { kind: "redirect", url: "/api/auth/oidc/login?returnTo=%2Forders%3Fa%3D1" });
});

test("an app that does not auto-redirect (the HTS, open to guests) just shows its page", () => {
  assert.deepEqual(ssoAction(true, at("/orders"), { autoRedirect: false }), { kind: "none" });
});

test("register and reset links from the identity provider open those existing views, never a redirect", () => {
  assert.deepEqual(ssoAction(true, at("/", "?register=1"), { autoRedirect: true }), { kind: "register" });
  assert.deepEqual(ssoAction(true, at("/", "?reset=1"), { autoRedirect: true }), { kind: "reset-password" });
});

test("a sign-in error is shown with a Korean message and is never answered by another redirect (no loop)", () => {
  const action = ssoAction(true, at("/", "?login_error=oidc_unavailable"), { autoRedirect: true });
  assert.equal(action.kind, "error");
  assert.match((action as { message: string }).message, /로그인 서버/);
  assert.equal((ssoAction(true, at("/", "?login_error=something_new"), { autoRedirect: true }) as { message: string }).message, loginErrorMessage("something_new"));
  assert.ok(loginErrorMessage("something_new"));
});

test("coming back from the Discord verification is left to the existing flow", () => {
  assert.deepEqual(ssoAction(true, at("/", "?discordAuth=verified"), { autoRedirect: true }), { kind: "none" });
  assert.deepEqual(ssoAction(true, at("/", "?authError=oauth_state_invalid"), { autoRedirect: true }), { kind: "none" });
});

test("authPanelRequested is true for the parameters that need the account panel and false otherwise", () => {
  for (const search of ["?register=1", "?reset=1", "?login_error=login_failed", "?discordAuth=verified", "?authError=x"]) assert.equal(authPanelRequested(search), true, search);
  for (const search of ["", "?tab=1", "?ret=/orders"]) assert.equal(authPanelRequested(search), false, search);
});

test("logoutRedirect follows the identity provider's sign-out only for a real sign-out", () => {
  assert.equal(logoutRedirect({ logoutUrl: "https://id.inazina.kr/oidc/session/end?client_id=naru" }), "https://id.inazina.kr/oidc/session/end?client_id=naru");
  assert.equal(logoutRedirect({}), undefined);
  assert.equal(logoutRedirect({ logoutUrl: "https://id.inazina.kr/x", returnedTo: { userId: "admin" } }), undefined, "leaving a delegated account stays in the app");
  assert.equal(logoutRedirect({ logoutUrl: "javascript:alert(1)" }), undefined);
});
