# Local Web Foundation

Two Vite + React frontends and a TypeScript API are grouped in the npm
workspace. The API starts the private Python data service automatically:

- Dashboard (administrators): `http://127.0.0.1:4310`
- HTS (trading): `http://127.0.0.1:4311`
- API: `http://127.0.0.1:4312/api/health`
- Private Python data service: `127.0.0.1:4313` (API-managed; not exposed to the browser)

```
Browser ─▶ Dashboard :4310 ─┐
        ─▶ HTS       :4311 ─┴─ /api proxy ─▶ Node API :4312 ─▶ Python service :4313 ─▶ database/inazina.db
                                             (sessions, roles)  (web_api/server.py)
```

## Setup

Requires Node.js 24 or newer and the project's Python virtual environment.

1. Copy the project-root `.env.example` to `.env`.
2. Create a Discord application and register
   `http://127.0.0.1:4312/api/auth/discord/callback` as an OAuth2 redirect URI.
   Set its client ID and secret in `.env`; the app requests only the
   `identify` scope.
3. Set `INAZINA_AUTH_SECRET` to a persistent random value from
   `openssl rand -hex 32` so sessions remain valid across API restarts.
4. Run `npm install` once.

Start everything, including the Discord bot, with `./start_all.command`
(Ctrl+C stops all services). To run pieces separately use
`npm run dev:api`, `npm run dev:dashboard`, and `npm run dev:hts` in separate
terminals. OAuth login requires network access to Discord; registration and
password reset stay unavailable until OAuth credentials are configured.

Dependency versions are pinned exactly in each `apps/*/package.json` and in
`package-lock.json`. Upgrade deliberately (`npm install <pkg>@<version>
--workspace <app>`), then run `npm run typecheck` and `npm run build`.

## Accounts and security

Both frontends share one account. Registration and password reset require
Discord OAuth verification; login accepts a Discord ID, an HTS ID, or a country
ID plus password. Passwords are stored as salted scrypt hashes in the existing
SQLite database, never in plain text. New verified users receive the same
initial balance as the bot registration flow. Country IDs come from active
`country_currencies` entries; country accounts trade with their national
treasury. The user's active status and role are always read from the database.

The Vite apps proxy `/api` requests to Node. Node authenticates signed HttpOnly
session cookies, checks the administrator role, and forwards authorized
requests to the Python service with a per-process internal token. The Python
service rechecks the role before every administrator action. On macOS the API
start scripts add `/etc/ssl/cert.pem` to Node's trusted CA list so HTTPS
requests to Discord validate normally; TLS verification remains on. Keep all
services on loopback; router port forwarding is not part of this setup.

## HTS

The HTS shares the dashboard's Apple-style glass design in a denser trading
layout and follows the system light/dark setting. Following Korean market
convention, buying and rising prices are red; selling and falling prices are blue.

- Candle (default) and line charts. Candles are aggregated in SQLite per
  period (1m, 5m, 10m, 30m, 1h, 6h, 12h, 1d); "자동" picks a period from the
  selected range. Rising candles are red and falling candles blue.
- Trend and horizontal lines are saved per user and instrument as chart
  time/price anchors, so they survive reloads, zoom, and period changes.
- The wallet shows cash and totals in the user's home currency (as the Discord
  `/내지갑` does), foreign-currency wallets, and holdings; selecting a holding
  opens its chart.
- Limit orders and cancellations use the existing Python order book.

## Dashboard

The dashboard uses the glass design prototyped in `glass-dashboard/index.html`
and follows the system light/dark setting.

- **운영 현황**: counts, per-exchange market state, and open/close/pause/resume.
- **종목 · 상품** and **금융 상품**: create, edit, stop, and restore every
  `$상품생성` kind — stocks, preferred shares, assets, ETFs, futures, corporate
  and treasury bonds, funds, index funds, commodities, options, and ELS; and
  savings, loans, currencies, and foreign-currency accounts. Forms follow the
  `$상품생성` argument order (same parser for dividends, periods, and fund
  components) and expose every other table column under 고급 설정. Each row
  links to its related commands (price, capital, dividends, split, merger, …).
- **거래소**: create and delete exchanges, change the owning country, apply
  open/close/pause/resume, circuit breaker stages 1–3, and sidecar per exchange
  or for all, and view index components.
- **사용자 · 국가**: every user field (nickname, nationality, cash, credit score,
  income, income stability, status, role), web login (set password, HTS ID,
  expire sessions, remove login), and read-only accounts, loans, and holdings.
  Countries: create/delete, base rate, legal maximum rate, tax, treasury cash,
  and the country account's HTS login.
