# Tasks: inmemory-engine

## 1. Core

- [x] 1.1 `PyLOB/events.py`: frozen dataclasses (Accepted, Filled, Cancelled,
      Modified) with engine-wide sequence numbers; sink protocol
- [x] 1.2 `PyLOB/engine.py`: book structure (price->deque, heap index with
      lazy deletion), order store, tick quantization per lifecycle spec
- [x] 1.3 Matching loop: limit crossing, IOC market orders, self-matching
      gate, maker-price formation, deterministic (price, sequence) priority
- [x] 1.4 Cancel / modify per lifecycle spec (single-target, validation
      errors as exceptions, clamp rule, priority re-stamping)
- [x] 1.5 In-core ledgers: balances and cumulative-recompute commissions per
      the commissions/balances specs

## 2. Sink

- [x] 2.1 `PyLOB/sinks/sqlite.py`: event-shaped schema, buffered
      executemany writes, flush on threshold and close
- [x] 2.2 Replay: load a persisted event stream into a fresh engine;
      end-state equality per recording-sink spec

## 3. Validation

- [x] 3.1 All acceptance suites (lifecycle, book-queries, commissions,
      balances, recording-sink) green against the new engine
- [x] 3.2 Issue-#8 regression tests green against the new engine
- [x] 3.3 Differential harness: seeded workloads vs fixed legacy engine,
      whitelist only IOC + priority-on-price-change divergences
- [x] 3.4 Sink-attached vs sink-detached outcome equality (seeded workload)

## 4. Swap

- [x] 4.1 `__init__.py` exports new engine as `OrderBook`, legacy as
      `LegacyOrderBook`; `example.py` rewritten on the new engine
- [x] 4.2 `./verify` green end to end; smoke stage exercises the new engine
- [x] 4.3 Handoff notes: measured orders/sec (informal; formal guard is the
      benchmark change), any spec reconciliations needed at archive

---

Reconciled at archive time, 2026-08-13. Beads were the execution source of
truth (`lob-5rt.1` .. `lob-5rt.15`); these boxes are checked from their
closure, not the other way round.

Two things happened after this change's tasks were written, and both belong in
the record rather than in a tick.

**A pre-retirement review (`lob-8k1`, `docs/engine-review-2026-08.md`) found six
P1 defects in what this change delivered**, and all six were fixed before the
engine was trusted: a NaN price that crossed the book between innocent traders,
an O(k squared) self-match walk that collapsed on the RL-gym shape, a heap that
never compacted, a sink that could lose acknowledged events silently, public
mutations that emitted no event, and a test suite that leaned on the legacy
oracle for five money-and-book mutants. The engine described by the tasks below
is not the engine that shipped; it is the engine that shipped minus those six.

**The legacy engine has since been retired** (ADR-0003). This change's design
assumed it would stay in tree as a cross-check oracle; that oracle was replaced
by a spec-derived reference matcher in `tests/reference/` before the deletion,
which is why the differential harness outlived the engine it was written
against.

The four reconciliations `HANDOFF.md` listed are all discharged:
`openspec/config.yaml`'s stale `context:` block was rewritten, ADR-0001's
transition clause was superseded by ADR-0003, this change's `recording-sink`
delta is synced into `openspec/specs/`, and ADR-0002 stands with the caveat
that its 100x denominator retired with the legacy engine (`lob-0m4`).
