import { execFile } from "node:child_process";
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import { wlSecurity } from "./security-store";

// Wide-Learning viewer. It only READS docs/wide-learning (and runs the read-only
// checker); it never writes there, so the two threads stay the single source of truth.
// (Its one write is the entry login log under database/, below.)
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const docs = join(root, "docs/wide-learning");
const FILES = [
  "README.md", "RULES.md", "WLMD.md", "SOURCES.md", "REVIEW.md", "topics.md", "wide-learning.md", "apply-queue.md",
  "threads/cthread-claude.md", "threads/gthread-codex.md",
  "registry/user.md", "registry/cthread-claude.md", "registry/gthread-codex.md",
];
const python = existsSync(join(root, ".venv/bin/python")) ? join(root, ".venv/bin/python") : "python3";

function run(args: string[]): Promise<string> {
  return new Promise((done) => {
    execFile(python, ["-I", join(root, "tools/wlmd_check.py"), ...args], { cwd: root, timeout: 15_000 },
      (_error, stdout, stderr) => done(`${stdout}${stderr}`.trim()));
  });
}

// Entry password attempts: one JSON line each (time, address, location, verdict, and only the
// LENGTH of what was typed — no password, right or wrong, is ever written down). Read back, newest first, by
// the entry screen's hidden log view. This is the one thing the viewer writes.
// Both files can be moved with an environment variable (used by tests that must not touch the real ones).
const ENTRY_LOG = process.env.INAZINA_WL_ENTRY_LOG ?? join(root, "database/wl-entry-log.jsonl");
const security = wlSecurity(root, process.env.INAZINA_WL_SECURITY_PATH);

// A small JSON body (at most 4 KB), read whole; a bad one answers 400.
function readJson(request: IncomingMessage, response: ServerResponse, done: (body: Record<string, unknown>) => void) {
  let text = "";
  request.on("data", (chunk: Buffer) => { if (text.length < 4096) text += chunk; });
  request.on("end", () => {
    let body: unknown;
    try { body = JSON.parse(text); } catch { return sendJson(response, { error: "bad body" }, 400); }
    if (!body || typeof body !== "object" || Array.isArray(body)) return sendJson(response, { error: "bad body" }, 400);
    done(body as Record<string, unknown>);
  });
}

// The browser's address, as in apps/api: behind a loopback proxy it is the LAST
// X-Forwarded-For entry (earlier ones come from the client and can be forged).
function clientIp(request: IncomingMessage): string {
  const peer = request.socket.remoteAddress ?? "unknown";
  const header = request.headers["x-forwarded-for"];
  const forwarded = (Array.isArray(header) ? header.join(",") : header ?? "").split(",").map((part) => part.trim()).filter(Boolean);
  const loopback = peer === "127.0.0.1" || peer === "::1" || peer === "::ffff:127.0.0.1";
  return (loopback && forwarded.length ? forwarded[forwarded.length - 1] : peer).slice(0, 64);
}

function locate(ip: string): Promise<string> {
  return new Promise((done) => {
    execFile(python, ["-I", join(root, "tools/geoip_lookup.py"), ip], { cwd: root, timeout: 10_000 }, (_error, stdout) => {
      try { done(JSON.parse(stdout)[ip] ?? "알 수 없음"); } catch { done("알 수 없음"); }
    });
  });
}

// Wrong current passwords on the settings tab: five in ten minutes, then it waits.
const misses = new Map<string, { count: number; resetAt: number }>();

function sendJson(response: ServerResponse, body: unknown, status = 200) {
  response.statusCode = status;
  response.setHeader("content-type", "application/json; charset=utf-8");
  response.setHeader("cache-control", "no-store");
  response.end(JSON.stringify(body));
}

