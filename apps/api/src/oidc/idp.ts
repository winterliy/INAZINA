// Everything the identity provider's host (id.inazina.kr) serves. Requests for any other host are not touched, so the
// API on the same process keeps answering exactly as before and the IdP host never exposes an API route.
import { randomBytes } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import type Provider from "oidc-provider";
import { createAccountHandler } from "./account.js";
import type { DiscordClient } from "./discord.js";
import { appLoginUrl, createInteractionHandler, type AppUrls, type InternalRequest } from "./interaction.js";
import { mount } from "./provider.js";
import { createSigner } from "./signed.js";
import { createUiFiles, uiPrefix } from "./ui.js";

export type { InternalRequest } from "./interaction.js";

export type IdpDeps = {
  provider: Provider;
  issuerHost: string; // the Host header of the identity provider, e.g. id.inazina.kr
  internalRequest: InternalRequest;
  clientIp: (request: IncomingMessage) => string;
  appUrls: AppUrls; // the app people are sent back to; its address ends with /
  discord?: DiscordClient; // without it (or unconfigured) the Discord-verified screens report that Discord is not set up
  uiDir?: string; // the built login screen (apps/id/dist); without it a server-rendered login page is used
  secret?: string; // signs the short Discord cookies
  secureCookies?: boolean;
};

const noDiscord: DiscordClient = { configured: false, authorizeUrl: () => "", identify: async () => { throw new Error("discord_oauth_not_configured"); } };

export function createIdp(deps: IdpDeps): (request: IncomingMessage, response: ServerResponse) => Promise<boolean> {
  const ui = createUiFiles(deps.uiDir);
  const interaction = createInteractionHandler({ ...deps, shell: ui.shell });
  const account = createAccountHandler({ provider: deps.provider, internalRequest: deps.internalRequest, discord: deps.discord ?? noDiscord, signer: createSigner(deps.secret ?? randomBytes(32).toString("hex")),
    secureCookies: deps.secureCookies ?? false, appUrls: deps.appUrls });
  const oidc = mount(deps.provider);
  const redirects: Record<string, string> = { "/register": appLoginUrl(deps.appUrls.home, "register"), "/reset": appLoginUrl(deps.appUrls.home, "reset-password"), "/login": appLoginUrl(deps.appUrls.home) };
  return async (request, response) => {
    if ((request.headers.host ?? "").toLowerCase() !== deps.issuerHost.toLowerCase()) return false;
    const url = new URL(request.url ?? "/", "http://idp.invalid");
    if (url.pathname.startsWith(uiPrefix) && ui.serve(response, url.pathname)) return true;
    if (url.pathname === "/oidc" || url.pathname.startsWith("/oidc/")) {
      if (!(await account(request, response, url)) && !(await interaction(request, response, url))) oidc(request, response);
      return true;
    }
    const target = redirects[url.pathname];
    if (target) response.writeHead(302, { location: target, "cache-control": "no-store", "content-length": "0" });
    else response.writeHead(404, { "content-length": "0", "cache-control": "no-store" });
    response.end();
    return true;
  };
}
