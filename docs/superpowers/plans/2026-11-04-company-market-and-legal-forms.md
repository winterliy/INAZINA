# Company Market and Legal Form Update Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate company legal form, ownership unit, listing route, and trading status; make OTC availability country-specific and classify 3DSG as Nakwon's OTC market.

**Architecture:** Keep exchange identifiers as market venues, with explicit venue type and nationality. Keep company lifecycle, trading status, and legal form in distinct company fields. Stock companies continue using the existing share register; non-stock companies use a separate capital-interest register and never create tradable share assets.

**Tech Stack:** Python, SQLite migrations/UnifiedConnection, existing admin API and Discord command registry, React/TypeScript dashboard.

**Spec:** User request in conversation; includes exchange selection for countries without an OTC venue.

## Global Constraints

- 3DSG is an OTC market with nationality `낙원`.
- A country with no OTC venue must still be able to keep a company private without assigning it to another country's market.
- Listing route and trading status are independent; suspending trading preserves ownership and company records.
- Non-stock legal forms use capital interests, not share assets or share classes.
- Existing company, exchange, ownership, and transaction records must remain readable during migration.
- Do not add or run tests unless the user asks; use syntax checks and read-only state inspection for verification.

## Review Focus

- Country has no OTC venue: private companies must store no exchange and remain non-tradable.
- OTC venue belongs to a different nationality: it must not appear in a company's listing choices.
- Trading-suspended company: orders stop while ownership, dividends, and company profile remain.
- Non-stock legal form: no stock ticker or share issuance may be created; capital interests must drive ownership views.
- Existing inactive stock and 3DSG rows: migration must preserve the current user-facing meaning.

---

### Task 1: Market venue and company state schema

**Files:** `database/system_db.py`, `database/company_db.py`, focused schema/migration code.

- [x] Add `market_type` (`exchange`/`otc`) to `market_exchanges`; migrate 3DSG to `otc`, nationality `낙원`.
- [x] Add `legal_form`, `ownership_unit`, and `trading_status` to companies, with backward-compatible defaults and migration of existing suspended rows.
- [x] Allow `exchange_id=NULL` to represent private with no OTC venue; retain `3DSG` as an actual market venue.
- [x] Keep existing `status` as company lifecycle status; make the trading status field control order eligibility.

### Task 2: Admin listing and trading controls

**Files:** `web_api/server.py`, `managers/admin_control_manager.py`, `managers/market_session_manager.py`, relevant market/order managers.

- [x] Return OTC type and nationality in exchange payloads.
- [x] Filter selectable exchanges to the company's nationality; add a private/no-OTC choice when appropriate.
- [x] Add an explicit trading-status operation; suspension cancels open orders and releases reservations through existing cancellation paths.
- [x] Apply `trading_status` consistently to order placement, matching, price updates, and listing indexes.

### Task 3: Legal form and ownership model

**Files:** `database/company_db.py` or focused legal-entity/participation modules, `managers/admin_control_manager.py`, `web_api/organizations.py`, ownership endpoints and command metadata.

- [x] Support stock company, limited company, limited liability company, general partnership, and limited partnership as distinct forms.
- [x] Add capital-interest records for non-stock entities, including holder, contribution amount, and voting/economic share.
- [x] Prevent stock issuance, exchange assignment, public orders, stock dividends, and shareholder meetings for non-stock forms.
- [x] Add validated contribution-interest editing and include capital interests in ownership graphs; a transfer is entered by updating the source and destination records.

### Task 4: Dashboard labels and controls

**Files:** `apps/dashboard/src/CompaniesPage.tsx` and related company/account views.

- [x] Show legal form, venue route (`listed`, `OTC registered`, `private/no OTC`), and trading status independently.
- [x] Show the actual OTC market name/ID and nationality; never label 3DSG simply as generic “unlisted”.
- [x] Render non-stock ownership as contributions/partner interests, not share counts.

### Task 5: Verification

- [x] Run `git diff --check`, Python syntax parsing, dashboard typecheck and build.
- [ ] Inspect migrated 3DSG metadata and confirm existing stock holdings remain intact (startup migration not run against the local database).
- [x] Inspect company selector and implementation paths for Nakwon OTC and a country with no OTC venue.
- [x] Confirm non-stock records are blocked from stock product and issuance creation paths by server validation.
