import assert from "node:assert/strict";
import test from "node:test";
import { guestCanView, loginPanelRequested } from "./guest.ts";
import { ssoLoginUrl, currentReturnTo } from "../../shared/sso.ts";

test("a visitor may open only the market home and the heatmap", () => {
  for (const path of ["/", "/heatmap", "/heatmap/"]) assert.equal(guestCanView(path), true, path);
  for (const path of ["/account", "/credit", "/portfolio", "/ownership", "/payouts/3", "/commands/x", "/loans", "/board"]) assert.equal(guestCanView(path), false, path);
});

test("the sign-in gate opens for register, reset, sign-in errors and old Discord round trips, and for nothing else", () => {
  for (const search of ["?register=1", "?reset=1", "?login_error=state_mismatch", "?discordAuth=verified", "?authError=x"]) assert.equal(loginPanelRequested(search), true, search);
  for (const search of ["", "?code=ABC", "?exchange=X", "?return=%2Fboard%3Fasset%3DA", "?return=https%3A%2F%2Fevil.example"]) assert.equal(loginPanelRequested(search), false, search);
});

test("every login goes to the login server and comes back to the same page, without the sign-in parameters", () => {
  assert.equal(ssoLoginUrl(currentReturnTo({ pathname: "/heatmap", search: "?exchange=KX&login_error=x" })), "/api/auth/oidc/login?returnTo=%2Fheatmap%3Fexchange%3DKX");
});
