// The login server (identity provider) and the sign-in of the app hosts. The apps (naru, console) have no login of their own,
// so this is always on: the issuer is INAZINA_OIDC_ISSUER, or https://id.<INAZINA_DOMAIN>/oidc (default domain inazina.localhost).
import type { IncomingMessage, ServerResponse } from "node:http";
import { loadClients } from "./clients.js";
import { createDiscordClient } from "./discord.js";
import { createIdp, type InternalRequest } from "./idp.js";
import { createProvider, loadJwks } from "./provider.js";
import { createRp } from "./rp.js";

export type OidcSetup = {
  idp: (request: IncomingMessage, response: ServerResponse) => Promise<boolean>; // the identity provider's own host
  rp: ReturnType<typeof createRp>; // sign-in for the app hosts (naru, console)
};

export type SetupDeps = {
  env: NodeJS.ProcessEnv;
  jwksFile: string;
  uiDir: string; // the built login screen (apps/id/dist); a server-rendered login page is used when it has not been built
  authSecret: string;
  internalRequest: InternalRequest;
  clientIp: (request: IncomingMessage) => string;
  issueSession: (userId: string, authVersion: number) => string; // the Set-Cookie value of an app session
};

export async function setupOidc(deps: SetupDeps): Promise<OidcSetup> {
  const { env, internalRequest } = deps;
  const domain = env.INAZINA_DOMAIN || "inazina.localhost";
  const issuer = new URL(env.INAZINA_OIDC_ISSUER || `https://id.${domain}/oidc`);
  if (issuer.pathname !== "/oidc") throw new Error("INAZINA_OIDC_ISSUER must end in /oidc (the provider is mounted there), e.g. https://id.inazina.kr/oidc");
  const home = `https://naru.${domain}/`;
  const provider = createProvider({
    issuer: issuer.href, clients: loadClients(env), call: async (operation, body) => (await internalRequest("POST", `/internal/oidc/store/${operation}`, undefined, body)).body,
    cookieKeys: [deps.authSecret], jwks: await loadJwks(env, deps.jwksFile),
    findAccount: async (userId) => {
      const result = await internalRequest("GET", "/internal/me", userId);
      const user = result.body?.user;
      return result.status === 200 && user ? { userId: user.userId, nickname: user.nickname, role: user.role, authVersion: user.authVersion } : undefined;
    },
  });
  return {
    rp: createRp({ env: { ...env, INAZINA_OIDC_ISSUER: issuer.href }, authSecret: deps.authSecret, issueSession: deps.issueSession }),
    idp: createIdp({
      provider, issuerHost: issuer.host, internalRequest, clientIp: deps.clientIp, appUrls: { home },
      // Discord proves who owns an account for register / reset; its callback is on the identity provider itself.
      discord: createDiscordClient(env, `${issuer.origin}/oidc/account/discord/callback`), uiDir: deps.uiDir, secret: deps.authSecret, secureCookies: issuer.protocol === "https:",
    }),
  };
}
