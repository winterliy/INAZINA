import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

// The apps (naru, console) have no login service of their own any more: signing in, registering and resetting a password
// exist only at the login server (apps/api/src/oidc). This guards the old routes against coming back.
const source = readFileSync(new URL("./index.ts", import.meta.url), "utf8");

test("the API no longer has its own login, register, password reset or Discord routes", () => {
  for (const gone of ["/api/auth/login", "/api/auth/register", "/api/auth/password/reset", "/api/auth/discord", "/api/auth/countries", "/api/auth/config",
    "DISCORD_REDIRECT_URI", "beginDiscordOAuth", "finishDiscordOAuth", "failedLogins", "consumedVerificationNonces"]) {
    assert.ok(!source.includes(gone), `${gone} is still in index.ts`);
  }
});

test("what the apps still use to hold and end a session stays", () => {
  for (const kept of ["/api/auth/session", "/api/auth/logout", "/api/admin/login-as", "oidc.rp"]) assert.ok(source.includes(kept), `${kept} is missing from index.ts`);
});

// 4312 and 4313 have no browser pages and no login of their own either: a browser gets the JSON as it is. The gate (landing
// pages, the browser-access login, the emergency passcode, the attempt log, the login server's fallback page) is gone.
test("the API serves JSON to browsers too: no landing pages, no browser-access login, no emergency passcode", () => {
  for (const gone of ["__browser-access", "emergencyPasscode", "sendLanding", "browserAccess", "landing.js", "browserNavigation", "requestBrowserAccess", "FAVICON_PNG"]) {
    assert.ok(!source.includes(gone), `${gone} is still in index.ts`);
  }
  const root = new URL("../../../", import.meta.url);
  for (const file of ["apps/api/src/landing.ts", "apps/api/src/browserAccess.ts", "apps/shared/gate.html"]) assert.ok(!existsSync(new URL(file, root)), `${file} still exists`);
  assert.ok(!readFileSync(new URL("./oidc/interaction.ts", import.meta.url), "utf8").includes("gate.html"), "the login server must not use the old gate page");
});
