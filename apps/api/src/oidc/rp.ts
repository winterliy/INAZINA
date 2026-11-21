// The relying party: what each INAZINA app host (naru, console) does to sign people in through the identity provider.
// /api/auth/oidc/login starts the authorization code flow with PKCE; /api/auth/oidc/callback checks everything the
// provider returns and only then issues the app's own session cookie.
import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import { createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey } from "jose";
import { loadClients, type OidcClient } from "./clients.js";

export type RpDeps = {
  env: NodeJS.ProcessEnv; // INAZINA_OIDC_ISSUER and the client registry variables
  authSecret: string;
  issueSession: (userId: string, authVersion: number) => string; // the Set-Cookie value of the app's session
};
export type VerifiedIdToken = { sub: string; authVersion: number; name?: string; role?: string };

const accountModes = new Set(["register", "reset-password", "find-id"]); // which account screen the login server opens first
const txCookie = "inazina_oidc_tx";
const txSeconds = 600;
export const rpPaths = { login: "/api/auth/oidc/login", callback: "/api/auth/oidc/callback" };

// Only a path on this site: anything that could leave it (another host, a scheme, a backslash) becomes "/".
export function safeReturnTo(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u001f]/.test(value) || value.startsWith("/api/auth/oidc")) return "/";
  return value.slice(0, 500);
}

export async function verifyIdToken(idToken: string, options: { issuer: string; audience: string; nonce: string; keys: JWTVerifyGetKey }): Promise<VerifiedIdToken> {
  try {
    const { payload } = await jwtVerify(idToken, options.keys, { issuer: options.issuer, audience: options.audience, algorithms: ["RS256"] });
    if (payload.nonce !== options.nonce) throw new Error("nonce");
    if (typeof payload.sub !== "string" || !payload.sub || payload.sub.length > 120 || /[\u0000-\u001f]/.test(payload.sub)) throw new Error("sub");
    if (typeof payload.auth_version !== "number" || !Number.isInteger(payload.auth_version)) throw new Error("auth_version");
    return { sub: payload.sub, authVersion: payload.auth_version, ...(typeof payload.name === "string" ? { name: payload.name } : {}), ...(typeof payload.role === "string" ? { role: payload.role } : {}) };
  } catch { throw new Error("invalid_token"); }
}

type Endpoints = { authorization_endpoint: string; token_endpoint: string; jwks_uri: string; end_session_endpoint?: string };

