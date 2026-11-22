// The identity provider's login step: the credential check against the data service (the login screen itself is apps/id,
// built and served by ui.ts) and the automatic consent for INAZINA's own apps.
import type { IncomingMessage, ServerResponse } from "node:http";
import type Provider from "oidc-provider";

export type InternalRequest = (method: "GET" | "POST", path: string, userId: string | undefined, body?: unknown) => Promise<{ status: number; body: any }>;
export type AppUrls = { home: string }; // the app the login server sends people back to (its address ends with /)
export type InteractionDeps = {
  provider: Provider; internalRequest: InternalRequest; clientIp: (request: IncomingMessage) => string; appUrls: AppUrls;
  shell?: () => string | undefined; // the built login screen's HTML, when there is one
};

// Where an app starts a login (optionally on the register or reset screen).
export const appLoginUrl = (home: string, mode?: string): string => `${home}api/auth/oidc/login${mode ? `?mode=${mode}` : ""}`;

const maxFailures = 8; // per address and id, per 15 minutes: the same limit as the apps' own login
const failureWindowMs = 15 * 60_000;
// The login history keeps the app names it always had.
const appLabels: Record<string, string> = { console: "dashboard", naru: "hts" };
export function sendHtml(response: ServerResponse, status: number, body: string): void {
  response.writeHead(status, {
    "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer",
    "x-frame-options": "DENY", "content-security-policy": "frame-ancestors 'none'",
  });
  response.end(body);
}

export function sendJson(response: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "content-length": Buffer.byteLength(text) });
  response.end(text);
}

export async function readBody(request: IncomingMessage, limit = 4096): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new Error("request_too_large");
    chunks.push(Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString("utf8");
}

export async function readJson(request: IncomingMessage): Promise<Record<string, unknown>> {
  const value = JSON.parse(await readBody(request));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_json");
  return value as Record<string, unknown>;
}

export const wantsJson = (request: IncomingMessage): boolean => (request.headers["content-type"] ?? "").includes("application/json");

export type InteractionDetails = Awaited<ReturnType<Provider["interactionDetails"]>>;

// The caller's own login interaction, or nothing: the request must carry its cookie and match `uid`.
export async function interactionFor(provider: Provider, request: IncomingMessage, response: ServerResponse, uid: string): Promise<InteractionDetails | undefined> {
  try {
    const details = await provider.interactionDetails(request, response);
    return details.uid === uid ? details : undefined;
  } catch { return undefined; }
}

export function createInteractionHandler(deps: InteractionDeps): (request: IncomingMessage, response: ServerResponse, url: URL) => Promise<boolean> {
  const { provider } = deps;
  const failures = new Map<string, { count: number; resetAt: number }>();
  const expired = (response: ServerResponse) => sendHtml(response, 400, "<!doctype html><meta charset=\"utf-8\"><p>로그인 요청이 만료되었습니다. 앱에서 다시 시작해 주세요.</p>");

  // One credential attempt, shared by the form and the JSON login: `status` 200 means signed in (with `accountId`).
  async function attempt(request: IncomingMessage, clientId: string, loginId: string, password: string): Promise<{ status: number; error?: string; accountId?: string }> {
    const key = `${deps.clientIp(request)}:idp:${loginId}`;
    const previous = failures.get(key);
    if (previous && previous.resetAt > Date.now() && previous.count >= maxFailures) return { status: 429, error: "login_rate_limited" };
    const meta = { clientIp: deps.clientIp(request), userAgent: String(request.headers["user-agent"] ?? "").slice(0, 300), app: appLabels[clientId] ?? clientId };
    const result = await deps.internalRequest("POST", "/internal/auth/login", undefined, { loginId, password, meta });
    if (result.status === 200 && result.body.user?.userId) { failures.delete(key); return { status: 200, accountId: String(result.body.user.userId) }; }
    if (result.status === 401 || result.status === 404) {
      const current = previous && previous.resetAt > Date.now() ? previous : { count: 0, resetAt: Date.now() + failureWindowMs };
      current.count += 1;
      failures.set(key, current);
    }
    const status = result.status >= 400 && result.status < 500 ? result.status : 503;
    return { status, error: String(result.body?.error ?? (status === 503 ? "unavailable" : "login_failed")) };
  }

  return async (request, response, url) => {
    const match = /^\/oidc\/interaction\/([\w-]+)(\/login)?$/.exec(url.pathname);
    if (!match) return false;
    const json = wantsJson(request);
    const details = await interactionFor(provider, request, response, match[1]);
    if (!details) { if (json) sendJson(response, 400, { error: "login_expired" }); else expired(response); return true; }
    const clientId = String(details.params.client_id ?? "");

    if (request.method === "GET" && !match[2]) {
      if (details.prompt.name === "login") {
        const shell = deps.shell?.();
        if (shell) sendHtml(response, 200, shell);
        else sendHtml(response, 503, "<!doctype html><meta charset=\"utf-8\"><title>로그인 화면 없음</title><p>로그인 화면(apps/id)이 빌드되지 않았습니다. <code>npm run build --workspace @inazina/id</code> 를 실행해 주세요.</p>");
        return true;
      }
      if (details.prompt.name === "consent") {
        // INAZINA's own apps need no consent screen: grant what they asked for.
        const grant = details.grantId ? await provider.Grant.find(details.grantId) : new provider.Grant({ accountId: details.session!.accountId, clientId });
        if (!grant) { expired(response); return true; }
        const missing = details.prompt.details as { missingOIDCScope?: string[]; missingOIDCClaims?: string[] };
        if (missing.missingOIDCScope) grant.addOIDCScope(missing.missingOIDCScope.join(" "));
        if (missing.missingOIDCClaims) grant.addOIDCClaims(missing.missingOIDCClaims);
        await provider.interactionFinished(request, response, { consent: { grantId: await grant.save() } }, { mergeWithLastSubmission: true });
        return true;
      }
      expired(response);
      return true;
    }

    if (request.method === "POST" && match[2] && details.prompt.name === "login") {
      if (!json) { sendJson(response, 415, { error: "json_required" }); return true; }
      let loginId = "";
      let password = "";
      try { const body = await readJson(request); loginId = String(body.loginId ?? ""); password = String(body.password ?? ""); }
      catch { sendJson(response, 400, { error: "invalid_request" }); return true; }
      loginId = loginId.trim().slice(0, 80);
      const result = await attempt(request, clientId, loginId, password);
      if (result.status !== 200) { sendJson(response, result.status, { error: result.error }); return true; }
      // remember: false -> the single sign-on session lives only as long as the browser stays open.
      const outcome = { login: { accountId: result.accountId!, remember: false } };
      sendJson(response, 200, { redirectTo: await provider.interactionResult(request, response, outcome, { mergeWithLastSubmission: false }) });
      return true;
    }
    expired(response);
    return true;
  };
}
