# Administrator-executed share restructuring implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Record shareholder resolutions separately from administrator-executed formal par splits, stock splits and reverse splits.
**Architecture:** Persist resolution evidence and immutable execution links. Validate and consume an approved resolution inside the same transaction as a formal split; ordinary stock splits and reverse splits retain explicit administrator execution.
**Tech Stack:** Python/SQLite, existing corporate-action manager, React/TypeScript dashboard.
**Spec:** docs/superpowers/specs/2026-10-31-share-restructuring-design.md

## Global constraints
- Meetings resolve; administrators execute. Recording a passed resolution never moves shares.
- Preserve capital and exact cost basis; refuse unsupported encumbrances and stale approvals.
- Keep all existing uncommitted user changes.

## Review focus
- Rejected, mismatched, stale or previously used resolutions must not execute.
- A forged role or missing documents must not change share state.
- An execution failure must not consume the resolution.
- Configured par value must match capital and quantity before and after formal execution.
- Small per-share cost must not silently erase acquisition basis.

### Task 1: Resolution records and execution validation
Files: database/corporate_db.py, managers/share_restructuring_manager.py, tests/test_share_restructuring.py.
- [x] Write tests for record-only behavior, approved formal execution, rejection, stale snapshots, replay, unauthorized access and rollback.
- [x] Run pytest tests/test_share_restructuring.py -q and observe missing implementation failures.
- [x] Add resolution schema and ShareRestructuringManager.record/prepare/finish/history. Snapshot ratio, shares, capital, currency, par value, references and decision.
- [x] Run the focused tests.

### Task 2: Atomic integration and HTTP delivery
Files: managers/corporate_actions_manager.py, managers/company_account_manager.py, web_api/server.py.
- [x] Add formal/stock/reverse modes to adjust_face_value without changing legacy command behavior.
- [x] Run prepare and finish inside the existing transaction. Expose capital/par and history through company detail.
- [x] Add authenticated admin company share-actions endpoint for record and execute.
- [x] Test route dispatch and rejected payload behavior, then run web and company suites.

### Task 3: Administrator dashboard
Files: apps/dashboard/src/CompaniesPage.tsx.
- [x] Add three modes, distinct resolution-result recording and administrator execution controls, references and par-value fields.
- [x] Show only matching passed/unconsumed resolutions in the formal execution selector and show history.
- [x] Build the dashboard and check failure, loading, keyboard labels and disabled states.

### Task 4: Verification and remaining integration
- [x] Run share-restructuring, split, company-account, web and liquidity tests; dashboard typecheck/build; git diff --check.
- [x] Verify database initialization preserves old records; do not report full 100경 support as completed until every amount path is migrated.
- [x] Execute the requested 25-stock batch only after exact total-cost preservation is implemented and validated. Keep an explicit backup and before/after report.

## Execution ledger
- Administrator-evidenced resolution recording selected by the user; no automatic execution or new voting engine.
- Ruling: keep fractional total acquisition basis as exact rational text. Per-share display may round, but basis allocation does not. This avoids rounding fractional lots or a split's repeating unit basis.
- Guarded unsupported fractional-basis custody and linked pledges rather than silently converting them.
- Replicated DB dry run: 25 stocks passed quantity, capital and total-basis invariants.
- Applied 25 real stock splits with full DB backup and per-stock report. API health returned ok and Discord bot reconnected.
- Whole-application 100경 migration remains separate and unfinished; liquidity startup repair alone is not full wide-money support.
