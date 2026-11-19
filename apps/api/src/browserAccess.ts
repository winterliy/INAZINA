// The attempt log of the browser-access pages on the API port (4312), shared with the data service (4313)
// through database/browser-access.log.jsonl (see web_api/security_store.py). One JSON line per attempt:
// when, which service, from where, which id was typed, how it was let in or not. Never a password.
import { appendFileSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const LOG_KEEP = 2000;

export function browserAccess(projectRoot: string) {
  const log = process.env.INAZINA_BROWSER_ACCESS_LOG ?? join(projectRoot, "database", "browser-access.log.jsonl");
  return {
    record(address: string, ok: boolean, loginId = "", via: "login" | "emergency" = "login"): void {
      try {
        appendFileSync(log, JSON.stringify({ at: Math.floor(Date.now() / 1000), service: "api", ip: address.slice(0, 64), ok, loginId: loginId.slice(0, 80), via }) + "\n");
        if (statSync(log).size > 400_000) {
          writeFileSync(log, readFileSync(log, "utf8").split("\n").filter(Boolean).slice(-LOG_KEEP).join("\n") + "\n");
        }
      } catch { /* the log is best effort */ }
    },
  };
}
