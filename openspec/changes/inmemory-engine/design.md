# Design: inmemory-engine

## Context

ADR-0001 fixes the architecture; the three spec changes fix the behavior.
Constraints: existing public API preserved (standing constraint); Python
>= 3.11, stdlib-only for the core (research portability — the original
project's zero-dependency ethos); legacy engine stays as differential oracle.

## Goals / Non-Goals

**Goals:**
- Single-layer correctness: one code path owns eligibility, allocation, and
  accounting.
- >= 100x the legacy baseline (439 orders/sec) on the benchmark workload —
  the benchmark change enforces; this design must not preclude it.
- Every acceptance suite passes unchanged.

**Non-Goals:**
- No async/concurrency (single-threaded by standing constraint).
- No removal of the legacy engine (separate change once cross-checks retire).
- No new dependencies.

## Decisions

1. **Book structure: per-instrument, per-side dict of price -> FIFO deque of
   orders, plus a sorted price index (heap with lazy deletion).** Boring,
   stdlib, O(log L) per operation in the number of price levels, empirically
   fast in CPython. *Alternatives rejected:* red-black tree (the 2013
   implementation — more code for no measured win at research scale);
   sortedcontainers (dependency).
2. **Priority = (price level, arrival sequence).** A single engine-wide
   monotonically increasing sequence number stamps every acceptance and every
   priority-losing modify (price change / qty increase re-stamps; qty
   decrease does not) — implements the lifecycle spec's determinism
   requirement; replay timestamps are recorded data, never sort keys.
3. **Commissions and balances compute in-core** (dict-based ledgers),
   updated per fill from cumulative (Q, V) recompute. Rationale: online PnL
   is a research feature; making it sink-only would put a required feature
   behind an optional component. The sink persists ledger events, it does not
   own them.
4. **Events are plain frozen dataclasses on a synchronous dispatch;**
   sinks implement a `consume(event)` protocol. Synchronous keeps replay
   deterministic and the core simple; "off the hot path" is achieved by the
   sink buffering internally (the SQLite sink batches with executemany on
   flush thresholds and close). *Alternative rejected:* background-thread
   sink — breaks single-threaded constraint and determinism for no measured
   need at 20k-order scale.
5. **Sink schema is a new, event-shaped schema** (events, orders, trades,
   ledger tables), not the legacy `create_lob.sql`. The legacy schema encodes
   matching (views, triggers) that ADR-0001 retires; carrying it would
   re-couple layers. `create_lob.sql` stays with the legacy engine only.
6. **Differential harness:** seeded random workloads run through both
   engines; comparator asserts identical trades, book states, balances,
   commissions, modulo a whitelist of specified divergences (IOC remainder,
   priority-on-price-change). Divergence outside the whitelist fails.
7. **Module layout:** `PyLOB/engine.py` (core), `PyLOB/events.py`,
   `PyLOB/sinks/sqlite.py`; `PyLOB/orderbook.py` remains the legacy engine;
   `PyLOB/__init__.py` exports the new engine as `OrderBook` and the legacy
   as `LegacyOrderBook`. The import swap is the breaking moment and happens
   in this change, with the acceptance suites as the gate.

## Risks / Trade-offs

- [API preserved means dict-quote inputs and `(trades, quote)` returns
  survive into the new engine] → accepted per standing constraint; an API
  redesign is a future ADR if clarity demands it.
- [Synchronous sink dispatch costs latency per event] → buffered writes;
  benchmark guards the 100x target with the sink attached and detached.
- [Legacy comparisons are only as good as the fixed legacy engine] →
  fix-fulfilled-accounting is a hard dependency; whitelist keeps the
  comparison honest about specified breaks.
- [`print` output format is depended on by nothing but eyeballs] → keep the
  legacy text shape to avoid gratuitous diff noise in example output.

## Open Questions

- Sink flush threshold default (pure tuning; benchmark data decides).
