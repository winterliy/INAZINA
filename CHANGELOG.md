# Changelog

## 0.3.0 — 2026-10-24

### Added

- Settle standard and foreign securities orders in quote-currency minor units, including reservations, matching, price improvement, tax, and proceeds, with fills linked to wallet and accounting ledgers.
- Reconcile option order reservations and tighten currency reconciliation.
- Make the initial currency catalog and base currency defaults configurable.
- Generalize fund cash and dividend receipt, and reconcile dividend settlement in the currency audit.
- `$분할` now splits every company asset by the ratio: securities, deposits, and loans in good standing, with both companies' statements reconciled.
- Show the country, region, and city of public addresses in admin login records (DB-IP City Lite, `npm run geoip:update`).

### Changed

- Restyle the dashboard, HTS, and API landing pages after Apple's product pages, and lay out the dashboard and HTS like the product-page mockup.

### Fixed

- Rebuild legacy currency tables without breaking triggers, so an interrupted migration no longer leaves a table dropped.
- Fix the Python landing page crashing on every request.
- Validate deposit insurance payout minor units only when the payout currency is registered.
- Complete the M4 ledger controls.

### Verification

- Python regression suite: 890 tests passed. `test_ctrl_c_stops_quietly` failed once under `pytest -n auto` load and passed on rerun.
- Web production build: API, dashboard, HTS, site, and WL succeeded.
- Python compile check and `git diff --check` passed.

## 0.2.0 — 2026-10-24

### Added

- Completed the M4 internal financial simulator accounting milestone, including currency-minor-unit reconciliation, shared journal mappings, opening and baseline reconstruction, settlement links, and close-boundary checks.
- Added company bond issuance, coupon, and maturity journal postings for same-currency flows.
- Added company stock split processing and administrator login history across the API and dashboard.
- Added registered currency amount-field coverage and reconciliation findings for missing or inconsistent snapshots.

### Verification

- Python regression suite: 864 tests passed.
- Web production build: API, dashboard, HTS, site, and WL succeeded.
- Python compile check and `git diff --check` passed.

### Known limitations

- Some SQLite REAL/float compatibility mirrors remain.
- Historical balances without transaction evidence are represented by opening/baseline entries rather than invented transaction history.
- Foreign-currency corporate bonds and some remittance/tax flows still lack full transaction-time FX fixing and clearing.
- This release targets an internal financial simulator, not production operation of a regulated financial institution.
