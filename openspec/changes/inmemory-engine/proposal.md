# Proposal: inmemory-engine

## Why

ADR-0001 (accepted 2026-08-10): the SQL matching path runs at 439 orders/sec
and split correctness across three layers that repeatedly disagreed (issue
#8). A research LOB needs to replay Level-2 event streams at speed with
single-layer correctness. This change builds the in-memory matching engine
that ADR-0001 decided on, with SQLite retained as an optional off-hot-path
recording sink (variant b).

## What Changes

- New in-memory matching core: price levels with price-time priority, all
  eligibility, allocation, and fill accounting in one layer, behind the
  existing public `OrderBook` API (`processOrder`, `cancelOrder`,
  `modifyOrder`, `getVolumeAtPrice`, `getBest*/getWorst*`, `print`).
- Implements every ratified contract: `order-matching`
  (fix-fulfilled-accounting), `order-lifecycle` + `book-queries`
  (spec-book-queries, incl. IOC market orders — **BREAKING** vs the legacy
  engine's resting market orders), `commissions` + `trader-balances`
  (spec-commissions-balances, computed in-core for online PnL).
- New capability `recording-sink`: the engine emits lifecycle events (accept,
  fill, cancel, modify); a SQLite sink adapter consumes them off the hot path
  and persists queryable history (orders, trades, balances, commissions).
  With no sink attached the engine runs at full speed with zero persistence.
- The legacy SQL engine stays importable (cross-check oracle per ADR-0001)
  until a later removal change; `example.py` switches to the new engine.
- Differential validation: both engines run the same randomized workloads;
  divergences beyond the specified breaks (IOC, priority-on-price-change)
  fail the build.

## Capabilities

### New Capabilities

- `recording-sink`: event emission contract and the SQLite sink's
  persistence guarantees.

### Modified Capabilities

<!-- order-matching, order-lifecycle, book-queries, commissions,
     trader-balances are implemented, not re-specified: their requirements
     stand as ratified in the earlier changes. If implementation forces a
     requirement change, that lands as a delta to the relevant capability at
     reconciliation time (CLAUDE.md: reconcile at archive). -->

## Impact

- New modules under `src/PyLOB/` (engine, book structure, events, sink).
- `example.py` rewritten against the new engine.
- Acceptance suites from the three spec changes must pass unchanged; the
  benchmark (next change) guards the performance claim.
- Legacy engine and its SQL files remain until explicitly removed.
