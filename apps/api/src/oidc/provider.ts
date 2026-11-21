// The INAZINA identity provider: node-oidc-provider configured for the authorization code flow with PKCE (S256) only.
import { randomBytes } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";
import { exportJWK, generateKeyPair } from "jose";
import Provider, { type AccountClaims, type Configuration } from "oidc-provider";
import { createAdapterClass, type StoreCall } from "./adapter.js";
import type { OidcClient } from "./clients.js";

export type Account = { userId: string; nickname: string; role: string; authVersion: number };
export type Jwk = { kty: string; kid?: string; alg?: string; use?: string; d?: string; [member: string]: unknown };
export type Jwks = { keys: Jwk[] };

export type ProviderDeps = {
  issuer: string; // e.g. https://id.inazina.kr/oidc; the provider is mounted at the issuer's path
  clients: OidcClient[];
  call: StoreCall;
  cookieKeys: string[];
  jwks: Jwks;
  findAccount: (userId: string) => Promise<Account | undefined>;
};

const mountPath = "/oidc";

export function claimsFor(account: Account, scope: string): AccountClaims {
  const claims: AccountClaims = { sub: account.userId };
  if (scope.split(" ").includes("profile")) Object.assign(claims, { name: account.nickname, role: account.role, auth_version: account.authVersion });
  return claims;
}

// The provider's account lookup: the claims it returns for an ID token depend on the requested scope.
export function providerFindAccount(find: ProviderDeps["findAccount"]): NonNullable<Configuration["findAccount"]> {
  return async (_ctx, id) => {
    const account = await find(id);
    return account ? { accountId: account.userId, claims: async (_use, scope) => claimsFor(account, scope) } : undefined;
  };
}

export function createProvider(deps: ProviderDeps): Provider {
  const configuration: Configuration = {
    adapter: createAdapterClass(deps.call),
    clients: deps.clients.map((client) => ({
      client_id: client.clientId, token_endpoint_auth_method: "none", application_type: "web",
      redirect_uris: client.redirectUris, post_logout_redirect_uris: client.postLogoutRedirectUris,
      grant_types: ["authorization_code"], response_types: ["code"],
      ...(client.maxAgeSeconds ? { default_max_age: client.maxAgeSeconds } : {}),
    })),
    cookies: { keys: deps.cookieKeys },
    jwks: deps.jwks as Configuration["jwks"],
    scopes: ["openid"],
    claims: { openid: ["sub"], profile: ["name", "role", "auth_version"] },
    responseTypes: ["code"],
    // The apps read name, role and auth_version from the verified ID token itself (no userinfo round trip).
    conformIdTokenClaims: false,
    pkce: { required: () => true }, // S256 is the library default; the discovery test pins it
    issueRefreshToken: async () => false,
    ttl: { AuthorizationCode: 60, AccessToken: 300, IdToken: 300, Interaction: 600, Session: 43_200, Grant: 43_200 },
    interactions: { url: (_ctx, interaction) => `${mountPath}/interaction/${interaction.uid}` },
    features: {
      devInteractions: { enabled: false },
      // Signing out is confirmed automatically: the page submits its own form, so a sign-out request ends the session at once.
      rpInitiatedLogout: {
        logoutSource: (ctx, form) => {
          ctx.body = `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="robots" content="noindex"><title>로그아웃 · inazina</title></head><body>${form}<p>로그아웃하는 중입니다…</p><button autofocus type="submit" form="op.logoutForm" value="yes" name="logout">로그아웃</button><script>document.querySelector("button").click()</script></body></html>`;
        },
        postLogoutSuccessSource: (ctx) => {
          ctx.body = `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="robots" content="noindex"><title>로그아웃 · inazina</title></head><body><p>로그아웃되었습니다.</p></body></html>`;
        },
      },
    },
    findAccount: providerFindAccount(deps.findAccount),
  };
  const provider = new Provider(deps.issuer, configuration);
  if (deps.issuer.startsWith("https:")) provider.proxy = true; // behind Caddy: trust X-Forwarded-Proto/Host
  return provider;
}

// A plain Node handler for requests under /oidc (the prefix is stripped the way a router mount would).
export function mount(provider: Provider): (request: IncomingMessage, response: ServerResponse) => void {
  const callback = provider.callback();
  return (request, response) => {
    const url = request.url ?? "/";
    if (url !== mountPath && !url.startsWith(`${mountPath}/`) && !url.startsWith(`${mountPath}?`)) {
      response.writeHead(404, { "content-length": "0" });
      response.end();
      return;
    }
    (request as IncomingMessage & { originalUrl?: string }).originalUrl = url;
    request.url = url.slice(mountPath.length) || "/";
    callback(request, response);
  };
}

// The signing keys: INAZINA_OIDC_JWKS (JSON) if set, else a private key file created once (mode 600).
export async function loadJwks(env: NodeJS.ProcessEnv, file: string): Promise<Jwks> {
  if (env.INAZINA_OIDC_JWKS) return JSON.parse(env.INAZINA_OIDC_JWKS) as Jwks;
  const read = async () => JSON.parse(await readFile(file, "utf8")) as Jwks;
  try { return await read(); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  const { privateKey } = await generateKeyPair("RS256", { extractable: true });
  const jwks: Jwks = { keys: [{ ...(await exportJWK(privateKey)), kid: randomBytes(8).toString("hex"), alg: "RS256", use: "sig" } as Jwk] };
  await mkdir(dirname(file), { recursive: true });
  try {
    await writeFile(file, JSON.stringify(jwks), { mode: 0o600, flag: "wx" });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") return read(); // another process created it first
    throw error;
  }
  return jwks;
}
