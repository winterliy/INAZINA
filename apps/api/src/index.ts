import { createHash, timingSafeEqual, randomBytes } from "node:crypto";
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

type Principal = { userId: string; tokenHash: Buffer };
type User = { userId: string; nickname: string; role: string; nationality: string; cash: number; creditScore: number };

const host = "127.0.0.1";
const port = Number(process.env.PORT ?? 4312);
const pythonPort = Number(process.env.INAZINA_PYTHON_PORT ?? 4313);
const internalToken = randomBytes(32).toString("hex");
const python = existsSync(join(projectRoot, ".venv", "bin", "python"))
  ? join(projectRoot, ".venv", "bin", "python")
  : process.env.PYTHON ?? "python3";
const principals = loadPrincipals();
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

function loadPrincipals(): Principal[] {
  const value = process.env.INAZINA_WEB_TOKENS_JSON;
  if (!value) return [];

  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("INAZINA_WEB_TOKENS_JSON must be an object mapping bearer tokens to Discord user IDs.");
  }

  return Object.entries(parsed).map(([token, userId]) => {
    if (token.length < 32 || typeof userId !== "string" || !/^\d{1,25}$/.test(userId)) {
      throw new Error("Web tokens must be at least 32 characters and map to a numeric Discord user ID.");
    }
    return { userId, tokenHash: createHash("sha256").update(token).digest() };
  });
}

function send(response: ServerResponse, status: number, body: unknown): void {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  response.end(JSON.stringify(body));
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

function principalFor(request: IncomingMessage): Principal | undefined {
  const authorization = request.headers.authorization ?? "";
  if (!authorization.startsWith("Bearer ")) return undefined;
  const tokenHash = createHash("sha256").update(authorization.slice(7)).digest();
  return principals.find((principal) => timingSafeEqual(principal.tokenHash, tokenHash));
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

  const principal = principalFor(request);
  if (!principal) {
    send(response, principals.length ? 401 : 503, {
      error: principals.length ? "authentication_required" : "web_auth_not_configured",
    });
    return;
  }

  const identity = await internalGet("/internal/me", principal.userId);
  if (identity.status !== 200) {
    send(response, identity.status, identity.body);
    return;
  }
  const user = identity.body.user as User;

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
  if (!principals.length) {
    console.warn("Web authentication is disabled until INAZINA_WEB_TOKENS_JSON is configured.");
  }
});

function shutdown(): void {
  server.close();
  pythonProcess.kill("SIGTERM");
}

process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);
