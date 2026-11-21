import test from "node:test";
import assert from "node:assert/strict";
import { createDiscordClient } from "./discord.js";

const redirect = "https://id.inazina.kr/oidc/account/discord/callback";
const env = { DISCORD_CLIENT_ID: "client-1", DISCORD_CLIENT_SECRET: "secret-1" };

test("without credentials the client is not configured", () => {
  assert.equal(createDiscordClient({}, redirect).configured, false);
  assert.equal(createDiscordClient({ DISCORD_CLIENT_ID: "x" }, redirect).configured, false);
  assert.equal(createDiscordClient(env, redirect).configured, true);
});

test("the authorization address asks for identify only, with our callback and the state", () => {
  const url = new URL(createDiscordClient(env, redirect).authorizeUrl("st4te"));
  assert.equal(url.origin + url.pathname, "https://discord.com/oauth2/authorize");
  assert.deepEqual(Object.fromEntries(url.searchParams), { response_type: "code", client_id: "client-1", scope: "identify", redirect_uri: redirect, state: "st4te" });
});

function fakeFetch(handlers: { token?: () => Response; user?: () => Response }) {
  const seen: Array<{ url: string; init?: RequestInit }> = [];
  const fetchImpl = (async (url: string, init?: RequestInit) => {
    seen.push({ url: String(url), init });
    return String(url).endsWith("/oauth2/token") ? (handlers.token?.() ?? new Response("{}", { status: 400 })) : (handlers.user?.() ?? new Response("{}", { status: 401 }));
  }) as typeof fetch;
  return { fetchImpl, seen };
}
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

test("identify exchanges the code, then reads the Discord id with the nickname (global name, else username, else id)", async () => {
  const { fetchImpl, seen } = fakeFetch({ token: () => json({ access_token: "tok", token_type: "Bearer" }), user: () => json({ id: "42", global_name: "Global", username: "user" }) });
  assert.deepEqual(await createDiscordClient(env, redirect, fetchImpl).identify("the-code"), { id: "42", nickname: "Global" });
  assert.match(String(seen[0].init?.body), /grant_type=authorization_code/);
  assert.match(String(seen[0].init?.body), /code=the-code/);
  assert.match(String(seen[0].init?.body), /redirect_uri=https%3A%2F%2Fid\.inazina\.kr/);
  assert.equal((seen[1].init?.headers as Record<string, string>).authorization, "Bearer tok");
  const second = fakeFetch({ token: () => json({ access_token: "t" }), user: () => json({ id: "43", global_name: null, username: "plain" }) });
  assert.equal((await createDiscordClient(env, redirect, second.fetchImpl).identify("c")).nickname, "plain");
  const third = fakeFetch({ token: () => json({ access_token: "t" }), user: () => json({ id: "44" }) });
  assert.equal((await createDiscordClient(env, redirect, third.fetchImpl).identify("c")).nickname, "44");
});

test("identify fails on a refused code, a missing token, a failed lookup and an id that is not numeric", async () => {
  const cases: Array<[string, Parameters<typeof fakeFetch>[0]]> = [
    ["refused code", { token: () => json({}, 400) }],
    ["no token", { token: () => json({}) }],
    ["lookup failed", { token: () => json({ access_token: "t" }), user: () => json({}, 401) }],
    ["bad id", { token: () => json({ access_token: "t" }), user: () => json({ id: "../../x" }) }],
  ];
  for (const [name, handlers] of cases) await assert.rejects(createDiscordClient(env, redirect, fakeFetch(handlers).fetchImpl).identify("c"), /discord_/, name);
});
