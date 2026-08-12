# Tasks: fix-fulfilled-accounting

## 1. Test scaffolding (red first)

- [x] 1.1 Create `tests/conftest.py`: prepend `src/` to `sys.path`; add a
      fixture that builds a fresh sqlite DB in `tmp_path` from
      `src/create_lob.sql`, seeds traders + the `FAKE` instrument (the
      `example.py` pattern), and yields a connected `OrderBook`
- [x] 1.2 Fetch issue #8's two repro scripts (`gh api repos/DrAshBooth/PyLOB/issues/8`)
      and port them to `tests/test_issue8_regressions.py`, asserting the
      issue's expected post-fix numbers (reprice trades 6 not 10; A ends
      fulfilled=10; C retains 14; follow-up 12-lot bid fills in full; bug-1
      fills credited only to participating orders)
- [x] 1.3 Write `tests/test_lifecycle.py`: limit add, crossing limit,
      partial fill remainder, market order, cancel, modify (qty and price),
      asserting the spec's invariants (0 <= fulfilled <= qty everywhere;
      per-trade quantity equal on both sides)
- [x] 1.4 Run `uvx pytest@<latest-stable> -q -o addopts= tests` and confirm the
      regression tests fail against unfixed code (red), lifecycle tests
      pass where current behavior is correct

## 2. Fixes

- [x] 2.1 `src/create_lob.sql` `trade_insert` trigger: change the fulfillment
      arm's predicate from `idNum in (new.bid_order, new.ask_order)` to
      `order_id in (new.bid_order, new.ask_order)`
- [x] 2.2 `src/PyLOB/orderbook.py`: add `fulfilled=fulfilled` to
      `modifyOrder`'s `orderUpdate.update(...)`; change `processMatchesDB` to
      `qtyToExec = quote["qty"] - quote.get("fulfilled", 0)`
- [x] 2.3 Drop the `trade_delete` trigger from `src/create_lob.sql` (never
      runnable: `new.*` in a DELETE trigger; retired by ADR-0001)
- [x] 2.4 Regenerate `src/lob.db` schema-only from the fixed
      `src/create_lob.sql` (no seed rows), replacing the committed file

## 3. Green + guardrails

- [x] 3.1 Run the full suite; all tests green, including both regressions
- [x] 3.2 Run `./verify`; format, lint, smoke all pass unchanged
- [x] 3.3 **Ask the maintainer** (amendment rule): add
      `stage "test" uvx pytest@<pin> -q -o addopts= tests` to `./verify`?
      Apply only on explicit yes; re-run `./verify` and report wall-clock
      against the 60s budget
- [~] 3.4 Post a comment on GitHub issue #8 only if the maintainer asks;
      otherwise note in the handoff that the fix addresses it

---

Reconciled at archive time, 2026-08-12. Beads were the execution source of
truth (`lob-6k4.1` .. `lob-6k4.13`); these boxes are checked from their
closure, not the other way round.

3.4 is marked `[~]`, not done: no comment was posted on issue #8. The task
makes it conditional on the maintainer asking, and it was not asked for. A
ready-to-post draft is in `HANDOFF.md`.

3.3 was approved and applied — `./verify` gained a `test` stage running
`uvx pytest@8.3.5 -q -o addopts= tests`, and still reports under 1s against
the 60s budget.
