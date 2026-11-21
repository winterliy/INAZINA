// Everything the identity provider's host (id.inazina.kr) serves. Requests for any other host are not touched, so the
// API on the same process keeps answering exactly as before and the IdP host never exposes an API route.
import type { IncomingMessage, ServerResponse } from "node:http";
import type Provider from "oidc-provider";
import { createInteractionHandler, type AppUrls, type InternalRequest } from "./interaction.js";
import { mount } from "./provider.js";

export type { InternalRequest } from "./interaction.js";

export type IdpDeps = {
  provider: Provider;
  issuerHost: string; // the Host header of the identity provider, e.g. id.inazina.kr
  internalRequest: InternalRequest;
  clientIp: (request: IncomingMessage) => string;
  appUrls: AppUrls; // registration, password reset and the home page live in the apps (Discord-verified views)
};

export function createIdp(deps: IdpDeps): (request: IncomingMessage, response: ServerResponse) => Promise<boolean> {
  const interaction = createInteractionHandler(deps);
  const oidc = mount(deps.provider);
  const redirects: Record<string, string> = { "/register": deps.appUrls.register, "/reset": deps.appUrls.reset, "/login": deps.appUrls.home };
  return async (request, response) => {
    if ((request.headers.host ?? "").toLowerCase() !== deps.issuerHost.toLowerCase()) return false;
    const url = new URL(request.url ?? "/", "http://idp.invalid");
    if (url.pathname === "/oidc" || url.pathname.startsWith("/oidc/")) {
      if (!(await interaction(request, response, url))) oidc(request, response);
      return true;
    }
    const target = redirects[url.pathname];
    if (target) response.writeHead(302, { location: target, "cache-control": "no-store", "content-length": "0" });
    else response.writeHead(404, { "content-length": "0", "cache-control": "no-store" });
    response.end();
    return true;
  };
}
