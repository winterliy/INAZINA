import test from "node:test";
import assert from "node:assert/strict";
import { createServer, request as httpRequest } from "node:http";
import { createLocalJWKSet, exportJWK, generateKeyPair, SignJWT } from "jose";
import { createProvider, type Account, type Jwk } from "./provider.js";
import { createIdp, type InternalRequest } from "./idp.js";
import { loadClients } from "./clients.js";
import { memoryStore } from "./memoryStore.js";
import { createRp, safeReturnTo, verifyIdToken } from "./rp.js";
import { Jar, idpSignIn, send } from "./harness.js";

const alice: Account = { userId: "alice", nickname: "앨리스", role: "admin", authVersion: 3 };

test("safeReturnTo keeps paths on this site and replaces everything else with /", () => {
  assert.equal(safeReturnTo("/portfolio?tab=1"), "/portfolio?tab=1");
  for (const bad of [null, undefined, "", "portfolio", "//evil.example", "https://evil.example/", "/\\evil.example", "/a\nb", "/api/auth/oidc/login"]) assert.equal(safeReturnTo(bad), "/", String(bad));
  assert.equal(safeReturnTo("/" + "a".repeat(900)).length, 500);
});

async function signing(overrides: { iss?: string; aud?: string; nonce?: string; exp?: string; key?: CryptoKey; auth_version?: unknown; sub?: string } = {}) {
  const pair = await generateKeyPair("RS256", { extractable: true });
  const jwk = { ...(await exportJWK(pair.publicKey)), kid: "k1", alg: "RS256", use: "sig" };
  const claims: Record<string, unknown> = { name: "앨리스", role: "admin", auth_version: "auth_version" in overrides ? overrides.auth_version : 3, nonce: overrides.nonce ?? "n1" };
  const token = await new SignJWT(claims).setProtectedHeader({ alg: "RS256", kid: "k1" }).setSubject(overrides.sub ?? "alice")
    .setIssuer(overrides.iss ?? "https://id.inazina.kr/oidc").setAudience(overrides.aud ?? "naru").setIssuedAt().setExpirationTime(overrides.exp ?? "5m")
    .sign(overrides.key ?? pair.privateKey);
  return { token, keys: createLocalJWKSet({ keys: [jwk] }) };
}
const expected = { issuer: "https://id.inazina.kr/oidc", audience: "naru", nonce: "n1" };

test("verifyIdToken accepts a good token and returns sub, auth_version, name and role", async () => {
  const { token, keys } = await signing();
  assert.deepEqual(await verifyIdToken(token, { ...expected, keys }), { sub: "alice", authVersion: 3, name: "앨리스", role: "admin" });
});

test("verifyIdToken rejects a wrong signature, issuer, audience, nonce, an expired token and malformed claims", async () => {
  const other = await generateKeyPair("RS256");
  const good = await signing();
  const cases: Array<[string, Awaited<ReturnType<typeof signing>>]> = [
    ["signature", { token: (await signing({ key: other.privateKey })).token, keys: good.keys }],
    ["issuer", await signing({ iss: "https://evil.example/oidc" })],
    ["audience", await signing({ aud: "console" })],
    ["nonce", await signing({ nonce: "other" })],
    ["expired", await signing({ exp: "-1m" })],
    ["auth_version", await signing({ auth_version: "3" })],
    ["empty sub", await signing({ sub: "x".repeat(121) })],
  ];
  for (const [name, { token, keys }] of cases) await assert.rejects(verifyIdToken(token, { ...expected, keys }), /invalid_token/, name);
  await assert.rejects(verifyIdToken("not.a.jwt", { ...expected, keys: good.keys }), /invalid_token/);
});

