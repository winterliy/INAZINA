// The OIDC clients (the INAZINA web apps) the identity provider serves. Registered from the environment,
// not the database: exact redirect URIs only, no wildcards.
export type OidcClient = {
  clientId: string;
  redirectUris: string[];
  postLogoutRedirectUris: string[];
  // Ask for a login at most this old (seconds); the admin console uses it so a shared browser's old session is not enough.
  maxAgeSeconds?: number;
};

const loopbackHosts = new Set(["localhost", "127.0.0.1", "[::1]"]);

function checkUri(clientId: string, uri: string): void {
  if (uri.includes("*")) throw new Error(`client ${clientId}: wildcard in URI ${uri}`);
  let parsed: URL;
  try { parsed = new URL(uri); } catch { throw new Error(`client ${clientId}: invalid URI ${uri}`); }
  if (parsed.hash) throw new Error(`client ${clientId}: fragment in URI ${uri}`);
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && loopbackHosts.has(parsed.hostname))) {
    throw new Error(`client ${clientId}: URI must use https (http only for loopback): ${uri}`);
  }
}

function validate(raw: unknown): OidcClient[] {
  if (!Array.isArray(raw)) throw new Error("INAZINA_OIDC_CLIENTS must be a JSON array");
  const seen = new Set<string>();
  return raw.map((entry) => {
    const client = entry as OidcClient;
    if (!client || typeof client.clientId !== "string" || !client.clientId) throw new Error("client without clientId");
    if (seen.has(client.clientId)) throw new Error(`duplicate clientId ${client.clientId}`);
    seen.add(client.clientId);
    if (!Array.isArray(client.redirectUris) || client.redirectUris.length === 0) throw new Error(`client ${client.clientId}: redirectUris required`);
    const postLogoutRedirectUris = client.postLogoutRedirectUris ?? [];
    [...client.redirectUris, ...postLogoutRedirectUris].forEach((uri) => checkUri(client.clientId, uri));
    return { clientId: client.clientId, redirectUris: client.redirectUris, postLogoutRedirectUris,
      ...(client.maxAgeSeconds ? { maxAgeSeconds: client.maxAgeSeconds } : {}) };
  });
}

export function loadClients(env: NodeJS.ProcessEnv): OidcClient[] {
  if (env.INAZINA_OIDC_CLIENTS) {
    let parsed: unknown;
    try { parsed = JSON.parse(env.INAZINA_OIDC_CLIENTS); } catch { throw new Error("INAZINA_OIDC_CLIENTS is not valid JSON"); }
    return validate(parsed);
  }
  const domain = env.INAZINA_DOMAIN || "inazina.localhost";
  const app = (clientId: string, maxAgeSeconds?: number): OidcClient => ({
    clientId, redirectUris: [`https://${clientId}.${domain}/api/auth/oidc/callback`], postLogoutRedirectUris: [`https://${clientId}.${domain}/`],
    ...(maxAgeSeconds ? { maxAgeSeconds } : {}),
  });
  return validate([app("naru"), app("console", 900), app("work")]);
}
