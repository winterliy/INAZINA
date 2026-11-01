# Wide money implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for the sequential integration; independent backend limits and frontend transport may be delegated under dispatching-parallel-agents.

**Goal:** Support at least 100경 exactly across every monetary path and verify persistence, arithmetic, transport, display and existing-data migration.
**Architecture:** Preserve SQLite using exact decimal text money columns, connection-wide exact arithmetic/aggregate functions and typed read conversion. Compile SQL arithmetic and monetary DDL at the connection boundary to preserve precision in existing transactions/triggers, then remove remaining Python float monetary boundaries. Large JSON amounts travel as decimal strings; inputs retain exact decimal text. Migrate only a verified offline copy, with full backups and matching process versions.
**Tech Stack:** Python int/Decimal (precision100), SQLite UDFs/collation, SQLGlot AST, React/TypeScript exact amount helpers.
**Spec:** docs/superpowers/specs/2026-10-31-wide-money-design.md (user requested full completion after reviewing remaining scope).

## Global constraints
- MAX_NATIVE=10^24, MAX_MINOR=10^30, MAX_SUBMINOR=10^36. Currency places remain0..6; quantity/score/time limits stay separate.
- Preserve current split/cost-basis features and all uncommitted changes. Do not migrate the live database until clone checks and money-flow tests pass.
- Fail closed for unsupported SQL, NaN/Infinity, excessive exponents and invalid units.
- All monetary updates preserve transaction rollback and conservation. Preserve legacy data without claiming lost float precision was recovered.

## Review focus
- 10^18 ± smallest unit must remain distinct after arithmetic, SUM, comparisons and JSON round trips.
- Trigger arithmetic, conditional debits and ordering must use decimal semantics rather than text ordering or SQLite REAL.
- User IDs/account numbers and quantities must retain their non-monetary semantics.
- Old clients/processes must not write the migrated schema; restore/restart must be validated.
- Native-double price paths must use exact monetary boundaries above their safe range.

### Task1: Exact storage/SQL foundation
- [ ] Add ExactDecimal, tagged text codecs and exact UDF arithmetic, aggregation, collation and row conversion.
- [ ] Add cached SQLite AST conversion for monetary columns, trigger bodies and numeric expressions; guard identifier/parameter semantics.
- [ ] Add clone migration and schema-version gating; register functions for every writable/read-only/web connection.
- [ ] Test huge additions, sums, order, atomic conditional debits, trigger mirrors, restart/migration and ordinary SQLite compatibility.

### Task2: Backend amount boundaries (independent ownership: functions/money.py and managers)
- [ ] Replace monetary-only JS/int64 limits with amount_limits constants. Keep quantity/ID/time constraints unchanged.
- [ ] Eliminate float monetary parsing and round only at currency boundaries; add large-value tests across wallet, corporate, treasury, bank, loan, tax, fund, derivative and price paths.

### Task3: API/frontend exact amounts (independent ownership: apps)
- [ ] Add exact native amount string helpers and large-value formatting; preserve strings in all mutation payloads.
- [ ] Adapt dashboard, HTS, shared forms and API transport. Use approximate conversions only for chart coordinates and explicitly approximate previews.
- [ ] Test100경+1 parse/format/edit/confirm/submit/readback and typecheck/build.

### Task4: Integration and live migration
- [ ] Run existing suite, wide-money flow matrix, SQL/float/cap inventory audit, static type checks and builds.
- [ ] Migrate a live snapshot twice and validate counts, FK/integrity, balances, price state and all ledger reconciliations.
- [ ] Stop writers, checkpoint and back up live DB, publish verified schema, restart all processes and verify API plus representative round trips.
