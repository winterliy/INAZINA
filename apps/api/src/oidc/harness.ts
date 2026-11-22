// Helpers for tests that drive the identity provider the way a browser does: a cookie jar, manual redirects, PKCE values.
import { createHash, randomBytes } from "node:crypto";

// A browser's cookie jar, including the Path rule: a cookie is only sent to addresses under its path (this is what the
// interaction cookies depend on, so tests must not ignore it).
export class Jar {
  cookies = new Map<string, { value: string; path: string }>();
  absorb(response: Response): void {
    for (const line of response.headers.getSetCookie()) {
      const [pair, ...attributes] = line.split(";");
      const index = pair.indexOf("=");
      const name = pair.slice(0, index);
      const value = pair.slice(index + 1);
      const path = attributes.map((part) => part.trim()).find((part) => /^path=/i.test(part))?.slice(5) ?? "/";
      if (value === "" || /;\s*max-age=0/i.test(line)) this.cookies.delete(name); else this.cookies.set(name, { value, path });
    }
  }
  // The Cookie header for a request to `path` (every cookie when no path is given).
  header(path?: string): string {
    return [...this.cookies].filter(([, cookie]) => path === undefined || path === cookie.path || path.startsWith(cookie.path.endsWith("/") ? cookie.path : `${cookie.path}/`))
      .map(([name, cookie]) => `${name}=${cookie.value}`).join("; ");
  }
}

export async function send(url: string, jar: Jar, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(url, { redirect: "manual", ...init, headers: { cookie: jar.header(new URL(url).pathname), ...(init.headers as Record<string, string> | undefined) } });
  jar.absorb(response);
  return response;
}

export const newVerifier = (): string => randomBytes(32).toString("base64url");
export const challengeOf = (verifier: string): string => createHash("sha256").update(verifier).digest("base64url");

// Follows redirects (resolving relative ones against `base`) until one leaves for `stopPrefix`; returns that URL.
export async function followUntil(base: string, jar: Jar, first: Response, stopPrefix: string): Promise<URL> {
  let response = first;
  for (let hop = 0; hop < 8; hop += 1) {
    const location = new URL(response.headers.get("location") ?? "", base);
    if (location.href.startsWith(stopPrefix)) return location;
    response = await send(location.href, jar);
  }
  throw new Error("redirect chain did not reach " + stopPrefix);
}

// Signs in at the identity provider as a browser would: starts at `authorizeUrl`, posts the JSON login, follows to `stopPrefix`.
export async function idpSignIn(idpBase: string, jar: Jar, authorizeUrl: string, stopPrefix: string, loginId = "alice", password = "pw"): Promise<URL> {
  const first = await send(authorizeUrl, jar);
  const uid = /\/oidc\/interaction\/([\w-]+)$/.exec(first.headers.get("location") ?? "")?.[1];
  if (!uid) throw new Error("no interaction: " + first.status + " " + first.headers.get("location"));
  const posted = await send(`${idpBase}/oidc/interaction/${uid}/login`, jar, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ loginId, password }) });
  const { redirectTo } = await posted.json() as { redirectTo: string };
  const resumed = await send(redirectTo, jar);
  return followUntil(idpBase, jar, resumed, stopPrefix);
}

// ---- A complete identity provider on a loopback port, with a fake data service and a fake Discord. ----
import { createServer } from "node:http";
import { exportJWK, generateKeyPair } from "jose";
import type { DiscordClient } from "./discord.js";
import { createIdp, type InternalRequest } from "./idp.js";
import { memoryStore } from "./memoryStore.js";
import { createProvider, type Account, type Jwk } from "./provider.js";

export const callbackUri = "https://naru.inazina.localhost/api/auth/oidc/callback";
export const alice: Account = { userId: "alice", nickname: "앨리스", role: "admin", authVersion: 3 };
export const bob: Account = { userId: "123456789012345678", nickname: "밥", role: "user", authVersion: 0 };
export const home = "https://naru.inazina.localhost/";

