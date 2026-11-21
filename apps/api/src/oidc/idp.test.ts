import test from "node:test";
import assert from "node:assert/strict";
import { createServer, request as httpRequest } from "node:http";
import { createLocalJWKSet, exportJWK, generateKeyPair, jwtVerify } from "jose";
import { createProvider, type Account, type Jwk } from "./provider.js";
import { createIdp, type InternalRequest } from "./idp.js";
import { memoryStore } from "./memoryStore.js";
import { Jar, send, newVerifier, challengeOf, followUntil } from "./harness.js";
import type { OidcClient } from "./clients.js";

const callback = "https://naru.inazina.localhost/api/auth/oidc/callback";
const clients: OidcClient[] = [{ clientId: "naru", redirectUris: [callback], postLogoutRedirectUris: ["https://naru.inazina.localhost/"] },
  { clientId: "console", redirectUris: ["https://console.inazina.localhost/api/auth/oidc/callback"], postLogoutRedirectUris: [], maxAgeSeconds: 900 }];
const alice: Account = { userId: "alice", nickname: "앨리스", role: "admin", authVersion: 3 };
const appUrls = { home: "https://naru.inazina.localhost/", register: "https://naru.inazina.localhost/?register=1", reset: "https://naru.inazina.localhost/?reset=1" };

async function start() {
  const logins: Array<{ path: string; body: any }> = [];
  const server = createServer();
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const port = (server.address() as { port: number }).port;
  const base = `http://127.0.0.1:${port}`;
  const { privateKey } = await generateKeyPair("RS256", { extractable: true });
  const jwk = { ...(await exportJWK(privateKey)), kid: "test", alg: "RS256", use: "sig" } as Jwk;
  const provider = createProvider({ issuer: `${base}/oidc`, clients, call: memoryStore(), cookieKeys: ["test-secret"], jwks: { keys: [jwk] },
    findAccount: async (id) => (id === "alice" ? alice : undefined) });
  const internalRequest: InternalRequest = async (_method, path, _user, body) => {
    logins.push({ path, body });
    const credentials = body as { loginId: string; password: string };
    if (credentials.loginId !== "alice") return { status: 404, body: { error: "account_not_found" } };
    if (credentials.password !== "pw") return { status: 401, body: { error: "invalid_credentials" } };
    return { status: 200, body: { user: alice } };
  };
  const idp = createIdp({ provider, issuerHost: `127.0.0.1:${port}`, internalRequest, clientIp: () => "203.0.113.9", appUrls });
  server.on("request", async (req, res) => { if (!(await idp(req, res))) { res.writeHead(418); res.end("api"); } });
  const { d: _d, p: _p, q: _q, dp: _dp, dq: _dq, qi: _qi, ...publicJwk } = jwk;
  return { base, port, logins, issuer: `${base}/oidc`, keys: createLocalJWKSet({ keys: [publicJwk as never] }), close: () => new Promise<void>((done) => server.close(() => done())) };
}

const authUrl = (base: string, codeVerifier: string, extra: Record<string, string> = {}) => `${base}/oidc/auth?` + new URLSearchParams({
  client_id: "naru", response_type: "code", scope: "openid profile", redirect_uri: callback, state: "s1", nonce: "n1",
  code_challenge: challengeOf(codeVerifier), code_challenge_method: "S256", ...extra });

// Starts the flow and returns the interaction uid plus the cookie jar holding the interaction cookies.
async function begin(base: string, jar: Jar, codeVerifier: string) {
  const first = await send(authUrl(base, codeVerifier), jar);
  assert.equal(first.status, 303);
  const uid = /\/oidc\/interaction\/([\w-]+)$/.exec(first.headers.get("location") ?? "")![1];
  return uid;
}
const postLogin = (base: string, jar: Jar, uid: string, loginId: string, password: string) =>
  send(`${base}/oidc/interaction/${uid}/login`, jar, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ loginId, password }) });