function wideLearningData(): Plugin {
  return {
    name: "wide-learning-data",
    configureServer(server) {
      // Push a change notice the moment any watched file is saved (the page re-reads at once).
      server.watcher.add([docs, join(root, "tools/wlmd_check.py")]);
      const notify = (path: string) => {
        if (path.startsWith(docs) || path.endsWith("wlmd_check.py")) server.ws.send({ type: "custom", event: "wl:changed", data: { path } });
      };
      server.watcher.on("change", notify);
      server.watcher.on("add", notify);
      server.watcher.on("unlink", notify);
      server.middlewares.use("/wl-api/files", (_request, response) => {
        const files = Object.fromEntries(FILES.map((name) => {
          const path = join(docs, name);
          return [name, existsSync(path) ? readFileSync(path, "utf8") : ""];
        }));
        response.setHeader("content-type", "application/json; charset=utf-8");
        response.setHeader("cache-control", "no-store");
        response.end(JSON.stringify({ files, readAt: Date.now() }));
      });
      server.middlewares.use("/wl-api/entry-attempt", (request, response) => {
        if (request.method !== "POST") return sendJson(response, { error: "POST only" }, 405);
        let body = "";
        request.on("data", (chunk: Buffer) => { if (body.length < 4096) body += chunk; });
        request.on("end", () => {
          let typed = "";
          try { typed = String(JSON.parse(body).typed ?? "").slice(0, 128); } catch { return sendJson(response, { error: "bad body" }, 400); }
          const pass = security.verifyEntry(typed);
          const ip = clientIp(request);
          void locate(ip).then((location) => {
            appendFileSync(ENTRY_LOG, JSON.stringify({ at: new Date().toISOString(), ip, location, length: [...typed].length, pass }) + "\n");
            sendJson(response, { pass });
          });
        });
      });
      // The hidden login log opens only with the log code (the digits typed last), checked here.
      server.middlewares.use("/wl-api/entry-log", (request, response) => {
        if (request.method !== "POST") return sendJson(response, { error: "POST only" }, 405);
        readJson(request, response, (body) => {
          if (!security.verifyLogCode(String(body.code ?? "").slice(-8))) return sendJson(response, { error: "forbidden" }, 403);
          const lines = existsSync(ENTRY_LOG) ? readFileSync(ENTRY_LOG, "utf8").split("\n").filter(Boolean) : [];
          const entries = lines.slice(-500).reverse().flatMap((line) => { try { return [JSON.parse(line)]; } catch { return []; } });
          sendJson(response, { entries });
        });
      });
      // Settings tab: change the entry password and/or the log code. Needs the current entry password.
      server.middlewares.use("/wl-api/security", (request, response) => {
        if (request.method === "GET") return sendJson(response, security.state());
        if (request.method !== "POST") return sendJson(response, { error: "GET or POST only" }, 405);
        readJson(request, response, (body) => {
          const text = (value: unknown) => (typeof value === "string" && value !== "" ? value : undefined);
          const ip = clientIp(request), miss = misses.get(ip);
          if (miss && miss.resetAt > Date.now() && miss.count >= 5) return sendJson(response, { ok: false, message: "잠시 뒤에 다시 시도해주세요." }, 429);
          try {
            security.change(String(body.current ?? ""), text(body.entryPassword), text(body.logCode));
            sendJson(response, { ok: true, ...security.state() });
            misses.delete(ip);
          } catch (error) {
            const live = miss && miss.resetAt > Date.now();
            misses.set(ip, { count: live ? miss.count + 1 : 1, resetAt: live ? miss.resetAt : Date.now() + 600_000 });
            sendJson(response, { ok: false, message: error instanceof Error ? error.message : "바꾸지 못했습니다." }, 400);
          }
        });
      });
      server.middlewares.use("/wl-api/check", (_request, response) => {
        void Promise.all([run(["--todo", "CLAUDE"]), run(["--todo", "CODEX"])]).then(([claude, codex]) => {
          response.setHeader("content-type", "application/json; charset=utf-8");
          response.setHeader("cache-control", "no-store");
          response.end(JSON.stringify({ claude, codex }));
        });
      });
    },
  };
}

export default defineConfig({
  plugins: [wideLearningData()],
  server: { host: "127.0.0.1", port: 4315, strictPort: true },
  // The entry screen imports Three.js lazily. Bundle it when the server starts, so the
  // first visit does not race a dependency re-optimisation (a failed dynamic import).
  optimizeDeps: {
    include: ["three", "three/addons/effects/AsciiEffect.js", "three/addons/controls/OrbitControls.js",
      "three/addons/loaders/GLTFLoader.js", "three/addons/environments/RoomEnvironment.js"],
  },
});
