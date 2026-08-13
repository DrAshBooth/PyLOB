# ADR-0001: Move matching to an in-memory engine; retain SQLite as an optional sink

Status: Accepted; the transition clause in Consequences is superseded by
ADR-0003 (the SQL engine is retired, not in tree)
Date: 2026-08-10

## Context

PR #7 (2023) moved the entire matching path into SQLite: eligibility in views,
allocation in a Python loop, accounting in triggers. The 2026-08 architecture
review (`docs/architecture-review-2026-08.md`) found the three-layer split to
be the root of multiple correctness bugs (issue #8, broken `trade_delete`
trigger) and measured throughput at **439 orders/sec** on a 20k mixed workload
— three orders of magnitude below what a research simulator needs to replay
Level-2 event streams. The maintainer had placed SQLite "on trial" with
orders/sec as the criterion; this ADR resolves the trial.

## Decision

Matching moves to an in-memory engine: price levels with price-time priority,
all eligibility, allocation, and fill accounting in one layer, behind the
existing public `OrderBook` API. SQLite leaves the hot path but is retained as
an **optional recording/analytics sink**: the engine emits lifecycle events
(orders, trades, fills), and a SQLite adapter persists them off the matching
path for post-hoc queries — trade history, trader balances, commissions.
No SQL statement executes per matched order inside the engine.

## Alternatives considered

- **Keep SQLite and optimize** (pragmas, WAL, prepared statements, schema
  tuning): rejected. The cost is structural — per-order round-trips, view
  scans, trigger cascades — and plausible tuning gains (~5–10x) do not
  approach the ~1000x gap. It also retains the three-layer coupling that
  produced the accounting bugs.
- **Drop SQLite entirely (variant a)**: rejected, narrowly. Simplest and
  truest to the research-library roots, but discards the queryable
  balance/commission/history model PR #7 built. Keeping it as a sink preserves
  that value at zero hot-path cost. If the sink proves unused, removing it
  later is cheap; re-adding it later would not be.

## Consequences

- Makes easy: fast replay/simulation, single-layer correctness reasoning,
  deterministic benchmarking, a reference implementation to cross-check.
- Makes hard: "the DB is always the live book" queries — the sink is
  eventually-written analytics, not the matching state. Restart/persistence
  semantics must be defined by the event log, not by reopening a live book DB.
- Forecloses: SQL-side matching logic. New matching features are engine
  features; the sink only records.
- The behavior specs (order-matching, book-queries, commissions/balances) are
  engine-neutral and carry over unchanged; the existing SQL engine remains in
  tree until the in-memory engine passes the same suite, serving as
  cross-check oracle during the transition.