const followToCallback = (base: string, jar: Jar, first: Response): Promise<URL> => followUntil(base, jar, first, callback);
const exchange = (base: string, code: string, codeVerifier: string) => fetch(`${base}/oidc/token`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: callback, client_id: "naru", code_verifier: codeVerifier }) });

async function signIn(base: string, jar: Jar, codeVerifier: string) {
  const uid = await begin(base, jar, codeVerifier);
  return followToCallback(base, jar, await postLogin(base, jar, uid, "alice", "pw"));
}

test("the interaction page is the shared login layout with a form posting to the interaction", async () => {
  const { base, close } = await start();
  try {
    const jar = new Jar();
    const uid = await begin(base, jar, newVerifier());
    const page = await send(`${base}/oidc/interaction/${uid}`, jar);
    assert.equal(page.status, 200);
    assert.match(page.headers.get("content-type") ?? "", /text\/html/);
    assert.equal(page.headers.get("cache-control"), "no-store");
    const html = await page.text();
    assert.ok(html.includes(`action="/oidc/interaction/${uid}/login"`) && html.includes('name="loginId"') && html.includes('name="password"'));
    assert.ok(html.includes(appUrls.register) && html.includes(appUrls.reset) && !html.includes("{{"));
  } finally { await close(); }
});

test("a correct login ends at the registered redirect URI with a code and the original state", async () => {
  const { base, close } = await start();
  try {
    const target = await signIn(base, new Jar(), newVerifier());
    assert.equal(target.origin + target.pathname, callback);
    assert.equal(target.searchParams.get("state"), "s1");
    assert.ok(target.searchParams.get("code"));
  } finally { await close(); }
});

test("the token response carries a signed ID token with sub, name, role, auth_version and the nonce", async () => {
  const { base, issuer, keys, close } = await start();
  try {
    const codeVerifier = newVerifier();
    const target = await signIn(base, new Jar(), codeVerifier);
    const response = await exchange(base, target.searchParams.get("code")!, codeVerifier);
    assert.equal(response.status, 200);
    const { id_token } = await response.json() as { id_token: string };
    const { payload } = await jwtVerify(id_token, keys, { issuer, audience: "naru" });
    assert.deepEqual({ sub: payload.sub, name: payload.name, role: payload.role, auth_version: payload.auth_version, nonce: payload.nonce },
      { sub: "alice", name: "앨리스", role: "admin", auth_version: 3, nonce: "n1" });
  } finally { await close(); }
});

test("a code cannot be used twice, nor with another verifier", async () => {
  const { base, close } = await start();
  try {
    const codeVerifier = newVerifier();
    const code = (await signIn(base, new Jar(), codeVerifier)).searchParams.get("code")!;
    assert.equal((await exchange(base, code, codeVerifier)).status, 200);
    assert.equal((await exchange(base, code, codeVerifier)).status, 400);
    const second = (await signIn(base, new Jar(), codeVerifier)).searchParams.get("code")!;
    const wrong = await exchange(base, second, newVerifier());
    assert.equal(wrong.status, 400);
    assert.equal(((await wrong.json()) as { error: string }).error, "invalid_grant");
  } finally { await close(); }
});

test("a wrong password re-renders the page with 401 and a message; an unknown account gives 404", async () => {
  const { base, close } = await start();
  try {
    const jar = new Jar();
    const uid = await begin(base, jar, newVerifier());
    const wrong = await postLogin(base, jar, uid, "alice", "nope");
    assert.equal(wrong.status, 401);
    const html = await wrong.text();
    assert.match(html, /아이디 또는 비밀번호가 올바르지 않습니다/);
    assert.ok(html.includes('value="alice"') && !html.includes("nope"));
    assert.equal((await postLogin(base, jar, uid, "ghost", "pw")).status, 404);
  } finally { await close(); }
});

test("the ninth failed attempt in 15 minutes from one address and id is refused with 429", async () => {
  const { base, close } = await start();
  try {
    const jar = new Jar();
    const uid = await begin(base, jar, newVerifier());
    for (let attempt = 0; attempt < 8; attempt += 1) assert.equal((await postLogin(base, jar, uid, "alice", "nope")).status, 401);
    assert.equal((await postLogin(base, jar, uid, "alice", "pw")).status, 429);
  } finally { await close(); }
});

