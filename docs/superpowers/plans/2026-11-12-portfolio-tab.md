# Portfolio Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move holdings analysis into a new-tab portfolio page with an allocation donut, performance rankings, and a complete holdings list.

**Architecture:** Reuse the existing `/api/me/portfolio` result and instrument quotes. The main HTS keeps only wallet totals; `/portfolio` renders a dedicated `PortfolioPage` using a CSS/SVG donut and responsive ranking/list panels. The chart toolbar owns account refresh, while the wallet action slot opens the portfolio tab.

**Tech Stack:** React, TypeScript, existing CSS, existing money formatters, Vite.

**Spec:** User-approved option 1: a separate portfolio tab; move refresh to the chart top bar and replace the wallet refresh button with a portfolio button.

## Global Constraints

- Open the portfolio page in a new browser tab.
- Do not add a new API or dependency; use current portfolio and instrument data.
- Keep the main page focused on quote/chart/order content and wallet totals.
- Use existing project formatting and navigation conventions.

## Review Focus

- No holdings: show an empty state instead of a broken donut.
- One holding or zero-valued holdings: render a valid allocation graphic.
- Missing `change24h`: exclude it from ranking rather than showing fake performance.
- Long names/codes: keep cards readable on mobile.
- Refresh failures: preserve the current page and show the existing notice.

### Task 1: Portfolio data presentation

**Files:**
- Create: `apps/hts/src/PortfolioPage.tsx`
- Test: `apps/hts/src/PortfolioPage.test.ts`

- [ ] Add pure helpers for allocation segments and performance ranking.
- [ ] Add failing tests for allocation percentages, missing changes, and positive/negative ordering.
- [ ] Implement the page with donut, rankings, and all holdings.
- [ ] Run the focused tests.

### Task 2: HTS routing and controls

**Files:**
- Modify: `apps/hts/src/App.tsx`
- Modify: `apps/hts/src/style.css`

- [ ] Add `/portfolio` route using authenticated portfolio/instrument state.
- [ ] Put `새로고침` in the chart control bar and wire it to `refreshAccount`.
- [ ] Replace the wallet refresh slot with a new-tab `내 주식` button.
- [ ] Remove the main-page holdings list while retaining other wallet/account panels.
- [ ] Add responsive page and donut/ranking styles.

### Task 3: Verification

- [ ] Run typecheck, focused tests, full build, and `git diff --check`.
- [ ] Confirm no development server is left running.
