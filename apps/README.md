# Local Web Foundation

Two Vite frontends and a TypeScript API are grouped in the npm workspace. The
API starts the private Python data service automatically:

- Dashboard: `http://127.0.0.1:4310`
- HTS: `http://127.0.0.1:4311`
- API: `http://127.0.0.1:4312/api/health`
- Private Python data service: `127.0.0.1:4313` (API-managed; not exposed to the browser)

Copy `.env.example` to `.env` and map long random bearer tokens to registered
Discord user IDs in `INAZINA_WEB_TOKENS_JSON`. Generate token values with
`openssl rand -hex 32`. The user's active status and role are always read from
the existing database; never put a role in the token configuration.

Run `npm install` once, then start the API and either frontend in separate
terminals with `npm run dev:api`, `npm run dev:dashboard`, and `npm run dev:hts`.
The browser-facing API stays bound to `127.0.0.1`.

The Vite apps proxy `/api` requests to Node. Node authenticates bearer tokens
and forwards authorized read requests to the Python service, which opens the
existing SQLite database in read-only mode. Available endpoints are:

- `GET /api/health` (public service health)
- `GET /api/auth/session` and `GET /api/me` (authenticated profile)
- `GET /api/market/instruments` (authenticated active instruments)
- `GET /api/admin/overview` (admin-only overview)

No write or trading endpoint is implemented. The HTML mockups remain separate
reference material.
