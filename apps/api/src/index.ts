import { createHash, createHmac, timingSafeEqual, randomBytes } from "node:crypto";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { AsyncLocalStorage } from "node:async_hooks";
import { createReadStream, existsSync } from "node:fs";
import { mkdir, unlink, writeFile } from "node:fs/promises";
import { resolve, join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createRateLimiter, createSharedCache, guestRatePerMinute, publicMarketPath } from "./publicMarket.js";
import { LOG_SERVICES, processRunning, readLog, safeSettings, serviceStatus } from "./ops.js";
import { setupOidc } from "./oidc/setup.js";

const projectRoot = findProjectRoot();

try {
  process.loadEnvFile(resolve(projectRoot, ".env"));
} catch (error) {
  if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
}

type Principal = { userId: string; authVersion: number; by?: string };
type User = { userId: string; nickname: string; role: string; nationality: string; cash: number; creditScore: number; authVersion: number };
type SignedPayload = { kind: "session"; userId: string; exp: number; authVersion?: number; by?: string };

const host = "127.0.0.1";
const port = Number(process.env.PORT ?? 4312);
const pythonPort = Number(process.env.INAZINA_PYTHON_PORT ?? 4313);
// Optional extra data-service processes on the following ports, so CPU-bound Python
// work (one GIL per process) runs on several cores. Off by default: a few caches
// (market event targets, browser-password attempt counts) live per process.
const pythonWorkers = Math.max(1, Math.min(8, Number(process.env.INAZINA_PYTHON_WORKERS ?? 1) || 1));
const pythonPorts = Array.from({ length: pythonWorkers }, (_, index) => pythonPort + index);
let nextPythonPort = 0;
const internalToken = randomBytes(32).toString("hex");
const authSecret = process.env.INAZINA_AUTH_SECRET || randomBytes(32).toString("hex");
const sessionCookie = "inazina_session";
const python = existsSync(join(projectRoot, ".venv", "bin", "python"))
  ? join(projectRoot, ".venv", "bin", "python")
  : process.env.PYTHON ?? "python3";
let stopping = false;
let lastPythonFailureLog = 0;
const pythonRestarts = new Map<number, number>();
// The data service is restarted when it exits on its own (a startup failure, a crash),
// so one bad start does not leave every request failing until the whole stack is restarted.
function startPython(workerPort: number): ChildProcess {
  const child = spawn(python, [join(projectRoot, "web_api", "server.py")], {
    cwd: projectRoot,
    env: { ...process.env, INAZINA_INTERNAL_TOKEN: internalToken,
      INAZINA_PYTHON_PORT: String(workerPort) },
    stdio: ["ignore", "inherit", "inherit"],
  });
  child.once("exit", (code, signal) => {
    if (stopping) return;
    const restarts = pythonRestarts.get(workerPort) ?? 0;
    const delay = Math.min(30, 2 ** Math.min(restarts, 5));
    pythonRestarts.set(workerPort, restarts + 1);
    console.error(`INAZINA Python data service exited (code ${code ?? "-"}${signal ? `, ${signal}` : ""}); the error above is the cause. Restarting in ${delay}s.`);
    setTimeout(() => { if (!stopping) pythonProcesses.set(workerPort, startPython(workerPort)); }, delay * 1000);
  });
  return child;
}
const pythonProcesses = new Map(pythonPorts.map((workerPort) => [workerPort, startPython(workerPort)]));

function findProjectRoot(): string {
  let current = dirname(fileURLToPath(import.meta.url));
  while (current !== dirname(current)) {
    if (existsSync(join(current, "web_api", "server.py"))) return current;
    current = dirname(current);
  }
  throw new Error("Could not locate web_api/server.py from the API workspace.");
}

function send(response: ServerResponse, status: number, body: unknown, headers: Record<string, string | string[]> = {}): void {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers });
  response.end(JSON.stringify(body));
}

function signed(value: Omit<SignedPayload, "kind"> & { kind: SignedPayload["kind"] }): string {
  const payload = Buffer.from(JSON.stringify(value)).toString("base64url");
  const signature = createHmac("sha256", authSecret).update(payload).digest("base64url");
  return `${payload}.${signature}`;
}

function verifiedCookie(value: string | undefined): SignedPayload | undefined {
  if (!value) return undefined;
  const [payload, signature] = value.split(".");
  if (!payload || !signature) return undefined;
  const expected = createHmac("sha256", authSecret).update(payload).digest();
  let actual: Buffer;
  try { actual = Buffer.from(signature, "base64url"); } catch { return undefined; }
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) return undefined;
  try {
    const result = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as SignedPayload;
    return result.exp > Math.floor(Date.now() / 1000) ? result : undefined;
  } catch { return undefined; }
}

function cookieValue(request: IncomingMessage, name: string): string | undefined {
  const cookies = request.headers.cookie?.split(";") ?? [];
  const entry = cookies.map((part) => part.trim()).find((part) => part.startsWith(`${name}=`));
  return entry ? decodeURIComponent(entry.slice(name.length + 1)) : undefined;
}

// `maxAge` undefined: a browser-session cookie, gone when the browser is closed.
function cookieHeader(name: string, value: string, maxAge?: number): string {
  const secure = process.env.INAZINA_COOKIE_SECURE === "true" ? "; Secure" : "";
  return `${name}=${encodeURIComponent(value)}; HttpOnly; SameSite=Lax; Path=/${maxAge === undefined ? "" : `; Max-Age=${maxAge}`}${secure}`;
}