async function startAll() {
  const idpServer = createServer();
  const rpServer = createServer();
  await Promise.all([idpServer, rpServer].map((server) => new Promise<void>((done) => server.listen(0, "127.0.0.1", done))));
  const idpPort = (idpServer.address() as { port: number }).port;
  const rpPort = (rpServer.address() as { port: number }).port;
  const idpBase = `http://127.0.0.1:${idpPort}`;
  // One API process, two app hosts: naru is 127.0.0.1, the console is localhost (both loopback, so http is allowed).
  const rpBase = `http://127.0.0.1:${rpPort}`;
  const consoleBase = `http://localhost:${rpPort}`;
  const registered = [{ clientId: "naru", redirectUris: [`${rpBase}/api/auth/oidc/callback`], postLogoutRedirectUris: [`${rpBase}/`] },
    { clientId: "console", redirectUris: [`${consoleBase}/api/auth/oidc/callback`], postLogoutRedirectUris: [`${consoleBase}/`], maxAgeSeconds: 900 }];
  const env = { INAZINA_OIDC_ISSUER: `${idpBase}/oidc`, INAZINA_OIDC_CLIENTS: JSON.stringify(registered) };
  const { privateKey } = await generateKeyPair("RS256", { extractable: true });
  const jwk = { ...(await exportJWK(privateKey)), kid: "k1", alg: "RS256", use: "sig" } as Jwk;
  const provider = createProvider({ issuer: env.INAZINA_OIDC_ISSUER, clients: loadClients(env), call: memoryStore(), cookieKeys: ["idp-secret"], jwks: { keys: [jwk] },
    findAccount: async (id) => (id === "alice" ? alice : undefined) });
  const internalRequest: InternalRequest = async (_m, _p, _u, body) => {
    const credentials = body as { loginId: string; password: string };
    return credentials.loginId === "alice" && credentials.password === "pw" ? { status: 200, body: { user: alice } } : { status: 401, body: { error: "invalid_credentials" } };
  };
  const idp = createIdp({ provider, issuerHost: `127.0.0.1:${idpPort}`, internalRequest, clientIp: () => "203.0.113.9", appUrls: { home: `${rpBase}/` } });
  idpServer.on("request", async (request, response) => { if (!(await idp(request, response))) { response.writeHead(418); response.end(); } });
  const sessions: Array<[string, number]> = [];
  const rp = createRp({ env, authSecret: "rp-secret", issueSession: (userId, authVersion) => { sessions.push([userId, authVersion]); return `inazina_session=${userId}.${authVersion}; Path=/; HttpOnly`; } });
  rpServer.on("request", async (request, response) => {
    if (!(await rp.handle(request, response, new URL(request.url ?? "/", rpBase)))) { response.writeHead(418); response.end(); }
  });
  return { idpBase, rpBase, consoleBase, rpPort, sessions, rp, closeIdp: () => new Promise<void>((done) => idpServer.close(() => done())),
    close: () => Promise.all([idpServer, rpServer].map((server) => new Promise<void>((done) => server.close(() => done())))) };
}

test("the login route sends the browser to the provider with PKCE S256, state and nonce, and keeps the secrets in a signed cookie", async () => {
  const all = await startAll();
  try {
    const jar = new Jar();
    const response = await send(`${all.rpBase}/api/auth/oidc/login?returnTo=/portfolio`, jar);
    assert.equal(response.status, 302);
    const target = new URL(response.headers.get("location")!);
    assert.equal(target.origin + target.pathname, `${all.idpBase}/oidc/auth`);
    const query = Object.fromEntries(target.searchParams);
    assert.deepEqual({ client_id: query.client_id, response_type: query.response_type, scope: query.scope, code_challenge_method: query.code_challenge_method, max_age: query.max_age },
      { client_id: "naru", response_type: "code", scope: "openid profile", code_challenge_method: "S256", max_age: undefined });
    assert.ok(query.state && query.nonce && query.code_challenge && !target.href.includes("verifier"));
    const cookie = response.headers.getSetCookie()[0];
    assert.match(cookie, /^inazina_oidc_tx=/);
    assert.match(cookie, /HttpOnly/);
    assert.match(cookie, /SameSite=Lax/);
  } finally { await all.close(); }
});

test("the console host asks for a login no older than its max_age, as its own client", async () => {
  const all = await startAll();
  try {
    const target = new URL((await send(`${all.consoleBase}/api/auth/oidc/login`, new Jar())).headers.get("location")!);
    assert.equal(target.searchParams.get("max_age"), "900");
    assert.equal(target.searchParams.get("client_id"), "console");
    assert.equal(target.searchParams.get("redirect_uri"), `${all.consoleBase}/api/auth/oidc/callback`);
  } finally { await all.close(); }
});

const callbackPrefix = (rpBase: string) => `${rpBase}/api/auth/oidc/callback`;
async function loginAndGetCallback(all: Awaited<ReturnType<typeof startAll>>, rpJar: Jar, returnTo = "/portfolio") {
  const start = await send(`${all.rpBase}/api/auth/oidc/login?returnTo=${returnTo}`, rpJar);
  return idpSignIn(all.idpBase, new Jar(), start.headers.get("location")!, callbackPrefix(all.rpBase));
}

test("a full sign-in issues the app session from the verified ID token and returns to returnTo", async () => {
  const all = await startAll();
  try {
    const rpJar = new Jar();
    const callback = await loginAndGetCallback(all, rpJar);
    const response = await send(callback.href, rpJar);
    assert.equal(response.status, 302);
    assert.equal(response.headers.get("location"), "/portfolio");
    assert.deepEqual(all.sessions, [["alice", 3]]);
    assert.ok(response.headers.getSetCookie().some((line) => line.startsWith("inazina_session=alice.3")));
    assert.ok(response.headers.getSetCookie().some((line) => /^inazina_oidc_tx=;.*Max-Age=0/.test(line)), "the transaction cookie is cleared");
  } finally { await all.close(); }
});