// One API process serves every app host, so the client (naru, console) is picked by the host the request came to.
export function createRp(deps: RpDeps): { handle: (request: IncomingMessage, response: ServerResponse, url: URL) => Promise<boolean>; logoutUrl: (host: string) => Promise<string | undefined>; servesHost: (host: string | undefined) => boolean } {
  const { env } = deps;
  const issuer = (env.INAZINA_OIDC_ISSUER ?? "").replace(/\/$/, "");
  if (!issuer) throw new Error("INAZINA_OIDC_ISSUER is required");
  const byHost = new Map<string, OidcClient>(loadClients(env).map((entry) => [new URL(entry.redirectUris[0]).host.toLowerCase(), entry]));
  const clientFor = (host: string | undefined) => byHost.get((host ?? "").toLowerCase());
  const secure = env.INAZINA_COOKIE_SECURE === "true" ? "; Secure" : "";

  let discovered: { endpoints: Endpoints; keys: JWTVerifyGetKey; at: number } | undefined;
  async function discover() {
    if (discovered && Date.now() - discovered.at < 300_000) return discovered;
    const response = await fetch(`${issuer}/.well-known/openid-configuration`, { signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error("oidc_unavailable");
    const endpoints = await response.json() as Endpoints;
    discovered = { endpoints, keys: createRemoteJWKSet(new URL(endpoints.jwks_uri), { timeoutDuration: 5000 }), at: Date.now() };
    return discovered;
  }

  const mac = (body: string) => createHmac("sha256", deps.authSecret).update(body).digest();
  const sign = (value: object): string => { const body = Buffer.from(JSON.stringify(value)).toString("base64url"); return `${body}.${mac(body).toString("base64url")}`; };
  const unsign = (raw: string | undefined): Record<string, any> | undefined => {
    const [body, signature] = (raw ?? "").split(".");
    if (!body || !signature) return undefined;
    const expected = mac(body);
    const actual = Buffer.from(signature, "base64url");
    if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) return undefined;
    try {
      const value = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
      return value.exp > Math.floor(Date.now() / 1000) ? value : undefined;
    } catch { return undefined; }
  };
  const cookieOf = (request: IncomingMessage, name: string): string | undefined => {
    const entry = (request.headers.cookie ?? "").split(";").map((part) => part.trim()).find((part) => part.startsWith(`${name}=`));
    return entry ? decodeURIComponent(entry.slice(name.length + 1)) : undefined;
  };
  const txHeader = (value: string, maxAge: number) => `${txCookie}=${encodeURIComponent(value)}; Path=/api/auth/oidc; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure}`;
  const same = (a: string, b: string) => a.length === b.length && timingSafeEqual(Buffer.from(a), Buffer.from(b));
  const redirect = (response: ServerResponse, location: string, cookies: string[]) => {
    response.writeHead(302, { location, "cache-control": "no-store", "content-length": "0", "set-cookie": cookies });
    response.end();
  };
  const fail = (response: ServerResponse, code: string) => redirect(response, `/?login_error=${code}`, [txHeader("", 0)]);

  async function login(client: OidcClient, response: ServerResponse, url: URL): Promise<void> {
    let endpoints: Endpoints;
    try { endpoints = (await discover()).endpoints; } catch { return fail(response, "oidc_unavailable"); }
    const verifier = randomBytes(32).toString("base64url");
    const state = randomBytes(16).toString("base64url");
    const nonce = randomBytes(16).toString("base64url");
    const target = new URL(endpoints.authorization_endpoint);
    target.search = new URLSearchParams({
      client_id: client.clientId, response_type: "code", scope: "openid profile", redirect_uri: client.redirectUris[0], state, nonce,
      code_challenge: createHash("sha256").update(verifier).digest("base64url"), code_challenge_method: "S256",
      ...(client.maxAgeSeconds ? { max_age: String(client.maxAgeSeconds) } : {}),
      ...(accountModes.has(url.searchParams.get("mode") ?? "") ? { ui_mode: url.searchParams.get("mode")! } : {}),
    }).toString();
    const transaction = sign({ client: client.clientId, state, nonce, verifier, returnTo: safeReturnTo(url.searchParams.get("returnTo")), exp: Math.floor(Date.now() / 1000) + txSeconds });
    redirect(response, target.href, [txHeader(transaction, txSeconds)]);
  }

  async function callback(client: OidcClient, request: IncomingMessage, response: ServerResponse, url: URL): Promise<void> {
    const transaction = unsign(cookieOf(request, txCookie));
    if (!transaction || transaction.client !== client.clientId) return fail(response, "login_expired");
    if (!same(url.searchParams.get("state") ?? "", String(transaction.state))) return fail(response, "state_mismatch");
    if (url.searchParams.get("error")) return fail(response, "login_denied");
    const code = url.searchParams.get("code");
    if (!code) return fail(response, "login_failed");
    let found: Awaited<ReturnType<typeof discover>>;
    let idToken: string;
    try {
      found = await discover();
      const exchange = await fetch(found.endpoints.token_endpoint, {
        method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, signal: AbortSignal.timeout(10_000),
        body: new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: client.redirectUris[0], client_id: client.clientId, code_verifier: String(transaction.verifier) }),
      });
      if (!exchange.ok) return fail(response, "login_failed");
      idToken = String(((await exchange.json()) as { id_token?: string }).id_token ?? "");
    } catch { return fail(response, "oidc_unavailable"); }
    let verified: VerifiedIdToken;
    try { verified = await verifyIdToken(idToken, { issuer, audience: client.clientId, nonce: String(transaction.nonce), keys: found.keys }); }
    catch { return fail(response, "invalid_token"); }
    redirect(response, safeReturnTo(transaction.returnTo), [deps.issueSession(verified.sub, verified.authVersion), txHeader("", 0)]);
  }

  return {
    servesHost: (host) => clientFor(host) !== undefined,
    async handle(request, response, url) {
      const client = clientFor(request.headers.host);
      if (!client || request.method !== "GET" || (url.pathname !== rpPaths.login && url.pathname !== rpPaths.callback)) return false;
      await (url.pathname === rpPaths.login ? login(client, response, url) : callback(client, request, response, url));
      return true;
    },
    // Where the browser goes to end the single sign-on session too, or nothing if the provider cannot be reached.
    async logoutUrl(host) {
      const client = clientFor(host);
      if (!client) return undefined;
      try {
        const { endpoints } = await discover();
        if (!endpoints.end_session_endpoint) return undefined;
        const target = new URL(endpoints.end_session_endpoint);
        target.search = new URLSearchParams({ client_id: client.clientId, ...(client.postLogoutRedirectUris[0] ? { post_logout_redirect_uri: client.postLogoutRedirectUris[0] } : {}) }).toString();
        return target.href;
      } catch { return undefined; }
    },
  };
}
