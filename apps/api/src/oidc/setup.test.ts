import test from "node:test";
import assert from "node:assert/strict";
import { createServer, request as httpRequest } from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setupOidc } from "./setup.js";
import type { InternalRequest } from "./idp.js";

const internalRequest: InternalRequest = async (_method, _path, userId) =>
  userId === "alice" ? { status: 200, body: { user: { userId: "alice", nickname: "앨리스", role: "user", authVersion: 1 } } } : { status: 404, body: {} };
const base = (env: NodeJS.ProcessEnv, dir: string) => ({ env, jwksFile: join(dir, "jwks.json"), uiDir: join(dir, "ui"), authSecret: "secret", internalRequest, clientIp: () => "203.0.113.9", issueSession: (userId: string, authVersion: number) => `s=${userId}.${authVersion}` });

async function through(handler: (request: any, response: any) => Promise<boolean>, host: string, path: string) {
  const server = createServer(async (request, response) => { if (!(await handler(request, response))) { response.writeHead(418); response.end(); } });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const port = (server.address() as { port: number }).port;
  try {
    return await new Promise<{ status: number; location?: string }>((done, fail) => {
      httpRequest({ host: "127.0.0.1", port, path, headers: { host } }, (response) => { response.resume(); done({ status: response.statusCode!, location: response.headers.location }); }).on("error", fail).end();
    });
  } finally { await new Promise<void>((done) => server.close(() => done())); }
}

test("without INAZINA_OIDC_ISSUER nothing is set up and the password login stays", async () => {
  const dir = mkdtempSync(join(tmpdir(), "oidc-setup-"));
  try { assert.deepEqual(await setupOidc(base({}, dir)), {}); } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("the issuer must end in /oidc, where the provider is mounted", async () => {
  const dir = mkdtempSync(join(tmpdir(), "oidc-setup-"));
  try {
    await assert.rejects(setupOidc(base({ INAZINA_OIDC_ISSUER: "https://id.inazina.kr" }, dir)), /\/oidc/);
    await assert.rejects(setupOidc(base({ INAZINA_OIDC_ISSUER: "https://id.inazina.kr/other" }, dir)), /\/oidc/);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("with an issuer the identity provider answers its own host and sends registration to the apps on the same domain", async () => {
  const dir = mkdtempSync(join(tmpdir(), "oidc-setup-"));
  try {
    const { idp } = await setupOidc(base({ INAZINA_OIDC_ISSUER: "https://id.inazina.kr/oidc", INAZINA_DOMAIN: "inazina.kr" }, dir));
    assert.ok(idp);
    assert.deepEqual(await through(idp, "id.inazina.kr", "/register"), { status: 302, location: "https://naru.inazina.kr/api/auth/oidc/login?mode=register" });
    assert.deepEqual(await through(idp, "id.inazina.kr", "/reset"), { status: 302, location: "https://naru.inazina.kr/api/auth/oidc/login?mode=reset-password" });
    assert.equal((await through(idp, "id.inazina.kr", "/api/health")).status, 404);
    assert.equal((await through(idp, "127.0.0.1:4312", "/api/health")).status, 418);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("the discovery document is served on the identity provider host with the configured issuer", async () => {
  const dir = mkdtempSync(join(tmpdir(), "oidc-setup-"));
  try {
    const { idp } = await setupOidc(base({ INAZINA_OIDC_ISSUER: "https://id.inazina.localhost/oidc" }, dir));
    assert.equal((await through(idp!, "id.inazina.localhost", "/oidc/.well-known/openid-configuration")).status, 200);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("with an issuer the app hosts get the relying party; the API's own host does not", async () => {
  const dir = mkdtempSync(join(tmpdir(), "oidc-setup-"));
  try {
    const { rp } = await setupOidc(base({ INAZINA_OIDC_ISSUER: "https://id.inazina.localhost/oidc" }, dir));
    assert.ok(rp);
    const handleRp = (request: any, response: any) => rp.handle(request, response, new URL(request.url, "http://x"));
    assert.equal(rp.servesHost("naru.inazina.localhost"), true);
    assert.equal(rp.servesHost("console.inazina.localhost"), true);
    assert.equal(rp.servesHost("127.0.0.1:4312"), false);
    assert.equal((await through(handleRp, "naru.inazina.localhost", "/api/auth/oidc/callback")).status, 302, "handled: the callback redirects (no transaction cookie)");
    assert.equal((await through(handleRp, "127.0.0.1:4312", "/api/auth/oidc/callback")).status, 418);
    assert.equal((await setupOidc(base({}, dir))).rp, undefined);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
