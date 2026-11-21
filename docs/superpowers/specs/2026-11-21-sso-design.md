# Unified SSO (OIDC IdP) and Caddy front door — design

Date: 2026-11-21 · Status: draft, awaiting approval

## Intent
One central authentication server (`id.inazina.kr`) acts as an OpenID Connect
identity provider. Every INAZINA web app signs users in by redirecting to it
(authorization code + PKCE) instead of each app running its own login. A Caddy
file under `infra/` is the single public entry point for all hostnames.

## Goals
- One sign-in works across naru, console, work, and any future app.
- Standard OIDC (code + PKCE, S256) so the new work platform is just another client.
- Keep the existing account model: Discord ID / HTS ID / country ID + password,
  scrypt hashes, `authVersion` (a password change ends sessions), delegated
  `$login` sessions (`by`), Discord-OAuth-verified registration and reset.
- Keep the emergency passcode and the 4312/4313 direct-URL access gate unchanged.
- `infra/Caddyfile` documents and enforces the host map.

## Non-goals / anti-goals
- `wl.kr` (4315) keeps its own entry password; it is not an OIDC client.
- `api.inazina.kr` is never published. A config that exposes 4312 or 4313 to the
  internet is a failure even if SSO works.
- No third-party IdP (Keycloak, Auth0). No SAML. No social login besides the
  existing Discord verification.
- Minecraft (`mc`) is not web SSO; account linking is a separate task
  (`docs/minecraft_integration_plan.md`).

## Host map (infra/Caddyfile)
| Host | Target | Note |
|---|---|---|
| `id.inazina.kr` | 4312, only `/oidc/*`, `/login`, `/register`, `/reset` | IdP; nothing else of the API |
| `naru.inazina.kr` | 4311 | HTS; `/api/*` proxied to 4312 on the same host |
| `console.inazina.kr` | 4310 | admin dashboard; `/api/*` same way |
| `work.inazina.kr` | new platform (port TBD) | OIDC client, placeholder block |
| `www.`, apex, `corp.inazina.kr` | 4314 | corporate site (apex redirects to www) |
| `wl.kr` | 4315 | own login |
| `mc.inazina.kr` | 25565 TCP | needs `caddy-l4` or plain DNS A/SRV; commented block |
| 4313 | none | loopback only, internal token, never in Caddy |
| `api.inazina.kr` | none | deliberately absent; comment says so |

Browsers reach the API only through each app's own host (`/api/*` same-origin),
so no CORS and no public API hostname. Caddy must not forward `/api/internal*`
or the browser-access gate paths. Local dev uses `tls internal` and
`*.inazina.localhost`, selected by an environment switch.

## Flow
1. User opens `naru.inazina.kr`. No app session → app redirects to
   `id.inazina.kr/oidc/auth` with `code_challenge` (S256), `state`, `nonce`.
2. IdP shows login (existing credentials) unless its own session cookie
   (host-only on `id.inazina.kr`) exists; then it returns a code immediately.
3. App backend (Node API, `/api/auth/callback` on that host) exchanges the code
   at `/oidc/token`, verifies the ID token (signature via JWKS, `iss`, `aud`,
   `nonce`), then issues its own host-only `inazina_session` cookie.
4. Per request the API still checks role and `authVersion` against the database.
5. Logout: app clears its cookie and calls IdP RP-initiated logout.
   Revocation elsewhere relies on the 12-hour session and `authVersion`.

Claims: `sub` (user id), `name`, `role`, `auth_version`, `act` (admin behind a
delegated session). Scopes: `openid profile`.
Console requires an admin role and asks the IdP for a fresh login
(`max_age`) so a shared-browser session is not enough.

## Clients (registered in a config file, not the database)
`naru` (HTS), `console` (dashboard), `work` (later). Exact redirect URIs per
environment; no wildcards.

## Approaches considered
- A: share a `.inazina.kr` cookie only — rejected: breaks for external apps.
- B: OIDC IdP — **chosen by the owner**.
- C: A first, B later — rejected: double work.
- IdP placement (my call): a module inside the existing Node API (4312),
  reached through Caddy on `id.inazina.kr`, because accounts, sessions and
  rate limiting already live there. A separate `apps/id` service is the fallback
  if the module grows unwieldy.

## Left to the builder (my calls, to check)
- Library: `node-oidc-provider` (pinned exactly) rather than hand-rolling;
  storage adapter on the existing database layer (PostgreSQL per cutover plan,
  see `docs/postgres-cutover-runbook.md`), signing keys from env/file, rotated.
- Login/registration/reset pages for `id` reuse the dashboard's current look
  (rebrand); no redesign (design approval rule applies).
- Rate limits and login-history recording move with the login endpoint.
- Rollout in steps: Caddyfile and host map → IdP + naru → console → work.
  Old direct ports (4310/4311) keep working locally until cutover.
