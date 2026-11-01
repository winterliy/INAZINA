# CURRENCY System Exchange Implementation Plan

> **For agentic workers:** Execute this plan task by task in the current session.

**Goal:** Make CURRENCY a system-only exchange that ordinary exchange controls cannot stop or reconfigure.

**Architecture:** Keep CURRENCY visible as a read-only system status panel with its active FX pairs. Enforce the same restrictions server-side, keep it open during session processing, and exclude it from bulk controls.

**Tech Stack:** React, TypeScript, Python, SQLite.

**Spec:** User request in this conversation: CURRENCY is a special exchange for system operations and should be restricted accordingly.

## Global Constraints

- Preserve the existing CURRENCY exchange and its currency pairs.
- Do not let single-exchange or ALL controls pause, halt, schedule, or change its country.
- Keep automatic currency conversion and pair-price updates working.
- Preserve unrelated uncommitted changes in ExchangesPage and style.css.

## Review Focus

- CURRENCY legacy halt state: normalize to system-open during session processing.
- ALL market controls: never apply to CURRENCY.
- Direct API requests: reject hours, country, and state changes for CURRENCY.
- Normal exchange behavior: retain all existing controls.
- System pair data: render absent or empty pair lists without crashing.

## Files

- `managers/market_session_manager.py`: system-open behavior and control guards.
- `web_api/server.py`: exchange settings API guard and active FX pair payload.
- `apps/dashboard/src/ExchangesPage.tsx`: special read-only CURRENCY panel.

## Tasks

### Task 1: Protect backend session controls

- Guard CURRENCY from direct state changes and trading-hours changes.
- Exclude CURRENCY from ALL market controls.
- Keep CURRENCY system-open during session processing.
- Reject country changes through the admin exchange API.

### Task 2: Specialize the admin screen

- Add active FX pair data to the exchange payload.
- Replace ordinary controls for CURRENCY with a read-only system status and pair list.
- Update bulk-control copy to identify that system exchanges are excluded.

### Task 3: Verify

- Check the diff for whitespace and unintended changes.
- Build the dashboard and inspect available compiler output.
