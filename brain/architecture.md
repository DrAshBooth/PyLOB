# Architecture

Draft from the 2026-08-10 setup interview. Edit freely — lines marked
*(inferred)* were deduced from the code/history, not stated by the maintainer.

## What this is

A simulation-for-research limit order book: price-time priority, market/limit
orders with add/cancel/update, single-threaded, zero-latency assumption.
Consumed as a library by people running LOB simulations. Public GitHub repo,
MIT, installable from GitHub — deliberately not on PyPI.

## Decisions that would be expensive to reverse

- **Public API surface.** `OrderBook.processOrder(dict, fromData, verbose)`,
  `cancelOrder`, `modifyOrder`, `getVolumeAtPrice`, `getBest*/getWorst*` is kept
  unless it proves a limiter of performance or clarity. Once people install from
  GitHub, breaking it breaks them; changing it needs an ADR.
- **No PyPI.** The `pylob` name is held by an unrelated active package
  (deltaleap/pylob). Decision: skip PyPI rather than rename. Reversing later
  means picking a new distribution name and migrating installs.
- **Modernization baseline.** Python >= 3.11, uv for versions/deps, ruff, pytest,
  pyproject.toml. Cheap individually, expensive collectively once the rewrite
  builds on them.

## Decisions that are cheap to revisit

- ruff version pin in `./verify` (moves into uv dev-deps when pyproject lands)
- `./verify` stage list (by contract: only ever changed by asking the maintainer)
- Whether mypy joins `./verify` (planned once the code is typed)

## Known unknowns

- **SQLite trial: resolved.** See ADR-0001 — matching moves in-memory, SQLite
  becomes an optional off-hot-path sink. Measured basis: 439 orders/sec on the
  current engine (20k mixed workload, 2026-08-10). Benchmarks stay out of
  `./verify`. Open remainder: the sink's event schema and whether balances/
  commissions compute in the core or the sink.
- **Correctness debt.** Issue #8 reports two `fulfilled`-column accounting bugs
  in current master (trigger updates by `idNum` not `order_id`; reprice-cross
  never subtracts fills). Issue #3's modify-then-reprice scenario overlaps.
  *(inferred)* The external matching-engine consensus benchmark from issue #8
  could become the correctness oracle once a test suite exists.
- **No tests.** setup.cfg points at a `tests/` that does not exist. The pytest
  suite is grown during the rewrite and joins `./verify` when real.
- **Docs describe deleted code.** README still claims pure-Python RBTrees and
  zero dependencies; the wiki matches the pre-SQL implementation. Wiki updates
  are in scope.
- *(inferred)* Resting market orders (price NULL) are invisible to
  `getBestAsk`/`getBestBid` while still counted by `getVolumeAtPrice` — seen in
  the smoke run's final book state. Unclear if intended.
