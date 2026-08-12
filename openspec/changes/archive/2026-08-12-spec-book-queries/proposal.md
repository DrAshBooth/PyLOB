# Proposal: spec-book-queries

## Why

The 2026-08 architecture review found the book's read side and order-lifecycle
edge cases to be *undefined rather than decided*: resting market orders
permanently outrank all limit orders and are invisible to best-price queries
(review §1.6), a wrong-side modify silently reports success (§1.7), restart
reissues duplicate idNums and one cancel then kills multiple orders (§1.4),
non-decimal ticks silently misquantize (§1.8), and validation failures kill
the host process (§1.9). The in-memory engine (ADR-0001) needs these ruled
before it can be built; this change turns each pathology into a contract.

## What Changes

- **Market orders become IOC** (maintainer ruling, 2026-08-10): fill against
  available liquidity, cancel the remainder. **BREAKING** vs current behavior:
  no order ever rests with a NULL price. This dissolves the entire NULL-price
  pathology (rest-at-top-forever, taker-price formation, best-price `None`
  over a non-empty book, market-vs-market matching at lastprice).
- Best/worst price and volume queries get defined semantics over a book that
  contains only priced orders.
- Order identity gets a contract: idNums unique per book, cancel targets
  exactly one order, unknown or mismatched targets raise errors.
- Modify semantics defined: side immutable, qty-below-fulfilled clamps,
  priority rules stated (qty increase or price change loses time priority;
  qty decrease keeps it — assumption flagged in design for maintainer review).
- Validation failures raise library exceptions; `sys.exit` leaves the API.
- Tick quantization defined for arbitrary positive ticks.
- Deterministic FIFO: ties at a price level break by arrival sequence.

This is a spec-first change: rulings land as spec deltas now; implementation
arrives with `inmemory-engine` (and is not backported to the legacy SQL engine
except where `fix-fulfilled-accounting` already touches it).

## Capabilities

### New Capabilities

- `order-lifecycle`: submission validation, identity, cancel/modify semantics,
  market-order IOC execution, priority rules.
- `book-queries`: best/worst price, volume-at-price, last-trade price, and
  book snapshot semantics.

### Modified Capabilities

<!-- none: order-matching (fill accounting) is untouched; its delta lives in
     fix-fulfilled-accounting -->

## Impact

- No code in this change (spec-first). Implementation lands in
  `inmemory-engine`, which depends on these rulings.
- The legacy SQL engine intentionally diverges from these specs (it rests
  market orders); the divergence ends when `inmemory-engine` replaces it as
  the default. Acceptance tests run against both engines, with the specified
  divergences marked xfail on the legacy engine (see tasks).
- `example.py`'s "Outstanding Market Orders" section will describe retired
  behavior once IOC lands; `rewrite-docs` owns that cleanup.