// `by`: the administrator who signed in as this country or company account ($login on the web).
// `browserSession`: the cookie is dropped when the browser closes (single sign-on); the signed expiry stays 12 hours either way.
function sessionHeader(userId: string, authVersion: number, by?: string, browserSession = false): string {
  return cookieHeader(sessionCookie, signed({ kind: "session", userId, authVersion, ...(by ? { by } : {}), exp: Math.floor(Date.now() / 1000) + 43_200 }), browserSession ? undefined : 43_200);
}
// The administrator behind the current request's delegated session, forwarded to the Python service.
const delegation = new AsyncLocalStorage<string>();

function principalFor(request: IncomingMessage): Principal | undefined {
  const session = verifiedCookie(cookieValue(request, sessionCookie));
  if (session?.kind === "session" && typeof session.userId === "string" &&
      session.userId.length > 0 && session.userId.length <= 120 && !/[\u0000-\u001f]/.test(session.userId) &&
      typeof session.authVersion === "number" && Number.isInteger(session.authVersion)) {
    return { userId: session.userId, authVersion: session.authVersion, ...(typeof session.by === "string" && session.by ? { by: session.by } : {}) };
  }
  return undefined;
}

async function internalRequest(
  method: "GET" | "POST",
  path: string,
  userId?: string,
  body?: unknown,
  timeoutMs = 10_000,
): Promise<{ status: number; body: any }> {
  try {
    const workerPort = pythonPorts[nextPythonPort++ % pythonPorts.length];
    const result = await fetch(`http://127.0.0.1:${workerPort}${path}`, {
      method,
      headers: {
        "x-inazina-internal-token": internalToken,
        ...(userId ? { "x-inazina-user-id": encodeURIComponent(userId) } : {}),
        ...(userId && delegation.getStore() ? { "x-inazina-delegated-by": encodeURIComponent(delegation.getStore()!) } : {}),
        ...(body ? { "content-type": "application/json" } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const responseText = await result.text();
    try {
      const parsed = JSON.parse(responseText);
      // During a restart the old data service may still answer with the previous token. That is the
      // service being unavailable, not the user's session ending, so browsers must not sign out.
      if (result.status === 401 && parsed?.error === "internal_authentication_required") {
        console.error(`Python service rejected the internal token for ${method} ${path}; is an old data service still running?`);
        return { status: 503, body: { error: "python_service_unavailable" } };
      }
      return { status: result.status, body: parsed };
    } catch {
      console.error(`Python service returned non-JSON for ${method} ${path} (${result.status}): ${responseText.slice(0, 500)}`);
      return { status: 503, body: { error: "python_service_unavailable" } };
    }
  } catch (error) {
    // While the data service is down every request fails the same way; say so once a minute.
    if (Date.now() - lastPythonFailureLog > 60_000) {
      lastPythonFailureLog = Date.now();
      console.error(`Python service request failed for ${method} ${path} (further failures muted for 60s):`, error instanceof Error ? error.cause ?? error.message : error);
    }
    return { status: 503, body: { error: "python_service_unavailable" } };
  }
}

const internalGet = (path: string, userId?: string) => internalRequest("GET", path, userId);
// The browser's address. Behind the local Vite proxy (loopback peer) it is the
// LAST X-Forwarded-For entry: the proxy appends the address it saw, while every
// earlier entry came from the client and can be forged. Any other peer is used
// as is. Re-check this if another proxy is ever placed in front of Vite.
function clientIp(request: IncomingMessage): string {
  const peer = request.socket.remoteAddress ?? "unknown";
  const header = request.headers["x-forwarded-for"];
  const forwarded = (Array.isArray(header) ? header.join(",") : header ?? "").split(",").map((part) => part.trim()).filter(Boolean);
  const loopback = peer === "127.0.0.1" || peer === "::1" || peer === "::ffff:127.0.0.1";
  return (loopback && forwarded.length ? forwarded[forwarded.length - 1] : peer).slice(0, 64);
}

// Board attachments live on disk; the Python service keeps their records
// (web_api/board.py). Same directory and limits on both sides.
const boardFilesDir = resolve(process.env.INAZINA_BOARD_FILES_DIR ?? join(projectRoot, "database", "board_files"));
const boardFileMaxBytes = 10 * 1024 * 1024;
const boardFileTypes: Record<string, string> = {
  pdf: "application/pdf", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp",
  txt: "text/plain", csv: "text/csv", zip: "application/zip",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  hwp: "application/x-hwp", hwpx: "application/x-hwp",
};

async function requestBytes(request: IncomingMessage, limit: number): Promise<Buffer> {
  if (Number(request.headers["content-length"] ?? 0) > limit) throw new Error("request_too_large");
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new Error("request_too_large");
    chunks.push(Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}

// A post's view count rises at most once per address every 10 minutes.
const boardViews = new Map<string, number>();
function countBoardView(ip: string, postId: string): boolean {
  const now = Date.now();
  if (boardViews.size > 20_000) for (const [key, time] of boardViews) if (now - time > 10 * 60_000) boardViews.delete(key);
  const key = `${ip}:${postId}`;
  const last = boardViews.get(key);
  if (last && now - last < 10 * 60_000) return false;
  boardViews.set(key, now);
  return true;
}

// Site form submissions per client address (at most 5 per 10 minutes).
const feedbackSubmissions = new Map<string, number[]>();
// Guest market reads (no session): INAZINA_GUEST_RATE_PER_MINUTE requests per minute per address (default 240), identical requests share one answer for 1 s.
const guestMarketAllowed = createRateLimiter(guestRatePerMinute(process.env.INAZINA_GUEST_RATE_PER_MINUTE), 60_000);
const guestMarketRead = createSharedCache((path) => internalGet(path), 1000);

async function requestJson(request: IncomingMessage, limit = 32_768): Promise<unknown> {
  if (!request.headers["content-type"]?.includes("application/json")) {
    throw new Error("application_json_required");
  }
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new Error("request_too_large");
    chunks.push(Buffer.from(chunk));
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new Error("invalid_json");
  }
}

async function handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
  const url = new URL(request.url ?? "/", `http://${host}:${port}`);

  if (oidc.rp && await oidc.rp.handle(request, response, url)) return; // single sign-on sign-in for the app hosts

  if (request.method === "GET" && url.pathname === "/api/health") {
    const health = await internalGet("/internal/health");
    send(response, health.status === 200 ? 200 : 503, {
      status: health.status === 200 ? "ok" : "degraded",
      service: "inazina-api",
      python: health.status === 200 ? "connected" : "unavailable",
    });
    return;
  }

  if (request.method !== "GET" && request.method !== "POST") {
    send(response, 405, { error: "method_not_allowed" });
    return;
  }

  // Official site (apps/site): public Contact/Report forms, shown in the dashboard.
  if (request.method === "POST" && url.pathname === "/api/site/feedback") {
    const key = request.socket.remoteAddress ?? "local";
    const now = Date.now();
    const recent = (feedbackSubmissions.get(key) ?? []).filter((time) => now - time < 10 * 60_000);
    if (recent.length >= 5) {
      send(response, 429, { error: "feedback_rate_limited" });
      return;
    }
    const result = await internalRequest("POST", "/internal/site/feedback", undefined, await requestJson(request));
    if (result.status === 201) feedbackSubmissions.set(key, [...recent, now]);
    send(response, result.status, result.body);
    return;
  }
  // HTS board: anyone may read; the session (if any) only marks the reader's own posts.
  // Board files download for anyone who can read the post; always as a download, never rendered here.
  const boardFile = request.method === "GET" ? url.pathname.match(/^\/api\/board\/files\/(\d+)$/) : null;
  if (boardFile) {
    // The session (if any) decides access to files on secret boards.
    const meta = await internalGet(`/internal/board/files/${boardFile[1]}`, principalFor(request)?.userId);
    const path = meta.status === 200 && /^[a-f0-9]{32}\.[a-z0-9]{1,5}$/.test(meta.body.storedName ?? "") ? join(boardFilesDir, meta.body.storedName) : "";
    if (!path || !existsSync(path)) {
      send(response, 404, { error: "not_found" });
      return;
    }
    response.writeHead(200, {
      "content-type": meta.body.mime,
      "content-length": String(meta.body.size),
      "content-disposition": `attachment; filename*=UTF-8''${encodeURIComponent(meta.body.name)}`,
      "x-content-type-options": "nosniff",
      "content-security-policy": "sandbox",
      "cache-control": "private, no-store",
    });
    createReadStream(path).pipe(response);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/board/boards") {
    const result = await internalGet("/internal/board/boards", principalFor(request)?.userId);
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "GET" && (url.pathname === "/api/board/posts" || /^\/api\/board\/posts\/\d+$/.test(url.pathname))) {
    const viewer = principalFor(request)?.userId;
    const postId = url.pathname.split("/")[4];
    const query = new URLSearchParams();
    for (const key of ["board", "category", "search", "asset", "page"]) {
      const value = url.searchParams.get(key);
      if (value) query.set(key, value);
    }
    if (postId && countBoardView(clientIp(request), postId)) query.set("view", "1");
    const result = await internalGet(`/internal/board/posts${postId ? `/${postId}` : ""}?${query}`, viewer);
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "POST" && url.pathname === "/api/auth/logout") {
    const session = verifiedCookie(cookieValue(request, sessionCookie));
    if (session?.kind === "session" && session.by) {
      // Leaving a country or company account returns to the administrator who signed in as it.
      const admin = await delegation.exit(() => internalGet("/internal/me", session.by));
      if (admin.status === 200 && admin.body.user?.role === "admin") {
        send(response, 200, { success: true, returnedTo: admin.body.user }, { "set-cookie": sessionHeader(admin.body.user.userId, admin.body.user.authVersion) });
        return;
      }
    } else if (session?.kind === "session") {
      await internalRequest("POST", "/internal/auth/logout", session.userId, {});
    }
    const logoutUrl = oidc.rp ? await oidc.rp.logoutUrl(request.headers.host ?? "") : undefined; // also ends the single sign-on session
    send(response, 200, { success: true, ...(logoutUrl ? { logoutUrl } : {}) }, { "set-cookie": cookieHeader(sessionCookie, "", 0) });
    return;
  }

  const principal = principalFor(request);
  const guestPath = !principal ? publicMarketPath(request.method ?? "GET", url) : null;
  if (guestPath) {
    // Read-only market data for visitors without a session (guest HTS); nothing here depends on who asks.
    if (!guestMarketAllowed(clientIp(request))) { send(response, 429, { error: "rate_limited" }, { "retry-after": "60" }); return; }
    const result = await guestMarketRead(guestPath);
    send(response, result.status, result.body);
    return;
  }
  if (!principal) {
    send(response, 401, { error: "authentication_required" });
    return;
  }

  const identity = await internalGet("/internal/me", principal.userId);
  if (identity.status !== 200) {
    send(response, identity.status, identity.body);
    return;
  }
  const user = identity.body.user as User;
  if (principal.authVersion !== undefined && user.authVersion !== principal.authVersion) {
    send(response, 401, { error: "session_expired" }, { "set-cookie": cookieHeader(sessionCookie, "", 0) });
    return;
  }

  if (request.method === "GET" && (url.pathname === "/api/auth/session" || url.pathname === "/api/me")) {
    send(response, 200, { user });
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/me/ownership-graph") {
    const result = await internalGet("/internal/me/ownership-graph", principal.userId);
    send(response, result.status, result.body);
    return;
  }
  // The console's 서비스 page (read-only, administrators only): which services answer, the settings that matter, and the end of the logs.
  if (request.method === "GET" && (url.pathname === "/api/admin/ops" || url.pathname === "/api/admin/ops/log")) {
    if (user.role !== "admin" || principal.by) {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const logDir = join(projectRoot, "logs");
    if (url.pathname === "/api/admin/ops/log") {
      const tail = readLog(logDir, url.searchParams.get("service") ?? "", Number(url.searchParams.get("lines") ?? 200));
      if (tail) send(response, 200, tail); else send(response, 400, { error: "unknown_service" });
      return;
    }
    const dataPorts = Object.fromEntries(pythonPorts.map((workerPort, index) => [pythonWorkers > 1 ? `data-${index + 1}` : "data", workerPort]));
    send(response, 200, {
      api: { pid: process.pid, uptimeSeconds: Math.round(process.uptime()), node: process.version, mode: import.meta.url.includes("/dist/") ? "build" : "watch" },
      services: await serviceStatus({ api: port, ...dataPorts, caddy: 2019, dashboard: 4310, hts: 4311, site: 4314, wl: 4315 }),
      bot: { running: await processRunning(join(projectRoot, "run.py")) },
      settings: safeSettings(process.env),
      logs: { dir: "logs", services: LOG_SERVICES },
    });
    return;
  }
  if (request.method === "POST" && url.pathname === "/api/admin/login-as") {
    if (user.role !== "admin" || principal.by) {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalRequest("POST", "/internal/admin/login-as", principal.userId, await requestJson(request));
    if (result.status === 200 && result.body.userId) {
      send(response, 200, result.body, { "set-cookie": sessionHeader(result.body.userId, user.authVersion, user.userId) });
    } else send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/hts/account" && request.method === "GET") {
    const result = await internalGet("/internal/hts/account", principal.userId);
    send(response, result.status, result.body);
    return;
  }
  if (url.pathname === "/api/hts/commands") {
    const result = request.method === "GET"
      ? await internalGet("/internal/hts/commands", principal.userId)
      : await internalRequest("POST", "/internal/hts/commands", principal.userId, await requestJson(request), 30_000);
    send(response, result.status, result.body);
    return;
  }
  // 주주총회 and 회생·파산: proxied as-is to the Python service.
  if (url.pathname === "/api/governance/meetings" || url.pathname === "/api/court/filings" || /^\/api\/governance\/meetings\/\d+\/(respond|close)$/.test(url.pathname)) {
    const internal = url.pathname.replace(/^\/api\//, "/internal/");
    const result = request.method === "GET"
      ? await internalGet(internal, principal.userId)
      : await internalRequest("POST", internal, principal.userId, await requestJson(request), 30_000);
    send(response, result.status, result.body);
    return;
  }
  if (url.pathname === "/api/hts/password" && request.method === "POST") {
    const result = await internalRequest("POST", "/internal/hts/password", principal.userId, await requestJson(request));
    // The change bumps the credential's auth version; reissue this browser's session so it stays signed in.
    if (result.status === 200 && result.body.user) send(response, 200, { success: true }, { "set-cookie": sessionHeader(result.body.user.userId, result.body.user.authVersion) });
    else send(response, result.status, result.body);
    return;
  }
  if (url.pathname === "/api/hts/account" && request.method === "POST") {
    const body = await requestJson(request);
    const result = await internalRequest("POST", "/internal/hts/account", principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && (url.pathname === "/api/market/instruments" || url.pathname === "/api/admin/instruments")) {
    if (url.pathname.startsWith("/api/admin/") && user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalGet(url.pathname === "/api/admin/instruments"
      ? "/internal/admin/instruments?includeInactive=1"
      : "/internal/market/instruments", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/market/exchange-indexes") {
    const result = await internalGet("/internal/market/exchange-indexes", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/admin/overview") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalGet("/internal/admin/overview", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/admin/login-history") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const target = url.searchParams.get("user");
    const result = await internalGet(`/internal/admin/login-history${target ? `?user=${encodeURIComponent(target)}` : ""}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/admin/audit") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalGet("/internal/admin/audit", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  const drVotingMeetingMatch = url.pathname.match(/^\/api\/admin\/dr\/voting-meetings(?:\/(\d+))?$/);
  if (drVotingMeetingMatch) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const meetingId = drVotingMeetingMatch[1];
    const result = request.method === "GET" && !meetingId
      ? await internalGet(`/internal/admin/dr/voting-meetings?limit=${encodeURIComponent(url.searchParams.get("limit") ?? "100")}`, principal.userId)
      : request.method === "GET" && meetingId
      ? await internalGet(`/internal/admin/dr/voting-meetings/${meetingId}`, principal.userId)
      : request.method === "POST" && !meetingId
        ? await internalRequest("POST", "/internal/admin/dr/voting-meetings", principal.userId,
          await requestJson(request))
        : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/dr/voting-agenda") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "POST"
      ? await internalRequest("POST", "/internal/admin/dr/voting-agenda", principal.userId,
        await requestJson(request))
      : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/dr/voting-window") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "POST"
      ? await internalRequest("POST", "/internal/admin/dr/voting-window", principal.userId,
        await requestJson(request))
      : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/dr/voting-results") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const query = new URLSearchParams({ meetingId: url.searchParams.get("meetingId") ?? "" });
    const agendaItemId = url.searchParams.get("agendaItemId");
    if (agendaItemId) query.set("agendaItemId", agendaItemId);
    const result = request.method === "GET"
      ? await internalGet(`/internal/admin/dr/voting-results?${query}`, principal.userId)
      : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/dr/custody-instructions") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "GET"
      ? await internalGet("/internal/admin/dr/custody-instructions", principal.userId)
      : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/dr/custody-reconciliation") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "GET"
      ? await internalGet("/internal/admin/dr/custody-reconciliation", principal.userId)
      : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/dr/dividend-tax") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "GET"
      ? await internalGet("/internal/admin/dr/dividend-tax", principal.userId)
      : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/dr/dividend-tax/rules") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "POST"
      ? await internalRequest("POST", "/internal/admin/dr/dividend-tax/rules", principal.userId,
        await requestJson(request))
      : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  const drCustodyActionMatch = url.pathname.match(/^\/api\/admin\/dr\/custody-instructions\/(settle|cancel|reject)$/);
  if (drCustodyActionMatch) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "POST"
      ? await internalRequest("POST", `/internal/admin/dr/custody-instructions/${drCustodyActionMatch[1]}`,
        principal.userId, await requestJson(request))
      : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/admin/users") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const search = encodeURIComponent(url.searchParams.get("search") ?? "");
    const kind = url.searchParams.get("kind") === "company" ? "&kind=company" : "";
    const result = await internalGet(`/internal/admin/users?search=${search}${kind}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/accounting-close" && (request.method === "GET" || request.method === "POST")) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "GET"
      ? await internalRequest("GET", `/internal/admin/accounting-close${url.search}`, principal.userId, undefined, 120_000)
      : await internalRequest("POST", "/internal/admin/accounting-close", principal.userId, await requestJson(request));
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/controls" && request.method === "GET") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalGet("/internal/admin/controls", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  const liquidityDetail = url.pathname.match(/^\/api\/admin\/liquidity\/([^/]+)$/);
  if (liquidityDetail && request.method === "GET") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalGet(`/internal/admin/liquidity/${encodeURIComponent(decodeURIComponent(liquidityDetail[1]))}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/liquidity" && (request.method === "GET" || request.method === "POST")) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "GET"
      ? await internalGet("/internal/admin/liquidity", principal.userId)
      : await internalRequest("POST", "/internal/admin/liquidity", principal.userId, await requestJson(request));
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/admin/events") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalGet("/internal/admin/events", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  // HTS refreshes the account every 15 s; one request instead of six saves the
  // per-request session check and five browser round trips. Each part keeps its
  // own status so an optional panel (credit, tax residency) never fails the rest.
  if (request.method === "GET" && url.pathname === "/api/me/account-bundle") {
    const parts = {
      portfolio: "/internal/market/portfolio",
      orders: "/internal/market/orders",
      loans: "/internal/me/loan-schedules",
      conversions: "/internal/me/dr/conversions",
      credit: "/internal/me/credit",
      taxResidency: "/internal/me/tax-residency",
    } as const;
    const results = await Promise.all(Object.values(parts).map((path) => internalGet(path, principal.userId)));
    send(response, 200, Object.fromEntries(Object.keys(parts).map((name, index) =>
      [name, { status: results[index].status, body: results[index].body }])));
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/me/portfolio") {
    const result = await internalGet("/internal/market/portfolio", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  // Personal statistics (always computed at today's exchange rates, so no caching).
  if (request.method === "GET" && url.pathname === "/api/me/stats") {
    const result = await internalGet(`/internal/me/stats${url.search}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }

  // Bell notifications and payout choices (the receiver's decision on a payment with no account to credit).
  if (url.pathname.startsWith("/api/me/notifications") || url.pathname.startsWith("/api/me/payouts")) {
    const internalPath = url.pathname.replace("/api/", "/internal/");
    const result = request.method === "GET"
      && (internalPath === "/internal/me/notifications" || internalPath === "/internal/me/payouts")
      ? await internalGet(internalPath, principal.userId)
      : request.method === "POST" && internalPath.split("/").length === 5
        ? await internalRequest("POST", internalPath, principal.userId, await requestJson(request))
        : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  // Self-service withdrawal: the member's property passes to the national treasury, then this session and the single sign-on end.
  if (request.method === "POST" && url.pathname === "/api/me/withdraw") {
    if (principal.by) {
      send(response, 403, { error: "active_user_required" });
      return;
    }
    const result = await internalRequest("POST", "/internal/me/withdraw", principal.userId, await requestJson(request));
    if (result.status !== 200) {
      send(response, result.status, result.body);
      return;
    }
    const logoutUrl = oidc.rp ? await oidc.rp.logoutUrl(request.headers.host ?? "") : undefined;
    send(response, 200, { success: true, ...(logoutUrl ? { logoutUrl } : {}) }, { "set-cookie": cookieHeader(sessionCookie, "", 0) });
    return;
  }

  if (url.pathname === "/api/me/tax-residency") {
    const result = request.method === "GET"
      ? await internalGet("/internal/me/tax-residency", principal.userId)
      : request.method === "POST"
        ? await internalRequest("POST", "/internal/me/tax-residency", principal.userId,
          await requestJson(request))
        : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/me/dr/movements") {
    const result = await internalGet("/internal/me/dr/movements", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/me/dr/conversions") {
    const result = await internalGet("/internal/me/dr/conversions", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/me/dr/voting-delegations") {
    const result = await internalGet("/internal/me/dr/voting-delegations", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/me/dr/proxy-voting-delegations") {
    const result = await internalGet("/internal/me/dr/proxy-voting-delegations", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/me/dr/voting-agenda") {
    const result = await internalGet("/internal/me/dr/voting-agenda", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/me/credit") {
    const result = await internalGet("/internal/me/credit", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/me/loan-schedules") {
    const result = await internalGet("/internal/me/loan-schedules", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/search-indicators") {
    const query = new URLSearchParams({ search: url.searchParams.get("search") ?? "" });
    const result = await internalGet(`/internal/search-indicators?${query}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/search-indicator-chart") {
    const query = new URLSearchParams({
      kind: url.searchParams.get("kind") ?? "",
      key: url.searchParams.get("key") ?? "",
    });
    const result = await internalGet(`/internal/search-indicator-chart?${query}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "POST" && /^\/api\/admin\/companies\/[^/]+\/share-actions$/.test(url.pathname)) {
    if (user.role !== "admin") { send(response, 403, { error: "admin_required" }); return; }
    const result = await internalRequest("POST", url.pathname.replace(/^\/api\//, "/internal/"), principal.userId, await requestJson(request));
    send(response, result.status, result.body);
    return;
  }
  const infoMatch = url.pathname.match(/^\/api\/info\/(panel)$/);
  if (request.method === "GET" && infoMatch) {
    const query = new URLSearchParams();
    for (const key of ["category", "page"]) { const value = url.searchParams.get(key); if (value) query.set(key, value); }
    const result = await internalGet(`/internal/info/${infoMatch[1]}?${query}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/me/company-financial") {
    const period = url.searchParams.get("period");
    const result = await internalGet(`/internal/me/company-financial${period ? `?period=${encodeURIComponent(period)}` : ""}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/me/history") {
    const result = await internalGet("/internal/me/history", principal.userId);
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/me/wallet-history") {
    const query = new URLSearchParams({
      sourceType: url.searchParams.get("sourceType") ?? "",
      sourceAccount: url.searchParams.get("sourceAccount") ?? "",
    });
    const result = await internalGet(`/internal/me/wallet-history?${query}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/me/dividend-payments") {
    const result = await internalGet("/internal/me/dividend-payments", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/market/orders") {
    const result = await internalGet("/internal/market/orders", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  const quoteMatch = url.pathname.match(/^\/api\/market\/quote\/([^/]+)$/);
  if (request.method === "GET" && quoteMatch) {
    const result = await internalGet(`/internal/market/quote/${encodeURIComponent(decodeURIComponent(quoteMatch[1]))}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }

  const bookMatch = url.pathname.match(/^\/api\/market\/book\/([^/]+)$/);
  if (request.method === "GET" && bookMatch) {
    const result = await internalGet(`/internal/market/book/${encodeURIComponent(decodeURIComponent(bookMatch[1]))}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }

  const chartMatch = url.pathname.match(/^\/api\/market\/chart\/([^/]+)$/);
  if (request.method === "GET" && chartMatch) {
    const query = new URLSearchParams({ range: url.searchParams.get("range") ?? "1d" });
    const interval = url.searchParams.get("interval");
    if (interval) query.set("interval", interval);
    for (const key of ["upper", "lower", "catalog"]) { const value = url.searchParams.get(key); if (value) query.set(key, value); }
    const result = await internalGet(`/internal/market/chart/${encodeURIComponent(decodeURIComponent(chartMatch[1]))}?${query}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }

  const drawingsMatch = url.pathname.match(/^\/api\/market\/drawings\/([^/]+)$/);
  if (drawingsMatch) {
    const path = `/internal/market/drawings/${encodeURIComponent(decodeURIComponent(drawingsMatch[1]))}`;
    const result = request.method === "GET"
      ? await internalGet(path, principal.userId)
      : await internalRequest("POST", path, principal.userId, await requestJson(request));
    send(response, result.status, result.body);
    return;
  }

  const dbMatch = url.pathname.match(/^\/api\/admin\/db\/tables(?:\/([^/]+)(\/rows(?:\/(\d+)(\/delete)?)?)?)?$/);
  if (dbMatch) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const [, table, rows, rowId, remove] = dbMatch;
    const tablePath = table ? `/${encodeURIComponent(decodeURIComponent(table))}` : "";
    let result: { status: number; body: any };
    if (request.method === "GET" && !rows) {
      const query = new URLSearchParams({ search: url.searchParams.get("search") ?? "", offset: url.searchParams.get("offset") ?? "0" });
      result = await internalGet(`/internal/admin/db/tables${tablePath}${table ? `?${query}` : ""}`, principal.userId);
    } else if (request.method === "POST" && table && rows) {
      const body = remove ? {} : await requestJson(request);
      result = await internalRequest("POST", `/internal/admin/db/tables${tablePath}/rows${rowId ? `/${rowId}${remove ? "/delete" : ""}` : ""}`, principal.userId, body);
    } else {
      result = { status: 405, body: { error: "method_not_allowed" } };
    }
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/commands") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "GET"
      ? await internalGet("/internal/admin/commands", principal.userId)
      // Bulk commands such as basic income touch every account and can outlast ordinary reads.
      : await internalRequest("POST", "/internal/admin/commands", principal.userId, await requestJson(request), 60_000);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/market/orders") {
    const body = await requestJson(request);
    const result = await internalRequest("POST", "/internal/market/orders", principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/dr/convert") {
    const body = await requestJson(request);
    const result = await internalRequest("POST", "/internal/dr/convert", principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/dr/convert/cancel") {
    const result = await internalRequest("POST", "/internal/dr/convert/cancel", principal.userId,
      await requestJson(request));
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/dr/voting-delegations") {
    const result = await internalRequest("POST", "/internal/dr/voting-delegations", principal.userId,
      await requestJson(request));
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/dr/votes") {
    const result = await internalRequest("POST", "/internal/dr/votes", principal.userId,
      await requestJson(request));
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname.startsWith("/api/dr/voting-delegations/") &&
      url.pathname.endsWith("/revoke")) {
    const result = await internalRequest("POST", "/internal/dr/voting-delegations/revoke",
      principal.userId, await requestJson(request));
    send(response, result.status, result.body);
    return;
  }

  const cancelMatch = url.pathname.match(/^\/api\/market\/orders\/(\d+)\/cancel$/);
  if (request.method === "POST" && cancelMatch) {
    const result = await internalRequest("POST", `/internal/market/orders/${cancelMatch[1]}/cancel`, principal.userId, {});
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/admin/market") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const body = await requestJson(request);
    const result = await internalRequest("POST", "/internal/admin/market", principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/admin/events") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const body = await requestJson(request);
    const result = await internalRequest("POST", "/internal/admin/events", principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  const fundMatch = url.pathname.match(/^\/api\/admin\/funds\/([^/]+)$/);
  if (fundMatch) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const path = `/internal/admin/funds/${encodeURIComponent(decodeURIComponent(fundMatch[1]))}`;
    const result = request.method === "GET"
      ? await internalGet(path, principal.userId)
      : request.method === "POST"
        ? await internalRequest("POST", path, principal.userId, await requestJson(request))
        : { status: 405, body: { error: "method_not_allowed" } };
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/admin/funds") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const body = await requestJson(request);
    const result = await internalRequest("POST", "/internal/admin/funds", principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/admin/users/country-account") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const body = await requestJson(request);
    const result = await internalRequest("POST", "/internal/admin/users/country-account", principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  const userMatch = url.pathname.match(/^\/api\/admin\/users\/([^/]+)$/);
  if (request.method === "POST" && userMatch) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const body = await requestJson(request);
    const targetId = encodeURIComponent(decodeURIComponent(userMatch[1]));
    const result = await internalRequest("POST", `/internal/admin/users/${targetId}`, principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  const statusMatch = url.pathname.match(/^\/api\/admin\/instruments\/([^/]+)\/status$/);
  if (request.method === "POST" && statusMatch) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const code = encodeURIComponent(decodeURIComponent(statusMatch[1]));
    const body = await requestJson(request);
    const result = await internalRequest("POST", `/internal/admin/instruments/${code}/status`, principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  const instrumentMatch = url.pathname.match(/^\/api\/admin\/instruments\/([^/]+)$/);
  if (request.method === "POST" && instrumentMatch) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const code = encodeURIComponent(decodeURIComponent(instrumentMatch[1]));
    const body = await requestJson(request);
    const result = await internalRequest("POST", `/internal/admin/instruments/${code}`, principal.userId, body);
    send(response, result.status, result.body);
    return;
  }

  // Board file upload (any signed-in writer): raw bytes, name in X-File-Name. The file is
  // written first, then registered; a refused registration removes it again.
  if (request.method === "POST" && url.pathname === "/api/board/files") {
    let name = "";
    try { name = decodeURIComponent(String(request.headers["x-file-name"] ?? "")).trim(); } catch { /* invalid encoding */ }
    const extension = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
    if (!name || name.length > 120 || !boardFileTypes[extension]) {
      send(response, 400, { error: `올릴 수 있는 파일: ${Object.keys(boardFileTypes).sort().join(", ")}` });
      return;
    }
    const bytes = await requestBytes(request, boardFileMaxBytes).catch(() => null);
    if (!bytes || !bytes.length) {
      send(response, 400, { error: "파일은 10MB 이하만 올릴 수 있습니다." });
      return;
    }
    const storedName = `${randomBytes(16).toString("hex")}.${extension}`;
    await mkdir(boardFilesDir, { recursive: true });
    await writeFile(join(boardFilesDir, storedName), bytes, { flag: "wx" });
    const result = await internalRequest("POST", "/internal/board/files", principal.userId,
      { name, storedName, size: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") });
    if (result.status !== 200) await unlink(join(boardFilesDir, storedName)).catch(() => undefined);
    send(response, result.status, result.status === 200 ? { ...result.body, name, size: bytes.length } : result.body);
    return;
  }

  // HTS board writes: the author's address is recorded with every post, comment and report.
  if (request.method === "POST" && /^\/api\/board\/(posts|posts\/\d+\/(edit|delete|comments|react)|comments\/\d+\/(delete|react)|polls\/\d+\/(vote|close|reopen)|reports)$/.test(url.pathname)) {
    const body = await requestJson(request);
    const result = await internalRequest("POST", url.pathname.replace(/^\/api\//, "/internal/"), principal.userId,
      { ...(body && typeof body === "object" ? body : {}), clientIp: clientIp(request) });
    send(response, result.status, result.body);
    return;
  }
  // Public documents (terms, privacy policy, rules, help, registration notice): read, preview, confirmed save.
  if (url.pathname === "/api/admin/documents" || /^\/api\/admin\/documents\/[a-z]+(\/(preview|cancel))?$/.test(url.pathname)) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const internal = url.pathname.replace(/^\/api\//, "/internal/");
    const result = request.method === "GET"
      ? await internalGet(internal, principal.userId)
      : await internalRequest("POST", internal, principal.userId, await requestJson(request, 262_144));
    send(response, result.status, result.body);
    return;
  }

  // Moderation and board structure: post/comment/report/category take a number ("new" to create),
  // board takes its ID.
  if (url.pathname === "/api/admin/board" || /^\/api\/admin\/board\/((post|comment|report|category|poll)\/(\d+|new)|board\/[a-z0-9_-]{1,32}|search\/[^/]{1,300})$/.test(url.pathname)) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = request.method === "GET"
      ? await internalGet(`/internal/admin/board${url.search}`, principal.userId)
      : await internalRequest("POST", url.pathname.replace(/^\/api\//, "/internal/"), principal.userId, await requestJson(request));
    send(response, result.status, result.body);
    return;
  }

  // Product, country, exchange, user-detail, help and bot-job administration.
  const adminMatch = url.pathname.match(/^\/api\/admin\/((?:products|countries|exchanges|help|bot-jobs|user|companies|organizations|warnings|feedback|court|naturalization|banks|logs|compliance|bank-journal|alm|funding|rate-rules|recovery-waterfall|deposit-insurance|dr|security)(?:\/[^/]+){0,2})$/);
  if (adminMatch) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const path = `/internal/admin/${adminMatch[1].split("/").map((part) => encodeURIComponent(decodeURIComponent(part))).join("/")}${url.search}`;
    const result = request.method === "GET"
      ? await internalGet(path, principal.userId)
      : await internalRequest("POST", path, principal.userId, await requestJson(request), 60_000);
    send(response, result.status, result.body);
    return;
  }

  send(response, 404, { error: "not_found" });
}

// Single sign-on (docs/sso.md): with INAZINA_OIDC_ISSUER set, the identity provider answers its own host first.
const oidc = await setupOidc({ env: process.env, jwksFile: join(projectRoot, "database", "oidc_jwks.json"), authSecret, internalRequest, clientIp,
  uiDir: join(projectRoot, "apps", "id", "dist"), issueSession: (userId, authVersion) => sessionHeader(userId, authVersion, undefined, true) });

const server = createServer((request, response) => {
  const by = principalFor(request)?.by;
  void (async () => {
    if (oidc.idp && await oidc.idp(request, response)) return;
    await (by ? delegation.run(by, () => handle(request, response)) : handle(request, response));
  })().catch((error: unknown) => {
    const message = error instanceof Error ? error.message : "internal_error";
    if (message === "invalid_json" || message === "application_json_required") {
      send(response, 400, { error: message });
    } else if (message === "request_too_large") {
      send(response, 413, { error: message });
    } else {
      console.error("API request failed:", error);
      if (!response.headersSent) send(response, 500, { error: "internal_error" });
      else response.destroy();
    }
  });
});

server.listen(port, host, () => {
  console.log(`INAZINA API listening at http://${host}:${port}`);
});

function shutdown(): void {
  stopping = true;
  server.close();
  for (const child of pythonProcesses.values()) child.kill("SIGTERM");
}

process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);
// Any other way out (a crash, process.exit) must not leave the data service holding its port.
process.once("exit", () => {
  stopping = true;
  for (const child of pythonProcesses.values()) if (child.exitCode === null) child.kill("SIGTERM");
});
