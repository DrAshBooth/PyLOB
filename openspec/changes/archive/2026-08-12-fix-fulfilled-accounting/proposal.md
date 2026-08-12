# Proposal: fix-fulfilled-accounting

## Why

GitHub issue #8 documents, with deterministic repros against current master
(`c0dd932`), two independent accounting bugs in the `fulfilled` column that both
allow a resting order to be matched past its true size — corrupting fills,
trader balances, and counterparty liquidity. For a simulation-for-research LOB,
fill accounting is the core correctness property; and the repo currently has no
test suite to keep it fixed.

Under ADR-0001 (matching moves in-memory), this change is also the foundation
of the transition: its engine-neutral test suite becomes the oracle the new
engine is built against, and the fixed SQL engine serves as the cross-check
implementation.

## What Changes

- Fix the `trade_insert` trigger in `src/create_lob.sql`: it credits fills to
  `trade_order` rows matched by `idNum`, but `trade.bid_order`/`trade.ask_order`
  store `order_id` values. Fills land on the wrong rows whenever the two
  identifier sequences diverge.
- Fix `OrderBook.processMatchesDB` in `src/PyLOB/orderbook.py`: it sets
  `qtyToExec = quote["qty"]` without subtracting `fulfilled`, so a repriced,
  partially-filled order re-trades its full size (issue #8's verified fix:
  carry `fulfilled` through `modifyOrder`'s order-update dict and subtract it,
  defaulting to 0 on the fresh-order path).
- Drop the `trade_delete` trigger from `src/create_lob.sql`: the 2026-08
  architecture review confirmed it has never been runnable (`new.*` references
  in a DELETE trigger), and ADR-0001 removes any future need for a SQL-side
  fill-reversal path.
- Create the repo's first pytest suite in `tests/` (where `setup.cfg` already
  points): order-lifecycle coverage (add, cancel, modify, crossing, partial
  fills, market orders) plus both issue-#8 repros as regression tests with the
  issue's expected numbers.
- **Decision point for the maintainer (amendment rule):** add a `test` stage
  running pytest to `./verify`. `./verify` is the contract; this addition ships
  only with explicit approval.

Not breaking: no public API changes; behavior changes only where current
behavior is wrong.

## Capabilities

### New Capabilities

- `order-matching`: fill accounting for the order lifecycle — fills credited to
  the correct orders, no order ever matched beyond its unfulfilled remainder,
  including across modify/reprice.

### Modified Capabilities

<!-- none: no existing specs yet; this change introduces the first one -->

## Impact

- `src/create_lob.sql` — `trade_insert` trigger (schema change; existing DBs
  built from the old schema keep the old trigger — the committed `src/lob.db`
  is regenerated or left as historical data, decided in design)
- `src/PyLOB/orderbook.py` — `processMatchesDB`, `modifyOrder`
- `tests/` — new; pytest run via uv
- `./verify` — pytest stage, pending maintainer approval
- Constraints respected: public API unchanged; SQLite dependence neither
  deepened nor reduced (fix-in-place); single instrument/currency
