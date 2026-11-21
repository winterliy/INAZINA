import test from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Jar, beginInteraction, bob, callbackUri, exchangeCode, fakeDiscord, followUntil, home, newVerifier, postJson, send, startIdp } from "./harness.js";

const json = async (response: Response) => response.json() as Promise<any>;
const get = (base: string, path: string, jar: Jar) => send(`${base}${path}`, jar);

// Signs the Discord way: start from the screen, "come back" from Discord, return the cookie jar holding the verification.
async function verifyWithDiscord(base: string, jar: Jar, uid: string, purpose: string, code = "good") {
  const start = await get(base, `/oidc/account/discord/start?purpose=${purpose}&uid=${uid}`, jar);
  const state = new URL(start.headers.get("location")!).searchParams.get("state")!;
  return get(base, `/oidc/account/discord/callback?code=${code}&state=${state}`, jar);
}

test("config says whether Discord is usable, which app asked and which screen to open first", async () => {
  const { base, close } = await startIdp();
  try {
    for (const [extra, expected] of [[{}, "login"], [{ ui_mode: "register" }, "register"], [{ ui_mode: "reset-password" }, "reset-password"], [{ ui_mode: "find-id" }, "find-id"], [{ ui_mode: "evil" }, "login"]] as const) {
      const jar = new Jar();
      const uid = await beginInteraction(base, jar, newVerifier(), extra);
      const response = await get(base, `/oidc/account/config?uid=${uid}`, jar);
      assert.equal(response.status, 200);
      assert.deepEqual(await json(response), { discordOAuthConfigured: true, clientId: "naru", startMode: expected });
    }
  } finally { await close(); }
});

test("account calls are tied to the caller's own login interaction", async () => {
  const { base, close } = await startIdp();
  try {
    const uid = await beginInteraction(base, new Jar());
    assert.equal((await get(base, `/oidc/account/config?uid=${uid}`, new Jar())).status, 400, "no interaction cookie");
    assert.equal((await get(base, `/oidc/account/config?uid=other`, new Jar())).status, 400);
    const jar = new Jar();
    await beginInteraction(base, jar);
    assert.equal((await get(base, `/oidc/account/config?uid=${uid}`, jar)).status, 400, "another interaction's uid");
  } finally { await close(); }
});

test("countries come from the data service", async () => {
  const { base, close } = await startIdp();
  try {
    const response = await get(base, "/oidc/account/countries", new Jar());
    assert.equal(response.status, 200);
    assert.deepEqual(await json(response), { countries: [{ countryId: "KR", currencyName: "원", currencyCode: "KRW" }] });
  } finally { await close(); }
});

test("a JSON login finishes the interaction and says where to go next", async () => {
  const { base, close } = await startIdp();
  try {
    const jar = new Jar();
    const codeVerifier = newVerifier();
    const uid = await beginInteraction(base, jar, codeVerifier);
    const response = await postJson(`${base}/oidc/interaction/${uid}/login`, jar, { loginId: "alice", password: "pw" });
    assert.equal(response.status, 200);
    const { redirectTo } = await json(response);
    const target = await followUntil(base, jar, await send(redirectTo, jar), callbackUri);
    assert.ok(target.searchParams.get("code"));
    assert.equal((await exchangeCode(base, target.searchParams.get("code")!, codeVerifier)).status, 200);
  } finally { await close(); }
});

test("JSON login errors: wrong password 401, unknown account 404, then 429 after eight failures", async () => {
  const { base, close } = await startIdp();
  try {
    const jar = new Jar();
    const uid = await beginInteraction(base, jar);
    const attempt = (loginId: string, password: string) => postJson(`${base}/oidc/interaction/${uid}/login`, jar, { loginId, password });
    let response = await attempt("alice", "nope");
    assert.equal(response.status, 401);
    assert.deepEqual(await json(response), { error: "invalid_credentials" });
    assert.equal((await attempt("ghost", "pw")).status, 404);
    for (let n = 0; n < 7; n += 1) await attempt("alice", "nope"); // eight failures for alice in all
    response = await attempt("alice", "pw");
    assert.equal(response.status, 429);
    assert.deepEqual(await json(response), { error: "login_rate_limited" });
  } finally { await close(); }
});

test("a login ends with the browser: the single sign-on session cookie has no expiry", async () => {
  const { base, close } = await startIdp();
  try {
    const jar = new Jar();
    const uid = await beginInteraction(base, jar);
    const { redirectTo } = await json(await postJson(`${base}/oidc/interaction/${uid}/login`, jar, { loginId: "alice", password: "pw" }));
    const resumed = await send(redirectTo, jar);
    const session = resumed.headers.getSetCookie().filter((line) => /^_session=/.test(line));
    assert.ok(session.length > 0, "a session cookie is set");
    for (const line of session) assert.doesNotMatch(line, /expires=|max-age=/i, line);
  } finally { await close(); }
});

