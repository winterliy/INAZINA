# Local Web Foundation

Two Vite frontends and a TypeScript API are grouped in the npm workspace. The
API starts the private Python data service automatically:

- Dashboard: `http://127.0.0.1:4310`
- HTS: `http://127.0.0.1:4311`
- API: `http://127.0.0.1:4312/api/health`
- Private Python data service: `127.0.0.1:4313` (API-managed; not exposed to the browser)

Copy the project-root `.env.example` to `.env`. Create a Discord application
and register `http://127.0.0.1:4312/api/auth/discord/callback` as an OAuth2
redirect URI. Set its client ID and secret in `.env`; the app requests only the
`identify` scope. Set `INAZINA_AUTH_SECRET` to a persistent random value from
`openssl rand -hex 32` so sessions remain valid across API restarts.

Both frontends share one account. Registration and password reset require
Discord OAuth verification; login uses Discord ID plus password. Passwords are
stored as salted scrypt hashes in the existing SQLite database, never in plain
text. New verified users receive the same initial balance as the existing bot
registration flow. Country IDs come from active `country_currencies` entries.
Unknown IDs are returned as `account_not_found` and the UI offers registration.
The user's active status and role are always read from the existing database.

Run `npm install` once, then start the API and either frontend in separate
terminals with `npm run dev:api`, `npm run dev:dashboard`, and `npm run dev:hts`.
The browser-facing API stays bound to `127.0.0.1`. OAuth login requires network
access to Discord; account registration and password reset remain unavailable
until OAuth credentials are configured.

The Vite apps proxy `/api` requests to Node. Node authenticates bearer tokens
and forwards authorized read requests to the Python service, which opens the
existing SQLite database in read-only mode. Available endpoints are:

- `GET /api/health` (public service health)
- `GET /api/auth/config`, `/api/auth/countries`, `/api/auth/session`, and `/api/me`
- `POST /api/auth/login`, `/api/auth/register`, `/api/auth/password/reset`, and `/api/auth/logout`
- `GET /api/auth/discord/start` and `/api/auth/discord/callback` (registration, ID lookup, and password reset verification)
- `GET /api/market/instruments`, `/api/me/portfolio`, `/api/market/orders`, and `/api/market/book/:code`
- `GET /api/market/chart/:code?range=1d` and `GET/POST /api/market/drawings/:code`
- `POST /api/market/orders` and `/api/market/orders/:id/cancel` (existing Python order validation)
- `GET /api/admin/overview`, `/api/admin/instruments`, `/api/admin/users`, and `/api/admin/events`
- `POST /api/admin/market`, `/api/admin/instruments/:code` (name, price, supply), `/api/admin/instruments/:code/status`, `/api/admin/users/:id` (cash, role, status), and `/api/admin/events`
- `GET /api/admin/audit`

Trading requests use the existing Python order book; administrator changes use
the existing market, user, and administrator managers. Bearer tokens are not
created automatically: the API intentionally refuses authenticated access until
the owner configures `.env`. Keep all services on loopback; router port forwarding
is not part of this local development setup. The HTML mockups remain separate
reference material.
