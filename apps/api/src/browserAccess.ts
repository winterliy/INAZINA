// Browser-access password of the API port (4312) and the attempt log, shared with the Python service
// through database/security.json (see web_api/security_store.py). Read on every attempt, so a change made
// in the dashboard's 보안 tab applies at once. The environment variable / built-in default still verifies
// until the first change.
import { createHash, pbkdf2Sync, timingSafeEqual } from "node:crypto";
import { appendFileSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const DEFAULT_SHA256 = "04b2ad6979a436129b5f30cd00a3804bd87fc8bef73b0bb38640bf49a8479c64";
const LOG_KEEP = 2000;

const same = (left: string, right: string): boolean => {
  const a = Buffer.from(left, "hex"), b = Buffer.from(right, "hex");
  return a.length === b.length && a.length > 0 && timingSafeEqual(a, b);
};

export function browserAccess(projectRoot: string) {
  const store = process.env.INAZINA_SECURITY_PATH ?? join(projectRoot, "database", "security.json");
  const log = process.env.INAZINA_BROWSER_ACCESS_LOG ?? join(projectRoot, "database", "browser-access.log.jsonl");

  const storedHash = (): string => {
    try {
      const entry = (JSON.parse(readFileSync(store, "utf8")) as Record<string, { hash?: unknown }>).api;
      if (entry && typeof entry.hash === "string") return entry.hash;
    } catch { /* no file yet: the environment value or the default applies */ }
    return process.env.INAZINA_BROWSER_PASSWORD_SHA256 || DEFAULT_SHA256;
  };

  return {
    verify(password: string): boolean {
      const stored = storedHash();
      if (stored.startsWith("pbkdf2$")) {
        const [, iterations, salt, digest] = stored.split("$");
        if (!iterations || !salt || !digest) return false;
        return same(pbkdf2Sync(password, Buffer.from(salt, "hex"), Number(iterations), 32, "sha256").toString("hex"), digest);
      }
      return same(createHash("sha256").update(password).digest("hex"), stored);
    },
    /** One JSON line per attempt: when, which service, from where, whether it passed. Never the password. */
    record(address: string, ok: boolean): void {
      try {
        appendFileSync(log, JSON.stringify({ at: Math.floor(Date.now() / 1000), service: "api", ip: address.slice(0, 64), ok }) + "\n");
        if (statSync(log).size > 400_000) {
          writeFileSync(log, readFileSync(log, "utf8").split("\n").filter(Boolean).slice(-LOG_KEEP).join("\n") + "\n");
        }
      } catch { /* the log is best effort */ }
    },
  };
}
