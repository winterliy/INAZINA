import test from "node:test";
import assert from "node:assert/strict";
import { loadClients } from "./clients.js";

const one = (overrides: object = {}) => JSON.stringify([{ clientId: "naru", redirectUris: ["https://naru.inazina.kr/cb"], postLogoutRedirectUris: ["https://naru.inazina.kr/"], ...overrides }]);

test("defaults register naru, console and work on the local domain", () => {
  const clients = loadClients({});
  assert.deepEqual(clients.map((client) => client.clientId), ["naru", "console", "work"]);
  assert.deepEqual(clients[2].redirectUris, ["https://work.inazina.localhost/api/auth/oidc/callback"]);
  assert.equal(clients[2].maxAgeSeconds, undefined);
  assert.deepEqual(clients[0].redirectUris, ["https://naru.inazina.localhost/api/auth/oidc/callback"]);
  assert.deepEqual(clients[0].postLogoutRedirectUris, ["https://naru.inazina.localhost/"]);
  assert.equal(clients[0].maxAgeSeconds, undefined);
  assert.equal(clients[1].maxAgeSeconds, 900);
});

test("INAZINA_DOMAIN moves the default clients to that domain", () => {
  assert.deepEqual(loadClients({ INAZINA_DOMAIN: "inazina.kr" })[1].redirectUris, ["https://console.inazina.kr/api/auth/oidc/callback"]);
});

test("INAZINA_OIDC_CLIENTS overrides the defaults", () => {
  assert.deepEqual(loadClients({ INAZINA_OIDC_CLIENTS: one() }).map((client) => client.clientId), ["naru"]);
});

test("loadClients rejects wildcard redirect URIs", () => {
  assert.throws(() => loadClients({ INAZINA_OIDC_CLIENTS: one({ redirectUris: ["https://*.inazina.kr/cb"] }) }), /wildcard/);
});

test("loadClients rejects http redirect URIs unless the host is loopback", () => {
  assert.throws(() => loadClients({ INAZINA_OIDC_CLIENTS: one({ redirectUris: ["http://naru.inazina.kr/cb"] }) }), /https/);
  assert.equal(loadClients({ INAZINA_OIDC_CLIENTS: one({ redirectUris: ["http://localhost:4311/cb"] }) })[0].redirectUris[0], "http://localhost:4311/cb");
});

test("loadClients rejects fragments, missing redirects and unparsable values", () => {
  assert.throws(() => loadClients({ INAZINA_OIDC_CLIENTS: one({ redirectUris: ["https://naru.inazina.kr/cb#x"] }) }), /fragment/);
  assert.throws(() => loadClients({ INAZINA_OIDC_CLIENTS: one({ redirectUris: [] }) }), /redirect/);
  assert.throws(() => loadClients({ INAZINA_OIDC_CLIENTS: "not json" }), /INAZINA_OIDC_CLIENTS/);
});

test("loadClients rejects duplicate clientId", () => {
  const twice = JSON.stringify([JSON.parse(one())[0], JSON.parse(one())[0]]);
  assert.throws(() => loadClients({ INAZINA_OIDC_CLIENTS: twice }), /duplicate/);
});