test("Discord start sends the browser to Discord with a state, for the three purposes only", async () => {
  const { base, close } = await startIdp();
  try {
    const jar = new Jar();
    const uid = await beginInteraction(base, jar);
    for (const purpose of ["register", "find-id", "reset-password"]) {
      const response = await get(base, `/oidc/account/discord/start?purpose=${purpose}&uid=${uid}`, jar);
      assert.equal(response.status, 302);
      assert.match(response.headers.get("location")!, /^https:\/\/discord\.test\/authorize\?state=[\w-]+$/);
    }
    assert.equal((await get(base, `/oidc/account/discord/start?purpose=login&uid=${uid}`, jar)).status, 400);
    assert.equal((await get(base, `/oidc/account/discord/start?purpose=register&uid=nope`, jar)).status, 400);
  } finally { await close(); }
});

test("Discord start reports 503 when Discord is not configured", async () => {
  const { base, close } = await startIdp({ discord: fakeDiscord({ configured: false }) });
  try {
    const jar = new Jar();
    const uid = await beginInteraction(base, jar);
    const response = await get(base, `/oidc/account/discord/start?purpose=register&uid=${uid}`, jar);
    assert.equal(response.status, 503);
    assert.deepEqual(await json(response), { error: "discord_oauth_not_configured" });
  } finally { await close(); }
});

test("the Discord callback checks the state, then returns to the interaction with a readable verification", async () => {
  const { base, close } = await startIdp();
  try {
    const jar = new Jar();
    const uid = await beginInteraction(base, jar);
    const back = await verifyWithDiscord(base, jar, uid, "register");
    assert.equal(back.status, 302);
    assert.equal(back.headers.get("location"), `/oidc/interaction/${uid}?discordAuth=verified&purpose=register`);
    const verification = await get(base, "/oidc/account/discord/verification", jar);
    assert.deepEqual(await json(verification), { userId: bob.userId, nickname: bob.nickname, purpose: "register" });
    assert.equal((await get(base, "/oidc/account/discord/verification", new Jar())).status, 401);
  } finally { await close(); }
});

test("a forged state, a refused authorization and a failed lookup each come back with an error code", async () => {
  const { base, close } = await startIdp();
  try {
    const jar = new Jar();
    const uid = await beginInteraction(base, jar);
    const forged = await get(base, "/oidc/account/discord/callback?code=good&state=forged", jar);
    assert.equal(forged.headers.get("location"), `${home}?authError=oauth_state_invalid`);
    const start = async () => new URL((await get(base, `/oidc/account/discord/start?purpose=register&uid=${uid}`, jar)).headers.get("location")!).searchParams.get("state")!;
    assert.equal((await get(base, `/oidc/account/discord/callback?error=access_denied&state=${await start()}`, jar)).headers.get("location"), `/oidc/interaction/${uid}?authError=discord_authorization_failed`);
    assert.equal((await get(base, `/oidc/account/discord/callback?code=bad&state=${await start()}`, jar)).headers.get("location"), `/oidc/interaction/${uid}?authError=discord_verification_failed`);
    assert.equal((await get(base, "/oidc/account/discord/verification", jar)).status, 401, "no verification was granted");
  } finally { await close(); }
});

test("register needs the Discord verification, uses the verified name and id, and signs the new account in", async () => {
  const { base, calls, close } = await startIdp();
  try {
    const jar = new Jar();
    const codeVerifier = newVerifier();
    const uid = await beginInteraction(base, jar, codeVerifier, { ui_mode: "register" });
    const register = (body: object = {}) => postJson(`${base}/oidc/account/register`, jar, { uid, countryId: "KR", password: "a-long-password-1", ...body });
    const refused = await register();
    assert.equal(refused.status, 403);
    assert.deepEqual(await json(refused), { error: "discord_verification_required" });
    await verifyWithDiscord(base, jar, uid, "register");
    const savedCookies = jar.header();
    const created = await register();
    assert.equal(created.status, 200);
    const sent = calls.find((call) => call.path === "/internal/auth/register")!;
    assert.equal(sent.userId, bob.userId);
    assert.deepEqual(sent.body, { countryId: "KR", password: "a-long-password-1", nickname: bob.nickname });
    const target = await followUntil(base, jar, await send((await json(created)).redirectTo, jar), callbackUri);
    const tokens = await json(await exchangeCode(base, target.searchParams.get("code")!, codeVerifier));
    assert.equal(JSON.parse(Buffer.from(tokens.id_token.split(".")[1], "base64url").toString()).sub, bob.userId);
    const replay = await send(`${base}/oidc/account/register`, new Jar(), { method: "POST", headers: { "content-type": "application/json", cookie: savedCookies }, body: JSON.stringify({ uid, countryId: "KR", password: "x" }) });
    assert.ok(replay.status === 403 || replay.status === 400, "the verification cannot be used twice");
  } finally { await close(); }
});

