// Helpers for tests that drive the identity provider the way a browser does: a cookie jar, manual redirects, PKCE values.
import { createHash, randomBytes } from "node:crypto";

export class Jar {
  cookies = new Map<string, string>();
  absorb(response: Response): void {
    for (const line of response.headers.getSetCookie()) {
      const [pair] = line.split(";");
      const index = pair.indexOf("=");
      const value = pair.slice(index + 1);
      if (value === "" || /;\s*max-age=0/i.test(line)) this.cookies.delete(pair.slice(0, index)); else this.cookies.set(pair.slice(0, index), value);
    }
  }
  header(): string { return [...this.cookies].map(([name, value]) => `${name}=${value}`).join("; "); }
}

export async function send(url: string, jar: Jar, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(url, { redirect: "manual", ...init, headers: { cookie: jar.header(), ...(init.headers as Record<string, string> | undefined) } });
  jar.absorb(response);
  return response;
}

export const newVerifier = (): string => randomBytes(32).toString("base64url");
export const challengeOf = (verifier: string): string => createHash("sha256").update(verifier).digest("base64url");

// Follows redirects (resolving relative ones against `base`) until one leaves for `stopPrefix`; returns that URL.
export async function followUntil(base: string, jar: Jar, first: Response, stopPrefix: string): Promise<URL> {
  let response = first;
  for (let hop = 0; hop < 8; hop += 1) {
    const location = new URL(response.headers.get("location") ?? "", base);
    if (location.href.startsWith(stopPrefix)) return location;
    response = await send(location.href, jar);
  }
  throw new Error("redirect chain did not reach " + stopPrefix);
}

// Signs in at the identity provider as a browser would: starts at `authorizeUrl`, posts the login form, follows to `stopPrefix`.
export async function idpSignIn(idpBase: string, jar: Jar, authorizeUrl: string, stopPrefix: string, loginId = "alice", password = "pw"): Promise<URL> {
  const first = await send(authorizeUrl, jar);
  const uid = /\/oidc\/interaction\/([\w-]+)$/.exec(first.headers.get("location") ?? "")?.[1];
  if (!uid) throw new Error("no interaction: " + first.status + " " + first.headers.get("location"));
  const posted = await send(`${idpBase}/oidc/interaction/${uid}/login`, jar, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ loginId, password }) });
  return followUntil(idpBase, jar, posted, stopPrefix);
}
