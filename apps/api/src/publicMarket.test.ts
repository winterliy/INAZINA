import assert from "node:assert/strict";
import test from "node:test";
import { createRateLimiter, createSharedCache, publicMarketPath } from "./publicMarket.js";

const get = (path: string) => publicMarketPath("GET", new URL(`http://x${path}`));

test("opens only the read-only market routes", () => {
  assert.equal(get("/api/market/instruments"), "/internal/market/instruments");
  assert.equal(get("/api/market/exchange-indexes"), "/internal/market/exchange-indexes");
  assert.equal(get("/api/market/quote/AB%20C"), "/internal/market/quote/AB%20C");
  assert.equal(get("/api/market/book/XYZ"), "/internal/market/book/XYZ");
  assert.equal(get("/api/market/chart/XYZ?range=1w&interval=60&upper=ma&evil=1"), "/internal/market/chart/XYZ?range=1w&interval=60&upper=ma");
  assert.match(get("/api/search-indicators?search=abc")!, /^\/internal\/search-indicators\?search=abc$/);
  assert.match(get("/api/search-indicator-chart?kind=a&key=b")!, /kind=a&key=b$/);
});

test("everything personal or writing stays closed", () => {
  for (const path of ["/api/market/orders", "/api/market/portfolio", "/api/market/drawings/XYZ", "/api/me/notifications", "/api/hts/account",
    "/api/admin/instruments", "/api/market/quote/a/b", "/api/market/quote/%E0%A4%A", "/api/me", "/api/auth/session"]) assert.equal(get(path), null, path);
  assert.equal(publicMarketPath("POST", new URL("http://x/api/market/instruments")), null);
});

test("rate limiter counts per address per window", () => {
  let time = 0;
  const allow = createRateLimiter(2, 1000, () => time);
  assert.deepEqual([allow("a"), allow("a"), allow("a"), allow("b")], [true, true, false, true]);
  time = 1001;
  assert.equal(allow("a"), true);
});

test("shared cache reuses one fetch, drops failures and expires", async () => {
  let time = 0, calls = 0, status = 200;
  const cached = createSharedCache(async () => { calls += 1; return { status, body: calls }; }, 1000, () => time);
  const [a, b] = await Promise.all([cached("/p"), cached("/p")]);
  assert.equal(calls, 1); assert.equal(a, b);
  time = 1500; await cached("/p"); assert.equal(calls, 2);
  status = 500; time = 3000; await cached("/p"); await cached("/p"); assert.equal(calls, 4);
});
