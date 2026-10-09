import { createHmac, timingSafeEqual, randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { existsSync } from "node:fs";
import { resolve, join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

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
  if (session?.kind === "session" && /^\d{1,25}$/.test(session.userId) &&
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
  try {
    const tokenResponse = await fetch("https://discord.com/api/v10/oauth2/token", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: discordClientId, client_secret: discordClientSecret,
        grant_type: "authorization_code", code, redirect_uri: discordRedirectUri }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!tokenResponse.ok) throw new Error("discord_token_exchange_failed");
    const tokenBody = await tokenResponse.json() as { access_token?: string; token_type?: string };
    if (!tokenBody.access_token) throw new Error("discord_token_missing");
    const userResponse = await fetch("https://discord.com/api/v10/users/@me", {
      headers: { authorization: `Bearer ${tokenBody.access_token}` }, signal: AbortSignal.timeout(10_000),
    });
    if (!userResponse.ok) throw new Error("discord_identity_failed");
    const discordUser = await userResponse.json() as { id?: string; global_name?: string | null; username?: string };
    if (!discordUser.id || !/^\d{1,25}$/.test(discordUser.id)) throw new Error("discord_identity_invalid");
    const signedVerification = signed({ kind: "discord", userId: discordUser.id,
      nickname: discordUser.global_name || discordUser.username || discordUser.id,
      purpose: pending.purpose, nonce: randomBytes(16).toString("hex"),
      exp: Math.floor(Date.now() / 1000) + 300 });
    const verificationCookie = cookieHeader(discordCookie, signedVerification, 300);
    response.writeHead(302, { location: `${destination}?discordAuth=verified&purpose=${pending.purpose}`,
      "set-cookie": [verificationCookie, cookieHeader(oauthStateCookie, "", 0)], "cache-control": "no-store" });
    response.end();
  } catch {
    redirectToFrontend(response, destination, { authError: "discord_verification_failed" },
      { "set-cookie": cookieHeader(oauthStateCookie, "", 0) });
  }
}

async function internalRequest(
  method: "GET" | "POST",
  path: string,
  userId?: string,
  body?: unknown,
): Promise<{ status: number; body: any }> {
  try {
    const result = await fetch(`http://127.0.0.1:${pythonPort}${path}`, {
      method,
      headers: {
        "x-inazina-internal-token": internalToken,
        ...(userId ? { "x-inazina-user-id": userId } : {}),
        ...(body ? { "content-type": "application/json" } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(10_000),
    });
    return { status: result.status, body: await result.json() };
  } catch {
    return { status: 503, body: { error: "python_service_unavailable" } };
  }
}

const internalGet = (path: string, userId?: string) => internalRequest("GET", path, userId);

async function requestJson(request: IncomingMessage): Promise<unknown> {
  if (!request.headers["content-type"]?.includes("application/json")) {
    throw new Error("application_json_required");
  }
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 32_768) throw new Error("request_too_large");
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
    const loginBody = body as { userId?: string; password?: string };
    const attemptKey = `${request.socket.remoteAddress ?? "local"}:${loginBody.userId ?? ""}`;
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

  if (request.method === "GET" && url.pathname === "/api/admin/users") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const search = encodeURIComponent(url.searchParams.get("search") ?? "");
    const result = await internalGet(`/internal/admin/users?search=${search}`, principal.userId);
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
    const range = url.searchParams.get("range") ?? "1d";
    const result = await internalGet(`/internal/market/chart/${encodeURIComponent(decodeURIComponent(chartMatch[1]))}?range=${encodeURIComponent(range)}`, principal.userId);
    send(response, result.status, result.body);
    return;
  }

  const drawingsMatch = url.pathname.match(/^\/api\/market\/drawings\/([^/]+)$/);
  if (drawingsMatch) {
    const code = encodeURIComponent(decodeURIComponent(drawingsMatch[1]));
    if (request.method === "GET") {
      const result = await internalGet(`/internal/market/drawings/${code}`, principal.userId);
      send(response, result.status, result.body);
      return;
    }
    if (request.method === "POST") {
      const body = await requestJson(request);
      const result = await internalRequest("POST", `/internal/market/drawings/${code}`, principal.userId, body);
      send(response, result.status, result.body);
      return;
    }
  }

  if (request.method === "POST" && url.pathname === "/api/market/orders") {
    const body = await requestJson(request);
    const result = await internalRequest("POST", "/internal/market/orders", principal.userId, body);
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

  const userMatch = url.pathname.match(/^\/api\/admin\/users\/(\d+)$/);
  if (request.method === "POST" && userMatch) {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const body = await requestJson(request);
    const result = await internalRequest("POST", `/internal/admin/users/${userMatch[1]}`, principal.userId, body);
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