test("a callback with the wrong state, without the transaction cookie, or with an IdP error issues no session", async () => {
  const all = await startAll();
  try {
    const rpJar = new Jar();
    const callback = await loginAndGetCallback(all, rpJar);
    const wrongState = new URL(callback.href); wrongState.searchParams.set("state", "forged");
    assert.equal((await send(wrongState.href, rpJar)).headers.get("location"), "/?login_error=state_mismatch");
    assert.equal((await send(callback.href, new Jar())).headers.get("location"), "/?login_error=login_expired");
    const denied = await loginAndGetCallback(all, rpJar);
    const errorUrl = new URL(denied.href); errorUrl.searchParams.delete("code"); errorUrl.searchParams.set("error", "access_denied");
    assert.equal((await send(errorUrl.href, rpJar)).headers.get("location"), "/?login_error=login_denied");
    assert.deepEqual(all.sessions, []);
  } finally { await all.close(); }
});

test("a replayed authorization code is refused by the provider and issues no session", async () => {
  const all = await startAll();
  try {
    const rpJar = new Jar();
    const callback = await loginAndGetCallback(all, rpJar);
    const saved = rpJar.header();
    assert.equal((await send(callback.href, rpJar)).headers.get("location"), "/portfolio");
    const replay = await send(callback.href, new Jar(), { headers: { cookie: saved } });
    assert.equal(replay.headers.get("location"), "/?login_error=login_failed");
    assert.equal(all.sessions.length, 1);
  } finally { await all.close(); }
});

test("an unreachable provider ends in oidc_unavailable instead of an error page or a redirect loop", async () => {
  const all = await startAll();
  try {
    const rpJar = new Jar();
    const callback = await loginAndGetCallback(all, rpJar);
    await all.closeIdp();
    const down = await send(callback.href, rpJar);
    assert.equal(down.status, 302);
    assert.match(down.headers.get("location")!, /^\/\?login_error=(oidc_unavailable|login_failed)$/);
    assert.deepEqual(all.sessions, []);
  } finally { await all.close(); }
});

test("logoutUrl points at the provider's end-session endpoint for this client and its registered return page", async () => {
  const all = await startAll();
  try {
    const url = new URL((await all.rp.logoutUrl(`127.0.0.1:${all.rpPort}`))!);
    assert.equal(url.origin + url.pathname, `${all.idpBase}/oidc/session/end`);
    assert.equal(url.searchParams.get("client_id"), "naru");
    assert.equal(url.searchParams.get("post_logout_redirect_uri"), `${all.rpBase}/`);
    assert.equal(new URL((await all.rp.logoutUrl(`localhost:${all.rpPort}`))!).searchParams.get("client_id"), "console");
    assert.equal(await all.rp.logoutUrl("evil.example"), undefined);
  } finally { await all.close(); }
});

test("a host that is no registered app is not handled, and one client's callback cannot be completed on another client's host", async () => {
  const all = await startAll();
  try {
    const stranger = await new Promise<number>((done, fail) => {
      httpRequest({ host: "127.0.0.1", port: all.rpPort, path: "/api/auth/oidc/login", headers: { host: "evil.example" } }, (response) => { response.resume(); done(response.statusCode!); }).on("error", fail).end();
    });
    assert.equal(stranger, 418, "an unknown host falls through to the API");
    const rpJar = new Jar();
    const callback = await loginAndGetCallback(all, rpJar);
    const crossed = await send(callback.href.replace(all.rpBase, all.consoleBase), rpJar);
    assert.equal(crossed.headers.get("location"), "/?login_error=login_expired");
    assert.deepEqual(all.sessions, []);
  } finally { await all.close(); }
});

test("servesHost is true only for the hosts of registered apps", async () => {
  const all = await startAll();
  try {
    assert.equal(all.rp.servesHost(`127.0.0.1:${all.rpPort}`), true);
    assert.equal(all.rp.servesHost(`LOCALHOST:${all.rpPort}`), true);
    assert.equal(all.rp.servesHost("127.0.0.1:4312"), false);
    assert.equal(all.rp.servesHost(undefined), false);
  } finally { await all.close(); }
});

test("login passes the requested account screen on as ui_mode, and drops anything that is not one", async () => {
  const all = await startAll();
  try {
    for (const mode of ["register", "reset-password", "find-id"]) {
      const target = new URL((await send(`${all.rpBase}/api/auth/oidc/login?mode=${mode}`, new Jar())).headers.get("location")!);
      assert.equal(target.searchParams.get("ui_mode"), mode);
    }
    for (const bad of ["evil", "login", "register%0d%0a", ""]) {
      const target = new URL((await send(`${all.rpBase}/api/auth/oidc/login?mode=${bad}`, new Jar())).headers.get("location")!);
      assert.equal(target.searchParams.has("ui_mode"), false, bad);
    }
  } finally { await all.close(); }
});
