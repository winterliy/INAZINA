# Changelog

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
