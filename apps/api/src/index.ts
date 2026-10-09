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

async function internalGet(path: string, userId?: string): Promise<{ status: number; body: any }> {
  try {
    const result = await fetch(`http://127.0.0.1:${pythonPort}${path}`, {
      headers: {
        "x-inazina-internal-token": internalToken,
        ...(userId ? { "x-inazina-user-id": userId } : {}),
      },
      signal: AbortSignal.timeout(2500),
    });
    return { status: result.status, body: await result.json() };
  } catch {
    return { status: 503, body: { error: "python_service_unavailable" } };
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

  if (request.method !== "GET") {
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

  if (url.pathname === "/api/auth/session" || url.pathname === "/api/me") {
    send(response, 200, { user });
    return;
  }

  if (url.pathname === "/api/market/instruments") {
    const result = await internalGet("/internal/market/instruments", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  if (url.pathname === "/api/admin/overview") {
    if (user.role !== "admin") {
      send(response, 403, { error: "admin_required" });
      return;
    }
    const result = await internalGet("/internal/admin/overview", principal.userId);
    send(response, result.status, result.body);
    return;
  }

  send(response, 404, { error: "not_found" });
}

const server = createServer((request, response) => {
  void handle(request, response).catch((error: unknown) => {
    console.error("API request failed:", error);
    if (!response.headersSent) send(response, 500, { error: "internal_error" });
    else response.destroy();
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
