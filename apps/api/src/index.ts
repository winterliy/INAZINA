import { createHash, createHmac, timingSafeEqual, randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { createReadStream, existsSync } from "node:fs";
import { mkdir, unlink, writeFile } from "node:fs/promises";
import { resolve, join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { landingPage, type LandingReason } from "./landing.js";

const projectRoot = findProjectRoot();

try {
  process.loadEnvFile(resolve(projectRoot, ".env"));
} catch (error) {
  if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
}

type Principal = { userId: string; authVersion: number };
type User = { userId: string; nickname: string; role: string; nationality: string; cash: number; creditScore: number; authVersion: number };
type DiscordPurpose = "register" | "find-id" | "reset-password";
type SignedPayload = { kind: "session" | "discord"; userId: string; exp: number; authVersion?: number; purpose?: DiscordPurpose; nickname?: string; nonce?: string };

const host = "127.0.0.1";
const port = Number(process.env.PORT ?? 4312);
const pythonPort = Number(process.env.INAZINA_PYTHON_PORT ?? 4313);
const internalToken = randomBytes(32).toString("hex");
const authSecret = process.env.INAZINA_AUTH_SECRET || randomBytes(32).toString("hex");
const sessionCookie = "inazina_session";
const discordCookie = "inazina_discord_verification";
const oauthStateCookie = "inazina_oauth_state";
const discordClientId = process.env.DISCORD_CLIENT_ID ?? "";
const discordClientSecret = process.env.DISCORD_CLIENT_SECRET ?? "";
const discordRedirectUri = process.env.DISCORD_REDIRECT_URI ?? "http://127.0.0.1:4312/api/auth/discord/callback";
const oauthStates = new Map<string, { purpose: DiscordPurpose; returnTo: string; expiresAt: number }>();
const consumedVerificationNonces = new Set<string>();
const failedLogins = new Map<string, { count: number; resetAt: number }>();
const python = existsSync(join(projectRoot, ".venv", "bin", "python"))
  ? join(projectRoot, ".venv", "bin", "python")
  : process.env.PYTHON ?? "python3";
const pythonProcess = spawn(python, [join(projectRoot, "web_api", "server.py")], {
  cwd: projectRoot,
  env: { ...process.env, INAZINA_INTERNAL_TOKEN: internalToken, INAZINA_PYTHON_PORT: String(pythonPort) },
  stdio: ["ignore", "inherit", "inherit"],
});

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

function cookieHeader(name: string, value: string, maxAge: number): string {
  const secure = process.env.INAZINA_COOKIE_SECURE === "true" ? "; Secure" : "";
  return `${name}=${encodeURIComponent(value)}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${maxAge}${secure}`;
}

function sessionHeader(userId: string, authVersion: number): string {
  return cookieHeader(sessionCookie, signed({ kind: "session", userId, authVersion, exp: Math.floor(Date.now() / 1000) + 43_200 }), 43_200);
}

function principalFor(request: IncomingMessage): Principal | undefined {
  const session = verifiedCookie(cookieValue(request, sessionCookie));
  if (session?.kind === "session" && typeof session.userId === "string" &&
      session.userId.length > 0 && session.userId.length <= 120 && !/[\u0000-\u001f]/.test(session.userId) &&
      typeof session.authVersion === "number" && Number.isInteger(session.authVersion)) {
    return { userId: session.userId, authVersion: session.authVersion };
  }
  return undefined;
}

function verifiedDiscord(request: IncomingMessage, purpose: DiscordPurpose): SignedPayload | undefined {
  const result = verifiedCookie(cookieValue(request, discordCookie));
  if (result?.kind !== "discord" || result.purpose !== purpose || !/^\d{1,25}$/.test(result.userId) || !result.nonce) return undefined;
  return result;
}

function redirectToFrontend(response: ServerResponse, destination: string, params: Record<string, string>, headers: Record<string, string | string[]> = {}): void {
  const url = new URL(destination);
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  response.writeHead(302, { location: url.toString(), "cache-control": "no-store", ...headers });
  response.end();
}

async function beginDiscordOAuth(_request: IncomingMessage, response: ServerResponse, url: URL): Promise<void> {
  if (!discordClientId || !discordClientSecret) {
    send(response, 503, { error: "discord_oauth_not_configured" });
    return;
  }
  const purpose = url.searchParams.get("purpose") as DiscordPurpose | null;
  const returnTo = url.searchParams.get("returnTo");
  const frontends: Record<string, string> = { dashboard: "http://127.0.0.1:4310/", hts: "http://127.0.0.1:4311/" };
  if (!purpose || !["register", "find-id", "reset-password"].includes(purpose) || !returnTo || !frontends[returnTo]) {
    send(response, 400, { error: "invalid_oauth_request" });
    return;
  }
  const state = randomBytes(32).toString("hex");
  oauthStates.set(state, { purpose, returnTo, expiresAt: Date.now() + 5 * 60_000 });
  const authorize = new URL("https://discord.com/oauth2/authorize");
  authorize.searchParams.set("response_type", "code");
  authorize.searchParams.set("client_id", discordClientId);
  authorize.searchParams.set("scope", "identify");
  authorize.searchParams.set("redirect_uri", discordRedirectUri);
  authorize.searchParams.set("state", state);
  response.writeHead(302, { location: authorize.toString(), "cache-control": "no-store",
    "set-cookie": cookieHeader(oauthStateCookie, state, 300) });
  response.end();
}

async function finishDiscordOAuth(request: IncomingMessage, response: ServerResponse, url: URL): Promise<void> {
  const state = url.searchParams.get("state") ?? "";
  const pending = oauthStates.get(state);
  oauthStates.delete(state);
  if (!pending || pending.expiresAt < Date.now() || cookieValue(request, oauthStateCookie) !== state) {
    redirectToFrontend(response, "http://127.0.0.1:4310/", { authError: "oauth_state_invalid" },
      { "set-cookie": cookieHeader(oauthStateCookie, "", 0) });
    return;
  }
  const destination = pending.returnTo === "hts" ? "http://127.0.0.1:4311/" : "http://127.0.0.1:4310/";
  const code = url.searchParams.get("code");
  if (!code || url.searchParams.has("error")) {
    redirectToFrontend(response, destination, { authError: "discord_authorization_failed" },
      { "set-cookie": cookieHeader(oauthStateCookie, "", 0) });
    return;
  }
  let failureStage = "token_exchange";
  try {
    const tokenResponse = await fetch("https://discord.com/api/v10/oauth2/token", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: discordClientId, client_secret: discordClientSecret,
        grant_type: "authorization_code", code, redirect_uri: discordRedirectUri }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!tokenResponse.ok) {
      failureStage = `token_exchange_http_${tokenResponse.status}`;
      throw new Error("discord_token_exchange_failed");
    }
    const tokenBody = await tokenResponse.json() as { access_token?: string; token_type?: string };
    if (!tokenBody.access_token) {
      failureStage = "token_missing";
      throw new Error("discord_token_missing");
    }
    failureStage = "discord_identity";
    const userResponse = await fetch("https://discord.com/api/v10/users/@me", {
      headers: { authorization: `Bearer ${tokenBody.access_token}` }, signal: AbortSignal.timeout(10_000),
    });
    if (!userResponse.ok) {
      failureStage = `discord_identity_http_${userResponse.status}`;
      throw new Error("discord_identity_failed");
    }
    const discordUser = await userResponse.json() as { id?: string; global_name?: string | null; username?: string };
    if (!discordUser.id || !/^\d{1,25}$/.test(discordUser.id)) {
      failureStage = "discord_identity_invalid";
      throw new Error("discord_identity_invalid");
    }
    const signedVerification = signed({ kind: "discord", userId: discordUser.id,
      nickname: discordUser.global_name || discordUser.username || discordUser.id,
      purpose: pending.purpose, nonce: randomBytes(16).toString("hex"),
      exp: Math.floor(Date.now() / 1000) + 300 });
    const verificationCookie = cookieHeader(discordCookie, signedVerification, 300);
    response.writeHead(302, { location: `${destination}?discordAuth=verified&purpose=${pending.purpose}`,
      "set-cookie": [verificationCookie, cookieHeader(oauthStateCookie, "", 0)], "cache-control": "no-store" });
    response.end();
  } catch {
    console.warn(`Discord OAuth verification failed at ${failureStage}`);
    redirectToFrontend(response, destination, { authError: "discord_verification_failed" },
      { "set-cookie": cookieHeader(oauthStateCookie, "", 0) });
  }
}

async function internalRequest(
  method: "GET" | "POST",
  path: string,
  userId?: string,
  body?: unknown,
  timeoutMs = 10_000,
): Promise<{ status: number; body: any }> {
  try {
    const result = await fetch(`http://127.0.0.1:${pythonPort}${path}`, {
      method,
      headers: {
        "x-inazina-internal-token": internalToken,
        ...(userId ? { "x-inazina-user-id": encodeURIComponent(userId) } : {}),
        ...(body ? { "content-type": "application/json" } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const responseText = await result.text();
    try {
      return { status: result.status, body: JSON.parse(responseText) };
    } catch {
      console.error(`Python service returned non-JSON for ${method} ${path} (${result.status}): ${responseText.slice(0, 500)}`);
      return { status: 503, body: { error: "python_service_unavailable" } };
    }
  } catch (error) {
    console.error(`Python service request failed for ${method} ${path}:`, error);
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

async function sendLanding(response: ServerResponse, reason: LandingReason, path: string, status?: number): Promise<void> {
  const health = await internalGet("/internal/health");
  response.writeHead(status ?? (reason === "not_found" ? 404 : 200), {
    "content-type": "text/html; charset=utf-8", "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
    "x-content-type-options": "nosniff",
  });
  response.end(landingPage(reason, path, health.status === 200));
}

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

  // People who open the API port in a browser get a page instead of raw JSON;
  // the apps' fetch calls (Accept: */*) keep receiving JSON.
  const browser = request.method === "GET" && (request.headers.accept ?? "").includes("text/html");
  if (browser && (url.pathname === "/" || url.pathname === "/api" || !url.pathname.startsWith("/api/"))) {
    await sendLanding(response, url.pathname === "/" || url.pathname === "/api" ? "home" : "not_found", url.pathname);
    return;
  }

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

  if (request.method === "GET" && url.pathname === "/api/auth/discord/start") {
    await beginDiscordOAuth(request, response, url);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/auth/discord/callback") {
    await finishDiscordOAuth(request, response, url);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/auth/discord/verification") {
    const verification = verifiedCookie(cookieValue(request, discordCookie));
    if (verification?.kind !== "discord" || !verification.purpose) {
      send(response, 401, { error: "discord_verification_required" });
    } else {
      send(response, 200, { userId: verification.userId, nickname: verification.nickname, purpose: verification.purpose });
    }
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
  if (request.method === "GET" && url.pathname === "/api/auth/countries") {
    const result = await internalGet("/internal/auth/countries");
    send(response, result.status, result.body);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/auth/config") {
    send(response, 200, { discordOAuthConfigured: Boolean(discordClientId && discordClientSecret) });
    return;
  }
  if (request.method === "POST" && url.pathname === "/api/auth/login") {
    const body = await requestJson(request);
    const loginBody = body as { loginId?: string; userId?: string; password?: string };
    const attemptKey = `${request.socket.remoteAddress ?? "local"}:auto:${loginBody.loginId ?? loginBody.userId ?? ""}`;
    const attempt = failedLogins.get(attemptKey);
    if (attempt && attempt.resetAt > Date.now() && attempt.count >= 8) {
      send(response, 429, { error: "login_rate_limited" });
      return;
    }
    const result = await internalRequest("POST", "/internal/auth/login", undefined, body);
    if (result.status === 200 && result.body.user?.userId) {
      failedLogins.delete(attemptKey);
      send(response, 200, result.body, { "set-cookie": sessionHeader(result.body.user.userId, result.body.user.authVersion) });
    } else {
      if (result.status === 401 || result.status === 404) {
        const current = attempt && attempt.resetAt > Date.now() ? attempt : { count: 0, resetAt: Date.now() + 15 * 60_000 };
        current.count += 1;
        failedLogins.set(attemptKey, current);
      }
      send(response, result.status, result.body);
    }
    return;
  }
  if (request.method === "POST" && (url.pathname === "/api/auth/register" || url.pathname === "/api/auth/password/reset")) {
    const purpose: DiscordPurpose = url.pathname === "/api/auth/register" ? "register" : "reset-password";
    const verification = verifiedDiscord(request, purpose);
    if (!verification || consumedVerificationNonces.has(verification.nonce!)) {
      send(response, 403, { error: "discord_verification_required" });
      return;
    }
    const body = await requestJson(request);
    const internalPath = purpose === "register" ? "/internal/auth/register" : "/internal/auth/password/reset";
    const result = await internalRequest("POST", internalPath, verification.userId, {
      ...(body as Record<string, unknown>), nickname: verification.nickname,
    });
    if (result.status >= 200 && result.status < 300) consumedVerificationNonces.add(verification.nonce!);
    const cookies = result.status >= 200 && result.status < 300 && result.body.user?.userId
      ? [cookieHeader(discordCookie, "", 0), sessionHeader(result.body.user.userId, result.body.user.authVersion)]
      : [];
    send(response, result.status, result.body, cookies.length ? { "set-cookie": cookies } : {});
    return;
  }
  if (request.method === "POST" && url.pathname === "/api/auth/logout") {
    const session = verifiedCookie(cookieValue(request, sessionCookie));
    if (session?.kind === "session") {
      await internalRequest("POST", "/internal/auth/logout", session.userId, {});
    }
    send(response, 200, { success: true }, { "set-cookie": cookieHeader(sessionCookie, "", 0) });
    return;
  }

  const principal = principalFor(request);
  if (!principal) {
    if (browser) await sendLanding(response, "login_required", url.pathname, 401);
    else send(response, 401, { error: "authentication_required" });
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

  if (request.method === "GET" && url.pathname === "/api/admin/overview") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalGet("/internal/admin/overview", principal.userId);
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

  if (request.method === "GET" && url.pathname === "/api/admin/events") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalGet("/internal/admin/events", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/me/portfolio") {
    const result = await internalGet("/internal/market/portfolio", principal.userId);
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

  if (request.method === "GET" && url.pathname === "/api/me/loan-schedules") {
    const result = await internalGet("/internal/me/loan-schedules", principal.userId);
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
  if (url.pathname === "/api/admin/board" || /^\/api\/admin\/board\/((post|comment|report|category|poll)\/(\d+|new)|board\/[a-z0-9_-]{1,32})$/.test(url.pathname)) {
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
  const adminMatch = url.pathname.match(/^\/api\/admin\/((?:products|countries|exchanges|help|bot-jobs|user|companies|warnings|feedback|logs|compliance|bank-journal|dr)(?:\/[^/]+){0,2})$/);
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

const server = createServer((request, response) => {
  void handle(request, response).catch((error: unknown) => {
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
  server.close();
  pythonProcess.kill("SIGTERM");
}

process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);
