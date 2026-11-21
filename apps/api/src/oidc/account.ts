// The account screens' server side on the identity provider: which screen to open, countries, Discord verification, and
// registering / resetting a password (which ends by signing the person in to the login interaction they came from).
import { randomBytes } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import type Provider from "oidc-provider";
import type { DiscordClient } from "./discord.js";
import { interactionFor, readJson, sendJson, type AppUrls, type InternalRequest } from "./interaction.js";
import { readCookie, type Signer } from "./signed.js";

export type AccountDeps = { provider: Provider; internalRequest: InternalRequest; discord: DiscordClient; signer: Signer; secureCookies: boolean; appUrls: AppUrls };

const modes = new Set(["register", "reset-password", "find-id"]);
const stateCookie = "idp_discord_state";
const verifiedCookie = "idp_discord";
const prefix = "/oidc/account/";

export function createAccountHandler(deps: AccountDeps): (request: IncomingMessage, response: ServerResponse, url: URL) => Promise<boolean> {
  const { provider, discord, signer } = deps;
  const used = new Map<string, number>(); // verification nonces already spent, until they would have expired anyway
  const secure = deps.secureCookies ? "; Secure" : "";
  const cookie = (name: string, value: string, maxAge: number) => `${name}=${encodeURIComponent(value)}; Path=${prefix}; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure}`;
  const redirect = (response: ServerResponse, location: string, cookies: string[] = []) => {
    response.writeHead(302, { location, "cache-control": "no-store", "content-length": "0", ...(cookies.length ? { "set-cookie": cookies } : {}) });
    response.end();
  };
  const verification = (request: IncomingMessage) => {
    const value = signer.unsign(readCookie(request, verifiedCookie));
    return value && typeof value.userId === "string" && /^\d{1,25}$/.test(value.userId) && typeof value.nonce === "string" && modes.has(value.purpose) ? value : undefined;
  };

  async function start(request: IncomingMessage, response: ServerResponse, url: URL) {
    if (!discord.configured) return sendJson(response, 503, { error: "discord_oauth_not_configured" });
    const purpose = url.searchParams.get("purpose") ?? "";
    const uid = url.searchParams.get("uid") ?? "";
    if (!modes.has(purpose) || !(await interactionFor(provider, request, response, uid))) return sendJson(response, 400, { error: "invalid_oauth_request" });
    const state = randomBytes(32).toString("hex");
    redirect(response, discord.authorizeUrl(state), [cookie(stateCookie, signer.sign({ state, purpose, uid }, 300), 300)]);
  }

  async function callback(request: IncomingMessage, response: ServerResponse, url: URL) {
    const pending = signer.unsign(readCookie(request, stateCookie));
    const clear = cookie(stateCookie, "", 0);
    if (!pending || pending.state !== url.searchParams.get("state") || !modes.has(pending.purpose) || !/^[\w-]+$/.test(String(pending.uid))) {
      return redirect(response, `${deps.appUrls.home}?authError=oauth_state_invalid`, [clear]);
    }
    const destination = `/oidc/interaction/${pending.uid}`;
    const code = url.searchParams.get("code");
    if (!code || url.searchParams.has("error")) return redirect(response, `${destination}?authError=discord_authorization_failed`, [clear]);
    try {
      const person = await discord.identify(code);
      const proof = signer.sign({ userId: person.id, nickname: person.nickname, purpose: pending.purpose, nonce: randomBytes(16).toString("hex") }, 300);
      redirect(response, `${destination}?discordAuth=verified&purpose=${pending.purpose}`, [cookie(verifiedCookie, proof, 300), clear]);
    } catch (error) {
      console.warn(`Discord verification failed: ${error instanceof Error ? error.message : "unknown"}`);
      redirect(response, `${destination}?authError=discord_verification_failed`, [clear]);
    }
  }

  // Register or reset a password for the Discord-verified person, then sign them in to the interaction.
  async function changeAccount(request: IncomingMessage, response: ServerResponse, purpose: "register" | "reset-password") {
    let body: Record<string, unknown>;
    try { body = await readJson(request); } catch { return sendJson(response, 400, { error: "invalid_request" }); }
    const { uid, ...fields } = body;
    if (!(await interactionFor(provider, request, response, String(uid ?? "")))) return sendJson(response, 400, { error: "login_expired" });
    const proof = verification(request);
    const now = Math.floor(Date.now() / 1000);
    for (const [nonce, expires] of used) if (expires <= now) used.delete(nonce);
    if (!proof || proof.purpose !== purpose || used.has(proof.nonce)) return sendJson(response, 403, { error: "discord_verification_required" });
    const result = await deps.internalRequest("POST", purpose === "register" ? "/internal/auth/register" : "/internal/auth/password/reset", proof.userId, { ...fields, nickname: proof.nickname });
    const user = result.body?.user;
    if (result.status < 200 || result.status >= 300 || !user?.userId) return sendJson(response, result.status, result.body);
    used.set(proof.nonce, proof.exp);
    // The new (or recovered) account is signed in right away, as it always was; the session lasts as long as the browser stays open.
    const redirectTo = await provider.interactionResult(request, response, { login: { accountId: String(user.userId), remember: false } }, { mergeWithLastSubmission: false });
    response.setHeader("set-cookie", cookie(verifiedCookie, "", 0));
    sendJson(response, 200, { redirectTo });
  }

  return async (request, response, url) => {
    if (!url.pathname.startsWith(prefix)) return false;
    const route = url.pathname.slice(prefix.length);
    if (request.method === "GET") {
      if (route === "config") {
        const details = await interactionFor(provider, request, response, url.searchParams.get("uid") ?? "");
        if (!details) { sendJson(response, 400, { error: "login_expired" }); return true; }
        const mode = String(details.params.ui_mode ?? "");
        sendJson(response, 200, { discordOAuthConfigured: discord.configured, clientId: String(details.params.client_id ?? ""), startMode: modes.has(mode) ? mode : "login" });
        return true;
      }
      if (route === "countries") { const result = await deps.internalRequest("GET", "/internal/auth/countries", undefined); sendJson(response, result.status, result.body); return true; }
      if (route === "discord/start") { await start(request, response, url); return true; }
      if (route === "discord/callback") { await callback(request, response, url); return true; }
      if (route === "discord/verification") {
        const proof = verification(request);
        if (proof) sendJson(response, 200, { userId: proof.userId, nickname: proof.nickname, purpose: proof.purpose }); else sendJson(response, 401, { error: "discord_verification_required" });
        return true;
      }
    }
    if (request.method === "POST" && route === "register") { await changeAccount(request, response, "register"); return true; }
    if (request.method === "POST" && route === "password-reset") { await changeAccount(request, response, "reset-password"); return true; }
    sendJson(response, 404, { error: "not_found" });
    return true;
  };
}