- **시장 사건**: create good/bad events by percentage or, for one asset, by
  target price (good news at or above the current price, bad news at or below;
  the percentage then sets volatility), and list recent `market_events`.
- **관리자 명령어**: runs every Discord `$` command. The 관리자 명령어 tab covers
  the 40 `AdminCommandsManager` commands; the 국가 명의 tab covers the 16
  treasury commands (`$매수`, `$송금`, `$계좌개설`, …) with the same form fields.
  Both keep the Discord validation and audit trail. `$사건` is the 시장 사건 page
  and `$도움말` is the 도움말 tab. `$주총` must send Discord DMs, so the dashboard
  queues it in `web_bot_jobs` and the running bot processes it within seconds
  (`functions/web_jobs.py`); the tab shows each job's status and result.
- **DB 편집**: replaces the DB Browser workflow described in `docs/` (for
  example `settings`, `country_currencies`, `users` nationality and credit,
  `financial_institutions`, `currency_liquidity_state`, `market_rate_curve`).
  Browse, search, edit, insert, and delete rows. Writes run with foreign keys
  on, so database CHECK constraints and triggers still validate them, and each
  change is written to `admin_audit` with before/after values.
  `web_auth_credentials`, `admin_audit`, and `web_chart_drawings` are hidden.
  Type `NULL` to store NULL. Password columns are shown as `<hidden>` and
  never written to the audit trail.
- **작업 기록**: the latest `admin_audit` entries.

## API endpoints

Public:

- `GET /api/health`
- `GET /api/auth/config`, `/api/auth/countries`
- `GET /api/auth/discord/start`, `/api/auth/discord/callback`, `/api/auth/discord/verification`
- `POST /api/auth/login`, `/api/auth/register`, `/api/auth/password/reset`, `/api/auth/logout`

Signed-in users:

- `GET /api/auth/session`, `/api/me`, `/api/me/portfolio`
- `GET/POST /api/hts/account` (HTS login ID)
- `GET /api/market/instruments`, `/api/market/orders`, `/api/market/book/:code`
- `GET /api/market/chart/:code?range=1h|1d|1w|1m|all&interval=<seconds>` (points and candles)
- `GET/POST /api/market/drawings/:code`
- `POST /api/market/orders`, `/api/market/orders/:id/cancel`

Administrators:

- `GET /api/admin/overview`, `/api/admin/instruments`, `/api/admin/users?search=`, `/api/admin/events`, `/api/admin/audit`
- `POST /api/admin/market` (`action`, optional `exchangeId`)
- `POST /api/admin/instruments/:code` (name, price, supply, fund settings), `/api/admin/instruments/:code/status`
- `POST /api/admin/funds`, `/api/admin/users/:id` (cash, role, status), `/api/admin/users/country-account`
- `POST /api/admin/events` (`eventType`, `severity`, `targetPercent`, optional `targetPrice`, `durationSeconds`, `assetCode`)
- `GET/POST /api/admin/commands` (catalog; run `{ command, arguments }` or `{ national: true, command, values }`)
- `GET /api/admin/products/catalog`, `/api/admin/products?group=market|finance`, `/api/admin/products/:code`
- `POST /api/admin/products` (`operation` create/edit, `kind`, `code`, `fields`, `advanced`), `/api/admin/products/:code/status` (`active`)
- `GET /api/admin/user/:id`; `POST /api/admin/user/:id` (`changes`), `/api/admin/user/:id/web` (`action` password/hts_id/logout/remove)
- `GET /api/admin/countries`; `POST /api/admin/countries/:nationality` (`action` create/delete/rates/treasury)
- `GET /api/admin/exchanges`; `POST /api/admin/exchanges/:id` (`action` create/delete/nationality/open/close/pause/resume/circuit/sidecar; `ALL` for every exchange)
- `GET /api/admin/help`, `/api/admin/bot-jobs`
- `GET /api/admin/db/tables`, `/api/admin/db/tables/:table?search=&offset=`
- `POST /api/admin/db/tables/:table/rows` (insert), `/rows/:rowid` (update), `/rows/:rowid/delete`

## Tests and CI

- Python: `.venv/bin/pip install -r requirements-dev.txt`, then
  `.venv/bin/python -m pytest` (or `python -m unittest discover -s tests`).
  `tests/test_web_api.py` covers candles, drawings, audit/events, the DB editor,
  and the command catalog against a temporary database.
- Web: `npm run typecheck` and `npm run build`.
- `.github/workflows/ci.yml` runs both on pushes and pull requests to `main`.

The HTML mockups in `ui-mockups/` and `glass-dashboard/` remain reference
material only.
