// Read-only market data the HTS shows to visitors who have not signed in. Every route here is user-independent on the
// Python side (no x-inazina-user-id is sent) and answers aggregates only: no wallet, order owner or notification.
// Anonymous traffic gets a per-address rate limit and a short shared cache so it cannot multiply database reads.

const SAFE_CODE = "[^/]{1,64}";
const PUBLIC_ROUTES: { pattern: RegExp; internal: (match: RegExpMatchArray, url: URL) => string | null }[] = [
  { pattern: /^\/api\/market\/instruments$/, internal: () => "/internal/market/instruments" },
  { pattern: /^\/api\/market\/exchange-indexes$/, internal: () => "/internal/market/exchange-indexes" },
  { pattern: new RegExp(`^/api/market/quote/(${SAFE_CODE})$`), internal: (m) => `/internal/market/quote/${encodeURIComponent(decodeURIComponent(m[1]))}` },
  { pattern: new RegExp(`^/api/market/book/(${SAFE_CODE})$`), internal: (m) => `/internal/market/book/${encodeURIComponent(decodeURIComponent(m[1]))}` },
  {
    pattern: new RegExp(`^/api/market/chart/(${SAFE_CODE})$`),
    internal: (m, url) => {
      const query = new URLSearchParams({ range: url.searchParams.get("range") ?? "1d" });
      const interval = url.searchParams.get("interval");
      if (interval) query.set("interval", interval);
      for (const key of ["upper", "lower", "catalog"]) { const value = url.searchParams.get(key); if (value) query.set(key, value); }
      return `/internal/market/chart/${encodeURIComponent(decodeURIComponent(m[1]))}?${query}`;
    },
  },
  { pattern: /^\/api\/search-indicators$/, internal: (_m, url) => `/internal/search-indicators?${new URLSearchParams({ search: (url.searchParams.get("search") ?? "").slice(0, 80) })}` },
  {
    pattern: /^\/api\/search-indicator-chart$/,
    internal: (_m, url) => `/internal/search-indicator-chart?${new URLSearchParams({ kind: (url.searchParams.get("kind") ?? "").slice(0, 40), key: (url.searchParams.get("key") ?? "").slice(0, 80) })}`,
  },
];

/** Guest requests allowed per minute and address: INAZINA_GUEST_RATE_PER_MINUTE (a positive whole number), else 240. */
export function guestRatePerMinute(value: string | undefined): number {
  return /^[1-9]\d*$/.test(value ?? "") ? Number(value) : 240;
}

/** The internal path for a guest-readable GET, or null when the route needs a session. */
export function publicMarketPath(method: string, url: URL): string | null {
  if (method !== "GET") return null;
  for (const route of PUBLIC_ROUTES) {
    const match = url.pathname.match(route.pattern);
    if (!match) continue;
    try { return route.internal(match, url); } catch { return null; } // malformed percent-encoding
  }
  return null;
}

/** Fixed-window limiter per address; the map is swept so it cannot grow without bound. */
export function createRateLimiter(limit: number, windowMs: number, now: () => number = Date.now) {
  const hits = new Map<string, { count: number; resetAt: number }>();
  return (key: string): boolean => {
    const time = now();
    if (hits.size > 5000) for (const [k, v] of hits) if (v.resetAt <= time) hits.delete(k);
    const entry = hits.get(key);
    if (!entry || entry.resetAt <= time) { hits.set(key, { count: 1, resetAt: time + windowMs }); return true; }
    entry.count += 1;
    return entry.count <= limit;
  };
}

type Result = { status: number; body: unknown };
/** Same path asked within `ttlMs` shares one answer (and one in-flight fetch); only 200s are kept. */
export function createSharedCache(fetcher: (path: string) => Promise<Result>, ttlMs: number, now: () => number = Date.now, maxEntries = 500) {
  const entries = new Map<string, { at: number; value: Promise<Result> }>();
  return (path: string): Promise<Result> => {
    const time = now();
    const hit = entries.get(path);
    if (hit && time - hit.at < ttlMs) return hit.value;
    if (entries.size >= maxEntries) for (const [k, v] of entries) if (time - v.at >= ttlMs) entries.delete(k);
    if (entries.size >= maxEntries) return fetcher(path); // full of fresh entries: pass through uncached
    const value = fetcher(path).then((result) => { if (result.status !== 200 && entries.get(path)?.value === value) entries.delete(path); return result; },
      (error) => { if (entries.get(path)?.value === value) entries.delete(path); throw error; });
    entries.set(path, { at: time, value });
    return value;
  };
}
