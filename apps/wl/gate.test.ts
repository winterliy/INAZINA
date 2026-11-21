// Run: node --import tsx --test apps/wl/gate.test.ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { cookieValue, failureLimiter, sessionCookie, sessionStore } from "./gate.ts";

test("the limiter blocks after five failures, forgets after the window, and prunes", () => {
  let clock = 0;
  const limit = failureLimiter(5, 600_000, () => clock);
  for (let i = 0; i < 4; i++) limit.fail("a");
  assert.equal(limit.blocked("a"), false);
  limit.fail("a");
  assert.equal(limit.blocked("a"), true);
  assert.equal(limit.blocked("b"), false);
  limit.fail("b");
  clock = 600_001;
  assert.equal(limit.blocked("a"), false);
  assert.equal(limit.size, 0, "expired entries are pruned");
  limit.fail("a");
  limit.clear("a");
  assert.equal(limit.size, 0);
});

test("sessions are random, expire and are only valid when issued", () => {
  let clock = 0;
  const sessions = sessionStore(1000, () => clock);
  const one = sessions.issue(), two = sessions.issue();
  assert.notEqual(one, two);
  assert.equal(sessions.valid(one), true);
  assert.equal(sessions.valid("forged"), false);
  assert.equal(sessions.valid(undefined), false);
  clock = 1001;
  assert.equal(sessions.valid(one), false);
  assert.equal(sessions.size, 0);
});

test("the cookie is HttpOnly and SameSite=Strict and is read back from a Cookie header", () => {
  const cookie = sessionCookie("tok");
  assert.match(cookie, /HttpOnly/);
  assert.match(cookie, /SameSite=Strict/);
  assert.equal(cookieValue("a=1; wl_session=tok; b=2", "wl_session"), "tok");
  assert.equal(cookieValue("xwl_session=tok", "wl_session"), undefined);
  assert.equal(cookieValue(undefined, "wl_session"), undefined);
});