test("a rejected registration passes the data service's answer through and keeps the verification", async () => {
  const { base, close } = await startIdp();
  try {
    const jar = new Jar();
    const uid = await beginInteraction(base, jar);
    await verifyWithDiscord(base, jar, uid, "register");
    const response = await postJson(`${base}/oidc/account/register`, jar, { uid, countryId: "KR", password: "dup" });
    assert.equal(response.status, 409);
    assert.deepEqual(await json(response), { error: "account_exists" });
    assert.equal((await get(base, "/oidc/account/discord/verification", jar)).status, 200, "the person can try again");
  } finally { await close(); }
});

test("a password reset works the same way, and a verification made for another purpose is refused", async () => {
  const { base, calls, close } = await startIdp();
  try {
    const jar = new Jar();
    const uid = await beginInteraction(base, jar);
    await verifyWithDiscord(base, jar, uid, "register");
    assert.equal((await postJson(`${base}/oidc/account/password-reset`, jar, { uid, password: "new-long-password-1" })).status, 403, "a register verification cannot reset a password");
    await verifyWithDiscord(base, jar, uid, "reset-password");
    const response = await postJson(`${base}/oidc/account/password-reset`, jar, { uid, password: "new-long-password-1" });
    assert.equal(response.status, 200);
    assert.ok(typeof (await json(response)).redirectTo === "string");
    const sent = calls.find((call) => call.path === "/internal/auth/password/reset")!;
    assert.equal(sent.userId, bob.userId);
    assert.deepEqual(sent.body, { password: "new-long-password-1", nickname: bob.nickname });
  } finally { await close(); }
});

test("account posts without the login interaction's cookie are refused", async () => {
  const { base, close } = await startIdp();
  try {
    const uid = await beginInteraction(base, new Jar());
    const response = await postJson(`${base}/oidc/account/register`, new Jar(), { uid, countryId: "KR", password: "x" });
    assert.equal(response.status, 400);
  } finally { await close(); }
});

const raw = (port: number, path: string) => new Promise<{ status: number; location?: string }>((done, fail) => {
  httpRequest({ host: "127.0.0.1", port, path, headers: { host: `127.0.0.1:${port}` } }, (response) => { response.resume(); done({ status: response.statusCode!, location: response.headers.location }); }).on("error", fail).end();
});

test("the built login screen is served for the interaction, with its files under /oidc/ui/ and nothing outside that folder", async () => {
  const dir = mkdtempSync(join(tmpdir(), "idp-ui-"));
  mkdirSync(join(dir, "assets"));
  writeFileSync(join(dir, "index.html"), "<!doctype html><title>id-ui</title>");
  writeFileSync(join(dir, "assets", "app-abc.js"), "console.log(1)");
  writeFileSync(join(dir, "..", `${dir.split("/").pop()}-secret.txt`), "secret");
  const { base, port, close } = await startIdp({ uiDir: dir });
  try {
    const jar = new Jar();
    const uid = await beginInteraction(base, jar);
    const page = await get(base, `/oidc/interaction/${uid}`, jar);
    assert.equal(page.status, 200);
    assert.match(page.headers.get("content-type")!, /text\/html/);
    assert.equal(page.headers.get("cache-control"), "no-store");
    assert.match(await page.text(), /id-ui/);
    const asset = await get(base, "/oidc/ui/assets/app-abc.js", jar);
    assert.equal(asset.status, 200);
    assert.match(asset.headers.get("content-type")!, /javascript/);
    assert.match(asset.headers.get("cache-control")!, /immutable/);
    for (const path of ["/oidc/ui/nope.js", "/oidc/ui/assets", "/oidc/ui/%2e%2e/" + dir.split("/").pop() + "-secret.txt", "/oidc/ui/..%2f..%2fetc%2fpasswd", "/oidc/ui/index.html"]) {
      assert.equal((await raw(port, path)).status, 404, path);
    }
  } finally { await close(); rmSync(dir, { recursive: true, force: true }); rmSync(join(dir, "..", `${dir.split("/").pop()}-secret.txt`), { force: true }); }
});

test("direct visits to /login, /register and /reset start a login at the app, on the right screen", async () => {
  const { port, close } = await startIdp();
  try {
    assert.deepEqual(await raw(port, "/register"), { status: 302, location: `${home}api/auth/oidc/login?mode=register` });
    assert.deepEqual(await raw(port, "/reset"), { status: 302, location: `${home}api/auth/oidc/login?mode=reset-password` });
    assert.deepEqual(await raw(port, "/login"), { status: 302, location: `${home}api/auth/oidc/login` });
  } finally { await close(); }
});

