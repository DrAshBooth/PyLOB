# Handoff: fix-fulfilled-accounting

Both bugs reported in [issue #8](https://github.com/DrAshBooth/PyLOB/issues/8)
are fixed, with regression tests that fail against the old code and pass
against the new. `./verify` gates them from now on.

**No comment has been posted on issue #8.** Task 3.4 makes that conditional on
the maintainer asking, and it has not been asked for. The draft below is ready
if wanted. Issue-hygiene beads under `rewrite-docs` (`lob-968.6`, `lob-968.7`)
carry their own maintainer gate for anything posted publicly.

## What changed

| Commit | Change |
| --- | --- |
| `52ed456` | `tests/conftest.py` — fresh-DB `OrderBook` fixtures, the repo's first test scaffolding |
| `306dfe5` | `tests/test_issue8_regressions.py` — both repros, red against unfixed code |
| `0469e38` | `tests/test_lifecycle.py` — 26 cases, green before and after, the regression safety net |
| `8b0c61c` | Finding 2 — `modifyOrder` / `processMatchesDB` |
| `489c360` | Finding 1 — `trade_insert` predicate; dead `trade_delete` dropped; `src/lob.db` regenerated |
| `5db0405` | `./verify` gains a `test` stage (maintainer-approved) |

**Finding 1.** `trade_insert`'s fulfillment arm keyed on `idNum`, but
`trade.bid_order` / `trade.ask_order` are foreign keys into
`trade_order(order_id)` and `processMatchesDB` only ever writes `order_id`
values there. The credit landed on whichever unrelated orders happened to
carry those numbers as their `idNum` — or on nobody, leaving a fully traded
order still advertising its whole size in `best_quotes`. Isolated, this
one-line fix takes the suite from 6 failures to 4.

**Finding 2.** `modifyOrder` unpacked `fulfilled` but never put it back into
the dict it handed to `processMatchesDB`, which sized its cross from the raw
requested `qty`. Both halves of the fix are required; either alone leaves the
tests red. The `.get("fulfilled", 0)` default keeps the fresh-order
`processOrder` path byte-identical in behavior.

**Also retired.** `trade_delete` referenced `new.*` inside a DELETE trigger,
where only `old.*` is bound, so it could never have run. Dropped per ADR-0001
rather than repaired. Worth noting it was the one place already using the
correct `order_id` predicate — `trade_insert` was fixed on its own merits, not
by copying it.

## Evidence

The reporter's measured numbers are reproduced exactly by the pre-fix test
run, and all of them invert post-fix:

| | Unfixed | Fixed |
| --- | --- | --- |
| Traded against a 10-share resting bid | 12 | 10 |
| Reprice cross executes | 10 | 6 |
| Repriced order ends | `qty=10 fulfilled=14` | `qty=10 fulfilled=10` |
| Counterparty retains | 10 of 20 | 14 of 20 |
| Later unrelated 12-lot bid fills | 10 of 12 | 12 of 12 |

Suite: 33 passed. All 26 lifecycle tests stayed green throughout, so neither
fix regressed working behavior.

## Not fixed here — filed separately

Writing the lifecycle tests surfaced five further divergences from the frozen
contracts. None is fixed in the legacy engine: ADR-0001 replaces it with the
in-memory core, so a legacy fix is throwaway work. They are pinned as strict
`engine_xfail` cases in the acceptance suites instead, which makes them
implementation-blocking for `inmemory-engine` and turns the run red if one is
ever silently fixed without removing the marker.

| Bead | Divergence |
| --- | --- |
| `lob-0bl` | A market order's remainder rests in the book with a null price, sorts *best*, and later trades at the taker's price — a 101-priced book filling at 200 |
| `lob-crf` | `modifyOrder` with `price=None` matches as a market order; a bid limited at 99 buys at 105 |
| `lob-ihv` | Invalid submissions call `sys.exit`; `SystemExit` escapes a caller's `except Exception` |
| `lob-0rb` | Cancel/modify of an unknown identifier, or with the wrong side, silently no-ops |
| `lob-pn3` | A price change on modify does not surrender time priority — an order can improve and return for free |

`lob-0bl` needs a maintainer decision rather than a fix: `src/example.py`
documents the resting behavior as intended, so honoring the IOC spec means
either changing the engine or amending a frozen spec via a new delta.

## Draft issue #8 comment (not posted)

> Both findings are fixed on `master`.
>
> Finding 1 — `trade_insert`'s fulfillment arm now keys on `order_id`
> (`489c360`). Finding 2 — `modifyOrder` now passes `fulfilled` through, and
> `processMatchesDB` sizes its cross as `qty - fulfilled` (`8b0c61c`); both
> halves are needed.
>
> Your two repro scripts are now regression tests in `tests/`, asserting the
> post-fix numbers you gave. They failed against the pinned commit with
> exactly the values you measured — 12 traded against a 10-share order, a
> reprice executing 10 instead of 6, an order at `fulfilled=14` on `qty=10`,
> and the follow-up 12-lot bid under-filling by 2 — and pass now. `./verify`
> runs them, so neither bug can come back quietly.
>
> The dead `trade_delete` trigger you spotted as the copy/paste counterpart
> has been dropped rather than repaired: it references `new.*` in a DELETE
> trigger, so it could never have run.
>
> Thank you for the report — the repro quality made this quick to confirm.
