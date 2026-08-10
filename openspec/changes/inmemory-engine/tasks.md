# Tasks: inmemory-engine

## 1. Core

- [ ] 1.1 `PyLOB/events.py`: frozen dataclasses (Accepted, Filled, Cancelled,
      Modified) with engine-wide sequence numbers; sink protocol
- [ ] 1.2 `PyLOB/engine.py`: book structure (price->deque, heap index with
      lazy deletion), order store, tick quantization per lifecycle spec
- [ ] 1.3 Matching loop: limit crossing, IOC market orders, self-matching
      gate, maker-price formation, deterministic (price, sequence) priority
- [ ] 1.4 Cancel / modify per lifecycle spec (single-target, validation
      errors as exceptions, clamp rule, priority re-stamping)
- [ ] 1.5 In-core ledgers: balances and cumulative-recompute commissions per
      the commissions/balances specs

## 2. Sink

- [ ] 2.1 `PyLOB/sinks/sqlite.py`: event-shaped schema, buffered
      executemany writes, flush on threshold and close
- [ ] 2.2 Replay: load a persisted event stream into a fresh engine;
      end-state equality per recording-sink spec

## 3. Validation

- [ ] 3.1 All acceptance suites (lifecycle, book-queries, commissions,
      balances, recording-sink) green against the new engine
- [ ] 3.2 Issue-#8 regression tests green against the new engine
- [ ] 3.3 Differential harness: seeded workloads vs fixed legacy engine,
      whitelist only IOC + priority-on-price-change divergences
- [ ] 3.4 Sink-attached vs sink-detached outcome equality (seeded workload)

## 4. Swap

- [ ] 4.1 `__init__.py` exports new engine as `OrderBook`, legacy as
      `LegacyOrderBook`; `example.py` rewritten on the new engine
- [ ] 4.2 `./verify` green end to end; smoke stage exercises the new engine
- [ ] 4.3 Handoff notes: measured orders/sec (informal; formal guard is the
      benchmark change), any spec reconciliations needed at archive
