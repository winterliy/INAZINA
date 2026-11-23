// What the console's "서비스" page shows about the running system: which services answer, the settings that matter (never a
// secret), and the end of each service's log file (start_all writes them to logs/<service>.log). Read-only.
import { execFile } from "node:child_process";
import { closeSync, existsSync, fstatSync, openSync, readSync } from "node:fs";
import { connect } from "node:net";
import { join } from "node:path";

export const LOG_SERVICES = ["api", "bot", "caddy", "hts", "dashboard", "site", "wl"] as const;
export type LogService = (typeof LOG_SERVICES)[number];

// Colour and cursor codes the dev servers print for a terminal.
const terminalCodes = /\u001b\[[0-9;?]*[ -/]*[@-~]/g;
const clean = (line: string): string => line.replace(terminalCodes, "").replace(/\r/g, "");

export type LogTail = { service: string; missing: boolean; lines: string[] };

// The last `lines` (1..500) lines of a known service's log. Only the end of the file is read (`maxBytes`), and a line cut in half by
// that is dropped. Names other than the known services are refused, so no path can be reached through this.
export function readLog(dir: string, service: string, lines: number, maxBytes = 262_144): LogTail | undefined {
  if (!(LOG_SERVICES as readonly string[]).includes(service)) return undefined;
  const count = Math.min(500, Math.max(1, Math.floor(Number(lines)) || 1));
  const path = join(dir, `${service}.log`);
  if (!existsSync(path)) return { service, missing: true, lines: [] };
  const handle = openSync(path, "r");
  try {
    const size = fstatSync(handle).size;
    const start = Math.max(0, size - maxBytes);
    const buffer = Buffer.alloc(size - start);
    readSync(handle, buffer, 0, buffer.length, start);
    let text = buffer.toString("utf8");
    if (start > 0) text = text.slice(text.indexOf("\n") + 1);
    const all = text.split("\n").map(clean);
    if (all.length && all[all.length - 1] === "") all.pop();
    return { service, missing: false, lines: all.slice(-count) };
  } finally { closeSync(handle); }
}

export function portOpen(port: number, timeoutMs = 300): Promise<boolean> {
  return new Promise((done) => {
    const socket = connect({ host: "127.0.0.1", port });
    const finish = (open: boolean) => { socket.destroy(); done(open); };
    socket.setTimeout(timeoutMs, () => finish(false));
    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
  });
}

// Whether a process whose command line contains `pattern` is running (pgrep -f; the pattern is matched literally).
export function processRunning(pattern: string): Promise<boolean> {
  return new Promise((done) => execFile("pgrep", ["-f", pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")], { timeout: 2000 }, (error) => done(!error)));
}

export type ServiceRow = { name: string; port: number; open: boolean };
export async function serviceStatus(ports: Record<string, number>, check: (port: number) => Promise<boolean> = portOpen): Promise<ServiceRow[]> {
  return Promise.all(Object.entries(ports).map(async ([name, port]) => ({ name, port, open: await check(port) })));
}

// The settings worth looking at. Defaults are shown for the two that always apply; the rest only when set. Nothing else from the
// environment is ever shown (secrets, tokens, database addresses).
const shownSettings = ["INAZINA_PYTHON_WORKERS", "INAZINA_MAX_HTTP_THREADS", "INAZINA_FRONTEND", "INAZINA_WL", "INAZINA_DOMAIN",
  "INAZINA_GUEST_RATE_PER_MINUTE", "INAZINA_READ_CACHE_SECONDS", "INAZINA_DB_BACKEND"] as const;
export function safeSettings(env: NodeJS.ProcessEnv): Record<string, string> {
  const shown: Record<string, string> = { INAZINA_PYTHON_WORKERS: "1", INAZINA_MAX_HTTP_THREADS: "16" };
  for (const key of shownSettings) if (env[key]) shown[key] = String(env[key]);
  return shown;
}
