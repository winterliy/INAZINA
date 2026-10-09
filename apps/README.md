# Local Web Foundation

Three independent local services are grouped in the npm workspace:

- Dashboard: `http://127.0.0.1:4310`
- HTS: `http://127.0.0.1:4311`
- API: `http://127.0.0.1:4312/api/health`

Run `npm install` once, then start each service in a separate terminal with
`npm run dev:dashboard`, `npm run dev:hts`, and `npm run dev:api`.

The two Vite apps proxy `/api` requests to the local API. The API currently
provides only a health endpoint; it is not connected to the Python bot or its
database. The existing HTML mockups remain separate reference material.