export const fakeDiscord = (overrides: Partial<DiscordClient> = {}): DiscordClient => ({
  configured: true,
  authorizeUrl: (state) => `https://discord.test/authorize?state=${state}`,
  identify: async (code) => { if (code !== "good") throw new Error("discord_identity_failed"); return { id: bob.userId, nickname: bob.nickname }; },
  ...overrides,
});

export async function startIdp(options: { uiDir?: string; discord?: DiscordClient } = {}) {
  const calls: Array<{ method: string; path: string; userId?: string; body: any }> = [];
  const server = createServer();
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const port = (server.address() as { port: number }).port;
  const base = `http://127.0.0.1:${port}`;
  const { privateKey } = await generateKeyPair("RS256", { extractable: true });
  const jwk = { ...(await exportJWK(privateKey)), kid: "k1", alg: "RS256", use: "sig" } as Jwk;
  const users: Record<string, Account> = { [alice.userId]: alice, [bob.userId]: bob };
  const provider = createProvider({
    issuer: `${base}/oidc`, cookieKeys: ["idp-secret"], jwks: { keys: [jwk] }, call: memoryStore(), findAccount: async (id) => users[id],
    clients: [{ clientId: "naru", redirectUris: [callbackUri], postLogoutRedirectUris: [home] }],
  });
  const internalRequest: InternalRequest = async (method, path, userId, body) => {
    calls.push({ method, path, userId, body });
    const input = (body ?? {}) as { loginId?: string; password?: string; countryId?: string };
    if (path === "/internal/auth/login") {
      if (input.loginId !== "alice") return { status: 404, body: { error: "account_not_found" } };
      return input.password === "pw" ? { status: 200, body: { user: alice } } : { status: 401, body: { error: "invalid_credentials" } };
    }
    if (path === "/internal/auth/countries") return { status: 200, body: { countries: [{ countryId: "KR", currencyName: "원", currencyCode: "KRW" }] } };
    if (path === "/internal/auth/register") return input.password === "dup" ? { status: 409, body: { error: "account_exists" } } : { status: 201, body: { user: bob } };
    if (path === "/internal/auth/password/reset") return { status: 200, body: { user: alice } };
    return { status: 404, body: {} };
  };
  const idp = createIdp({ provider, issuerHost: `127.0.0.1:${port}`, internalRequest, clientIp: () => "203.0.113.9", appUrls: { home },
    discord: options.discord ?? fakeDiscord(), ...(options.uiDir ? { uiDir: options.uiDir } : {}), secret: "test-secret" });
  server.on("request", async (request, response) => { if (!(await idp(request, response))) { response.writeHead(418); response.end("api"); } });
  return { base, port, calls, close: () => new Promise<void>((done) => server.close(() => done())) };
}

export const authorizeUrl = (base: string, codeVerifier: string, extra: Record<string, string> = {}) => `${base}/oidc/auth?` + new URLSearchParams({
  client_id: "naru", response_type: "code", scope: "openid profile", redirect_uri: callbackUri, state: "s1", nonce: "n1",
  code_challenge: challengeOf(codeVerifier), code_challenge_method: "S256", ...extra });

// Starts an authorization request and returns the interaction's uid (the interaction cookies land in `jar`).
export async function beginInteraction(base: string, jar: Jar, codeVerifier = newVerifier(), extra: Record<string, string> = {}): Promise<string> {
  const first = await send(authorizeUrl(base, codeVerifier, extra), jar);
  const uid = /\/oidc\/interaction\/([\w-]+)$/.exec(first.headers.get("location") ?? "")?.[1];
  if (!uid) throw new Error(`no interaction: ${first.status} ${first.headers.get("location")}`);
  return uid;
}

export const postJson = (url: string, jar: Jar, body: unknown) =>
  send(url, jar, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

export const exchangeCode = (base: string, code: string, codeVerifier: string) => fetch(`${base}/oidc/token`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: callbackUri, client_id: "naru", code_verifier: codeVerifier }) });
