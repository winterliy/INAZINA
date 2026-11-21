// The identity provider's login step: the page people type their credentials into, the credential check against the
// data service, and the automatic consent for INAZINA's own apps.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { IncomingMessage, ServerResponse } from "node:http";
import type Provider from "oidc-provider";
import { ICON } from "../landing.js";

export type InternalRequest = (method: "GET" | "POST", path: string, userId: string | undefined, body?: unknown) => Promise<{ status: number; body: any }>;
export type AppUrls = { home: string; register: string; reset: string };
export type InteractionDeps = { provider: Provider; internalRequest: InternalRequest; clientIp: (request: IncomingMessage) => string; appUrls: AppUrls };

const maxFailures = 8; // per address and id, per 15 minutes: the same limit as the apps' own login
const failureWindowMs = 15 * 60_000;
// The login history keeps the app names it always had.
const appLabels: Record<string, string> = { console: "dashboard", naru: "hts" };
const clientNames: Record<string, string> = { console: "콘솔", naru: "나루" };
const messages: Record<number, string> = {
  401: "아이디 또는 비밀번호가 올바르지 않습니다.", 404: "존재하지 않는 계정입니다.",
  429: "로그인 시도가 너무 많습니다. 잠시 뒤에 다시 시도해 주세요.", 403: "사용할 수 없는 계정입니다.",
};

const sharedDir = join(dirname(fileURLToPath(import.meta.url)), "../../../shared");
const escape = (value: string) => value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);

// The shared gate layout (apps/shared/gate.html) with its script-driven form swapped for a plain form post.
let template: string | undefined;
function layout(): string {
  if (!template) {
    const html = readFileSync(join(sharedDir, "gate.html"), "utf8").replace("{{SCENE}}", readFileSync(join(sharedDir, "scene.svg"), "utf8"))
      .replace(/<form id="gate"[\s\S]*?<\/form>\s*<p class="err"[^>]*><\/p>\s*<div id="out" hidden><\/div>/, "{{FORM}}")
      .replace(/<script>[\s\S]*<\/script>/, "");
    if (!html.includes("{{FORM}}")) throw new Error("apps/shared/gate.html changed: the identity provider login page cannot be built");
    template = html;
  }
  return template;
}

function loginPage(uid: string, clientId: string, appUrls: AppUrls, loginId = "", error = ""): string {
  const form = `<form method="post" action="/oidc/interaction/${escape(uid)}/login" autocomplete="off">
<label><span>아이디</span><input name="loginId" autocomplete="username" placeholder="아이디" value="${escape(loginId)}" required></label>
<label><span>비밀번호</span><input name="password" type="password" autocomplete="current-password" placeholder="비밀번호" required></label>
<button class="go" type="submit">로그인</button>
</form>
<p class="err" role="alert">${escape(error)}</p>`;
  const values: Record<string, string> = {
    TITLE: "로그인", ICON, HEADING: "INAZINA 로그인", LEAD: "하나의 계정으로 나루와 콘솔을 이용하세요.",
    PATH_CHIP: `<span class="chip">${escape(clientNames[clientId] ?? clientId)}</span>`, FORM: form,
    LINKS: `<a class="back" href="${escape(appUrls.register)}">회원가입</a><a class="back" href="${escape(appUrls.reset)}">비밀번호 찾기</a>`,
    STATUS_CLASS: "ok", STATUS_TEXT: "통합 로그인",
  };
  return layout().replace(/\{\{(\w+)\}\}/g, (_match, key: string) => values[key] ?? "");
}

export function sendHtml(response: ServerResponse, status: number, body: string): void {
  response.writeHead(status, {
    "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer",
    "x-frame-options": "DENY", "content-security-policy": "frame-ancestors 'none'",
  });
  response.end(body);
}

async function readForm(request: IncomingMessage): Promise<URLSearchParams> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 2048) throw new Error("request_too_large");
    chunks.push(Buffer.from(chunk));
  }
  return new URLSearchParams(Buffer.concat(chunks).toString("utf8"));
}

export function createInteractionHandler(deps: InteractionDeps): (request: IncomingMessage, response: ServerResponse, url: URL) => Promise<boolean> {
  const { provider, appUrls } = deps;
  const failures = new Map<string, { count: number; resetAt: number }>();
  const expired = (response: ServerResponse) => sendHtml(response, 400, "<!doctype html><meta charset=\"utf-8\"><p>로그인 요청이 만료되었습니다. 앱에서 다시 시작해 주세요.</p>");

  return async (request, response, url) => {
    const match = /^\/oidc\/interaction\/([\w-]+)(\/login)?$/.exec(url.pathname);
    if (!match) return false;
    let details;
    try { details = await provider.interactionDetails(request, response); } catch { expired(response); return true; }
    if (details.uid !== match[1]) { expired(response); return true; }
    const clientId = String(details.params.client_id ?? "");

    if (request.method === "GET" && !match[2]) {
      if (details.prompt.name === "login") { sendHtml(response, 200, loginPage(details.uid, clientId, appUrls)); return true; }
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
      let form: URLSearchParams;
      try { form = await readForm(request); } catch { sendHtml(response, 413, loginPage(details.uid, clientId, appUrls, "", messages[401])); return true; }
      const loginId = (form.get("loginId") ?? "").trim().slice(0, 80);
      const password = form.get("password") ?? "";
      const key = `${deps.clientIp(request)}:idp:${loginId}`;
      const attempt = failures.get(key);
      const show = (status: number) => sendHtml(response, status, loginPage(details.uid, clientId, appUrls, loginId, messages[status] ?? "로그인할 수 없습니다. 잠시 뒤에 다시 시도해 주세요."));
      if (attempt && attempt.resetAt > Date.now() && attempt.count >= maxFailures) { show(429); return true; }
      const meta = { clientIp: deps.clientIp(request), userAgent: String(request.headers["user-agent"] ?? "").slice(0, 300), app: appLabels[clientId] ?? clientId };
      const result = await deps.internalRequest("POST", "/internal/auth/login", undefined, { loginId, password, meta });
      if (result.status === 200 && result.body.user?.userId) {
        failures.delete(key);
        await provider.interactionFinished(request, response, { login: { accountId: String(result.body.user.userId) } }, { mergeWithLastSubmission: false });
        return true;
      }
      if (result.status === 401 || result.status === 404) {
        const current = attempt && attempt.resetAt > Date.now() ? attempt : { count: 0, resetAt: Date.now() + failureWindowMs };
        current.count += 1;
        failures.set(key, current);
      }
      show(result.status >= 400 && result.status < 500 ? result.status : 503);
      return true;
    }
    expired(response);
    return true;
  };
}
