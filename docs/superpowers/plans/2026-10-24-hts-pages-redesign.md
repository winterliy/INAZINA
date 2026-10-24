# HTS Pages Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate HTS command and account screens, add useful lower chart plots, and redesign the board around a DCinside-inspired community layout.

**Architecture:** Keep the existing React/Vite app and APIs. Add direct path dispatch in `main.tsx`, extract reusable page shells for commands/account, retain command and board API behavior, and extend the existing SVG chart with aligned lower plots.

**Tech Stack:** React 19, TypeScript, Vite, SVG, existing REST API.

**Spec:** `docs/superpowers/specs/2026-10-24-hts-pages-and-board-redesign.md`

## Global Constraints

- Keep the existing trading, account, and board APIs and user data working.
- Do not import DCinside assets or copy its branding; use its information hierarchy and compact forum conventions as reference.
- Do not change financial calculations or command semantics.
- Keep the experience usable on mobile and retain authentication and return-to-page behavior.

## Review Focus

- Deep links with unknown command names must resolve to a safe command page or helpful not-found state.
- Sparse/missing candle volume must show an honest empty state and not break price rendering.
- Long titles, author names, and Korean text must remain readable in compact board rows.
- Logged-out account access must preserve the intended return URL after login.
- No credit data must not make the wallet fail to render.

---

### Task 1: Route shell and command pages

**Files:**
- Create: `apps/hts/src/CommandPage.tsx`
- Modify: `apps/hts/src/CommandPanel.tsx`
- Modify: `apps/hts/src/main.tsx`
- Modify: `apps/hts/src/App.tsx`
- Modify: `apps/hts/src/style.css`
- Modify: `apps/dashboard/src/App.tsx`
- Modify: `apps/dashboard/src/style.css`

**Interfaces:**
- Consume current `/api/hts/commands` command definitions and existing form/result behavior.
- Produce `/commands/<encoded-command-name>` pages, one selected command per URL, with group navigation and shared page shell.

- [ ] Refactor `CommandPanel` to expose a standalone selected-command page while preserving fields, lookups, execution, confirmation, and results.
- [ ] Dispatch `/commands/:name` in `main.tsx`; unknown names get a safe in-app route state.
- [ ] Replace the main header modal trigger with navigation to the command index and link command groups from the standalone pages.
- [ ] Update styles for responsive page navigation and forms.
- [ ] Add function-category tabs to the dashboard administrator command picker and filter its command list by the selected category.
- [ ] Review command read/write flows and direct URL behavior against the existing route and API contracts.

### Task 2: Dedicated account page and wallet credit placement

**Files:**
- Modify: `apps/hts/src/main.tsx`
- Modify: `apps/hts/src/App.tsx`
- Modify: `apps/hts/src/style.css`

**Interfaces:**
- Consume existing account identity, portfolio, and credit endpoints.
- Produce `/account` with identity settings and account information; make the home wallet own the credit summary.

- [ ] Move the account identity editor flow from the modal into the dedicated account page without changing its API or validation.
- [ ] Render account identity/account information at `/account`, including auth redirect with return path.
- [ ] Place the credit score, factors, and recent events inside the wallet panel and remove the separate bottom card.
- [ ] Review visitor, no-credit-data, and mobile states.

### Task 3: Lower chart plots

**Files:**
- Modify: `apps/hts/src/PriceChart.tsx`
- Modify: `apps/hts/src/App.tsx`
- Modify: `apps/hts/src/style.css`

**Interfaces:**
- Consume `points`, `candles`, `candleInterval`, selected range, and current mode.
- Produce a main price plot plus a time-aligned volume plot and selectable auxiliary plot where source values exist.

- [ ] Derive and render volume bars from candle ticks/volume data in a separate lower SVG region using the same horizontal viewport.
- [ ] Add at least one useful auxiliary series (price change/return) in a separate lower plot with clear labeling and an unavailable-data state.
- [ ] Preserve existing price drawing, zoom, pan, and live-follow behavior.
- [ ] Review sparse series, mode changes, resize, and small-screen layout.

### Task 4: Community board rebuild

**Files:**
- Modify: `apps/hts/src/BoardPanel.tsx`
- Modify: `apps/hts/src/BoardPage.tsx`
- Modify: `apps/hts/src/style.css`

**Interfaces:**
- Consume existing board/post APIs and current board state/actions.
- Produce a responsive standalone gallery layout with board/category tabs, compact post table, clear notice/popular labels, search, paging, and a focused post reading view.

- [ ] Replace the current split modal-like page composition with a full-width community page structure.
- [ ] Render gallery navigation and post columns inspired by DCinside conventions; preserve notices, pinned items, filters, asset discussions, admin controls, and pagination.
- [ ] Keep post reading, writing, attachments, polls, reactions, reports, comments, and moderation wired to existing actions.
- [ ] Review dense data, empty/loading/error states, logged-out writing, and mobile layout.

### Task 5: Shared copyright footer

**Files:**
- Modify: `apps/dashboard/src/App.tsx`
- Modify: `apps/hts/src/App.tsx`
- Modify: `apps/hts/src/CommandPanel.tsx`
- Modify: `apps/hts/src/BoardPanel.tsx`
- Modify: `apps/hts/src/style.css`

- [ ] Replace the dashboard local-console line and add `© INAZINA. All rights reserved.` to the bottom of HTS home, account, command, and board pages.

## Completion

- [ ] Review the resulting diff and modified route/page interactions.
- [ ] Report any checks not run and any unavailable chart data honestly.
