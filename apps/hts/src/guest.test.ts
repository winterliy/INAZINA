import assert from "node:assert/strict";
import test from "node:test";
import { guestCanView, loginAction, loginPanelRequested } from "./guest.ts";

test("a visitor may open only the market home and the heatmap", () => {
  for (const path of ["/", "/heatmap", "/heatmap/"]) assert.equal(guestCanView(path), true, path);
  for (const path of ["/account", "/credit", "/portfolio", "/ownership", "/payouts/3", "/commands/x", "/loans", "/board"]) assert.equal(guestCanView(path), false, path);
});

test("the account panel opens for register, reset, errors, Discord round trips and board sign-in", () => {
  for (const search of ["?register=1", "?reset=1", "?login_error=state_mismatch", "?discordAuth=verified", "?authError=x", "?return=%2Fboard%3Fasset%3DA"]) assert.equal(loginPanelRequested(search), true, search);
  for (const search of ["", "?code=ABC", "?exchange=X", "?return=https%3A%2F%2Fevil.example"]) assert.equal(loginPanelRequested(search), false, search);
});

test("login goes to the identity provider with a clean return path, or opens the panel without SSO", () => {
  assert.deepEqual(loginAction(true, { pathname: "/heatmap", search: "?exchange=KX&login_error=x" }), { kind: "redirect", url: "/api/auth/oidc/login?returnTo=%2Fheatmap%3Fexchange%3DKX" });
  assert.deepEqual(loginAction(false, { pathname: "/", search: "" }), { kind: "panel" });
});
