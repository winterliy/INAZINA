// Small server-side helpers of the Wide-Learning viewer's entry gate (used by vite.config.ts):
// a per-address failure limiter and the session tokens behind the entry cookie.
import { randomBytes, timingSafeEqual } from "node:crypto";

export const SESSION_COOKIE = "wl_session";
export const SESSION_MS = 12 * 3_600_000;

/** `max` failures per `windowMs` for one key (an address); expired entries are dropped on every call. */
export function failureLimiter(max = 5, windowMs = 600_000, now: () => number = Date.now) {
  const misses = new Map<string, { count: number; resetAt: number }>();
  const prune = () => { const t = now(); for (const [key, miss] of misses) if (miss.resetAt <= t) misses.delete(key); };
  return {
    blocked(key: string): boolean { prune(); const miss = misses.get(key); return Boolean(miss && miss.count >= max); },
    fail(key: string): void {
      prune();
      const miss = misses.get(key);
      misses.set(key, miss ? { count: miss.count + 1, resetAt: miss.resetAt } : { count: 1, resetAt: now() + windowMs });
    },
    clear(key: string): void { misses.delete(key); },
    get size() { return misses.size; },
  };
}

/** Random session tokens kept in memory with an expiry; a restart logs everyone out. */
export function sessionStore(ttlMs = SESSION_MS, now: () => number = Date.now) {
  const sessions = new Map<string, number>();
  const prune = () => { const t = now(); for (const [token, expiresAt] of sessions) if (expiresAt <= t) sessions.delete(token); };
  return {
    issue(): string { prune(); const token = randomBytes(32).toString("base64url"); sessions.set(token, now() + ttlMs); return token; },
    valid(token: string | undefined): boolean {
      if (!token) return false;
      prune();
      for (const [known] of sessions) {
        const a = Buffer.from(known), b = Buffer.from(token);
        if (a.length === b.length && timingSafeEqual(a, b)) return true;
      }
      return false;
    },
    get size() { return sessions.size; },
  };
}

export function cookieValue(header: string | undefined, name: string): string | undefined {
  for (const part of (header ?? "").split(";")) {
    const index = part.indexOf("=");
    if (index > 0 && part.slice(0, index).trim() === name) return part.slice(index + 1).trim();
  }
  return undefined;
}

export const sessionCookie = (token: string, ttlMs = SESSION_MS) =>
  `${SESSION_COOKIE}=${token}; HttpOnly; SameSite=Strict; Path=/wl-api; Max-Age=${Math.floor(ttlMs / 1000)}`;