test("the login call carries the client address, user agent and the app label used by the login history", async () => {
  const { base, logins, close } = await start();
  try {
    await signIn(base, new Jar(), newVerifier());
    assert.equal(logins[0].path, "/internal/auth/login");
    assert.equal(logins[0].body.loginId, "alice");
    assert.deepEqual({ clientIp: logins[0].body.meta.clientIp, app: logins[0].body.meta.app }, { clientIp: "203.0.113.9", app: "hts" });
  } finally { await close(); }
});

test("an interaction used without its cookie cannot log in", async () => {
  const { base, close } = await start();
  try {
    const uid = await begin(base, new Jar(), newVerifier());
    const response = await postLogin(base, new Jar(), uid, "alice", "pw");
    assert.ok(response.status >= 400 && response.status < 500);
  } finally { await close(); }
});

test("signing out ends the single sign-on session: the next authorization asks for a login again", async () => {
  const { base, close } = await start();
  try {
    const jar = new Jar();
    await signIn(base, jar, newVerifier());
    const silent = await send(authUrl(base, newVerifier()), jar);
    assert.ok((silent.headers.get("location") ?? "").startsWith(callback), "a live session returns a code without a prompt");
    const page = await send(`${base}/oidc/session/end`, jar);
    assert.equal(page.status, 200);
    const html = await page.text();
    assert.match(html, /<script>/);
    const xsrf = /name="xsrf" value="([^"]+)"/.exec(html)![1];
    const confirmed = await send(`${base}/oidc/session/end/confirm`, jar, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ xsrf, logout: "yes" }) });
    assert.equal(confirmed.status, 303);
    const again = await send(authUrl(base, newVerifier()), jar);
    assert.match(again.headers.get("location") ?? "", /\/oidc\/interaction\//);
  } finally { await close(); }
});

test("an unregistered post_logout_redirect_uri is never followed after a sign-out", async () => {
  const { base, close } = await start();
  try {
    const jar = new Jar();
    await signIn(base, jar, newVerifier());
    const page = await send(`${base}/oidc/session/end?` + new URLSearchParams({ client_id: "naru", post_logout_redirect_uri: "https://evil.example/landing" }), jar);
    assert.equal(page.status, 400, "the request itself is refused");
    assert.ok(!(page.headers.get("location") ?? "").includes("evil.example"));
    assert.ok(!(await page.text()).includes("evil.example/landing\""), "the address is not offered as a link either");
  } finally { await close(); }
});

const raw = (port: number, path: string, host: string) => new Promise<{ status: number; location?: string }>((done, fail) => {
  httpRequest({ host: "127.0.0.1", port, path, headers: { host } }, (response) => { response.resume(); done({ status: response.statusCode!, location: response.headers.location }); }).on("error", fail).end();
});

test("the identity provider host serves no API route; other hosts fall through to the API", async () => {
  const { port, close } = await start();
  try {
    assert.equal((await raw(port, "/api/health", `127.0.0.1:${port}`)).status, 404);
    assert.equal((await raw(port, "/", `127.0.0.1:${port}`)).status, 404);
    assert.equal((await raw(port, "/api/health", "127.0.0.1:4312")).status, 418);
    assert.equal((await raw(port, "/oidc/.well-known/openid-configuration", "naru.inazina.localhost")).status, 418);
  } finally { await close(); }
});

test("/register, /reset and /login on the identity provider host send people to the app pages", async () => {
  const { port, close } = await start();
  try {
    const host = `127.0.0.1:${port}`;
    assert.deepEqual(await raw(port, "/register", host), { status: 302, location: appUrls.register });
    assert.deepEqual(await raw(port, "/reset", host), { status: 302, location: appUrls.reset });
    assert.deepEqual(await raw(port, "/login", host), { status: 302, location: appUrls.home });
  } finally { await close(); }
});
