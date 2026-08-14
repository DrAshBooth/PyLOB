# Architecture

> **Historical record; annotated 2026-08-14.** This is the draft written on the
> day the modernization started, before the in-memory engine, the test suite,
> the ADR set or the frozen specs existed. It is kept because it is the
> provenance of the standing constraints — the `context:` block of
> `openspec/config.yaml` is the version of this document that is true now, and
> `docs/adr/README.md` records the decisions that got there. Read this one for
> what was believed on 2026-08-10, not for what the code does.
>
> The "edit freely" instruction below is spent: like the dated reviews in
> `docs/`, the text is left as written and only this note is new. Where its
> claims ended up:
>
> - **"No tests"** — false since 2026-08-12. `./verify` runs a pytest stage;
>   the suite is 431 tests and one xfail, layered so that essentially no test
>   can fail indistinctly.
> - **"SQLite trial: resolved"** — resolved further than this says. ADR-0001
>   made SQLite an optional off-hot-path sink; ADR-0003 then retired the SQL
>   engine outright, so there is one engine. The trial's stated open
>   remainder is closed both ways: the sink's event schema is
>   `src/PyLOB/events.py` with `src/PyLOB/sinks/sqlite.py` behind it, and
>   balances and commissions compute in the core, with the sink recording what
>   the engine computed rather than deriving it.
> - **439 orders/sec** — a historical origin, not a live denominator. The
>   engine it measured is deleted and cannot be re-measured; ADR-0005 replaced
>   the ratio with calibration-normalised baselines in
>   `benchmarks/baselines.json`.
> - **"Correctness debt"** (issue #8's two `fulfilled` bugs, issue #3's
>   overlap) — fixed, and pinned by `tests/test_issue8_regressions.py`. The
>   correctness oracle this anticipated arrived, but not from issue #8's
>   external benchmark: it is `tests/reference/matcher.py`, derived from the
>   specs and sharing no code with the engine.
> - **"Docs describe deleted code"** — done. README was rewritten for the
>   shipped engine and the wiki was rewritten with it; the wiki's
>   `Implementation` page is kept under its own banner as history.
> - **mypy** ("planned once the code is typed") — the condition passed without
>   the decision being taken. `src/PyLOB` is fully annotated; mypy is still not
>   installed and not a `./verify` stage, and adding a stage is the
>   maintainer's call. `openspec/config.yaml` states that position.
> - **ruff pinned in `./verify`** — moved. The pins live in pyproject's dev
>   group and `uv.lock`; `./verify` runs them through `uv run`.
> - **Resting market orders** *(inferred)* — ruled on rather than inherited.
>   `openspec/specs/order-lifecycle/spec.md` makes a market order
>   immediate-or-cancel: it never rests, so the query asymmetry described here
>   cannot arise.
>
> The public-API and no-PyPI decisions are the two that survive unchanged, and
> they survive in `openspec/config.yaml`, which is where a change proposal
> must read them.

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
