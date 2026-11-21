import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, statSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { generateKeyPair, exportJWK } from "jose";
import { createProvider, mount, loadJwks, claimsFor, providerFindAccount, type Account, type Jwk } from "./provider.js";
import { createAdapterClass } from "./adapter.js";
import { memoryStore } from "./memoryStore.js";
import type { OidcClient } from "./clients.js";

const callback = "https://naru.inazina.localhost/api/auth/oidc/callback";
const clients: OidcClient[] = [{ clientId: "naru", redirectUris: [callback], postLogoutRedirectUris: ["https://naru.inazina.localhost/"] },
  { clientId: "console", redirectUris: ["https://console.inazina.localhost/api/auth/oidc/callback"], postLogoutRedirectUris: [], maxAgeSeconds: 900 }];
const alice: Account = { userId: "alice", nickname: "앨리스", role: "admin", authVersion: 3 };

async function start() {
  const server = createServer();
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const { privateKey } = await generateKeyPair("RS256", { extractable: true });
  const jwk = { ...(await exportJWK(privateKey)), kid: "test", alg: "RS256", use: "sig" } as Jwk;
  const provider = await createProvider({
    issuer: `${base}/oidc`, clients, call: memoryStore(), cookieKeys: ["test-secret"], jwks: { keys: [jwk] },
    findAccount: async (id) => (id === "alice" ? alice : undefined),
  });
  server.on("request", mount(provider));
  return { base, provider, close: () => new Promise<void>((done) => server.close(() => done())) };
}

test("discovery lists only the code flow, S256 and openid profile", async () => {
  const { base, close } = await start();
  try {
    const metadata = await (await fetch(`${base}/oidc/.well-known/openid-configuration`)).json() as Record<string, string[] | string>;
    assert.equal(metadata.issuer, `${base}/oidc`);
    assert.deepEqual(metadata.response_types_supported, ["code"]);
    assert.deepEqual(metadata.code_challenge_methods_supported, ["S256"]);
    assert.ok((metadata.scopes_supported as string[]).includes("openid") && (metadata.scopes_supported as string[]).includes("profile"));
    assert.ok(!(metadata.scopes_supported as string[]).includes("offline_access"));
    assert.ok(!(metadata.grant_types_supported as string[]).includes("implicit"));
  } finally { await close(); }
});

const authorize = (base: string, extra: Record<string, string>) =>
  fetch(`${base}/oidc/auth?` + new URLSearchParams({ client_id: "naru", response_type: "code", scope: "openid", redirect_uri: callback, state: "s1", nonce: "n1", ...extra }), { redirect: "manual" });

test("authorization request without code_challenge is rejected", async () => {
  const { base, close } = await start();
  try {
    const response = await authorize(base, {});
    assert.equal(response.status, 303);
    const location = response.headers.get("location") ?? "";
    assert.ok(location.startsWith(callback), location);
    assert.match(location, /error=invalid_request/);
  } finally { await close(); }
});

test("a plain code_challenge_method is rejected", async () => {
  const { base, close } = await start();
  try {
    const response = await authorize(base, { code_challenge: "x".repeat(43), code_challenge_method: "plain" });
    assert.match(response.headers.get("location") ?? "", /error=invalid_request/);
  } finally { await close(); }
});

test("an unregistered redirect_uri is rejected without redirecting", async () => {
  const { base, close } = await start();
  try {
    const response = await authorize(base, { redirect_uri: "https://evil.example/cb", code_challenge: "x".repeat(43), code_challenge_method: "S256" });
    assert.equal(response.status, 400);
    assert.equal(response.headers.get("location"), null);
  } finally { await close(); }
});

test("a valid PKCE request is sent to the interaction page", async () => {
  const { base, close } = await start();
  try {
    const response = await authorize(base, { code_challenge: "x".repeat(43), code_challenge_method: "S256" });
    assert.equal(response.status, 303);
    assert.match(response.headers.get("location") ?? "", /\/oidc\/interaction\/[\w-]+$/);
  } finally { await close(); }
});

test("claims carry sub, name, role and auth_version only for the profile scope", () => {
  assert.deepEqual(claimsFor(alice, "openid profile"), { sub: "alice", name: "앨리스", role: "admin", auth_version: 3 });
  assert.deepEqual(claimsFor(alice, "openid"), { sub: "alice" });
});

test("providerFindAccount resolves known users with scope-dependent claims and unknown users to nothing", async () => {
  const find = providerFindAccount(async (id) => (id === "alice" ? alice : undefined));
  const found = await find({} as never, "alice");
  assert.equal(found!.accountId, "alice");
  assert.deepEqual(await found!.claims("id_token", "openid profile", {}, []), claimsFor(alice, "openid profile"));
  assert.deepEqual(await found!.claims("id_token", "openid", {}, []), { sub: "alice" });
  assert.equal(await find({} as never, "nobody"), undefined);
});

test("the adapter maps provider operations to the store's argument names", async () => {
  const sent: Array<[string, Record<string, unknown>]> = [];
  const Adapter = createAdapterClass(async (op, body) => { sent.push([op, body]); return { result: op === "find" ? null : undefined }; });
  const adapter = new Adapter("Session");
  const before = Math.floor(Date.now() / 1000);
  await adapter.upsert("s1", { uid: "u1", grantId: "g1", userCode: "c1", extra: { n: 1 } }, 60);
  const [op, body] = sent[0];
  assert.equal(op, "upsert");
  assert.deepEqual({ ...body, expires_at: undefined }, { model: "Session", id: "s1", payload: { uid: "u1", grantId: "g1", userCode: "c1", extra: { n: 1 } }, expires_at: undefined, grant_id: "g1", uid: "u1", user_code: "c1" });
  assert.ok((body.expires_at as number) >= before + 60 && (body.expires_at as number) <= before + 62);
  assert.equal(await adapter.find("missing"), undefined);
  await adapter.consume("s1"); await adapter.destroy("s1"); await adapter.revokeByGrantId("g1");
  assert.deepEqual(await adapter.findByUid("u1"), undefined);
  assert.deepEqual(sent.slice(2).map(([name, args]) => [name, args]), [
    ["consume", { model: "Session", id: "s1" }], ["destroy", { model: "Session", id: "s1" }],
    ["revoke_by_grant_id", { grant_id: "g1" }], ["find_by_uid", { model: "Session", uid: "u1" }]]);
});

test("loadJwks creates a private key file once, mode 600, and reuses it; the env value wins", async () => {
  const dir = mkdtempSync(join(tmpdir(), "oidc-jwks-"));
  try {
    const file = join(dir, "jwks.json");
    const first = await loadJwks({}, file);
    assert.equal(first.keys.length, 1);
    assert.ok(first.keys[0].d && first.keys[0].kid && first.keys[0].alg === "RS256");
    assert.equal(statSync(file).mode & 0o777, 0o600);
    assert.equal((await loadJwks({}, file)).keys[0].kid, first.keys[0].kid);
    const fromEnv = await loadJwks({ INAZINA_OIDC_JWKS: JSON.stringify({ keys: [{ ...first.keys[0], kid: "from-env" }] }) }, file);
    assert.equal(fromEnv.keys[0].kid, "from-env");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
