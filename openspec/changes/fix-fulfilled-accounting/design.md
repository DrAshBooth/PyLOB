# Design: fix-fulfilled-accounting

## Context

See proposal.md for motivation. Constraints that shape the approach:

- `trade.bid_order` / `trade.ask_order` receive `quote["order_id"]`
  (`crsr.lastrowid`) from `OrderBook.processMatchesDB`; all four
  trader-balance arms of the `trade_insert` trigger already join
  `trade_order.order_id = new.bid_order/new.ask_order`. Only the fulfillment
  arm matches on `idNum` — it is inconsistent with the trigger's own convention.
- `modifyOrder` re-reads the order row (including `fulfilled`) before its
  reprice-cross, so the true remainder is already available at the call site.
- No packaging exists yet (no `pyproject.toml`); tests must run without an
  installed package. `setup.cfg` carries PyScaffold-era pytest `addopts` with
  `--cov`, which errors unless `pytest-cov` is present.
- `./verify` may not gain a stage without maintainer approval (amendment rule).
- ADR-0001: matching moves in-memory; the SQL engine is fixed in place here to
  serve as the transition's cross-check oracle, not extended.

## Goals / Non-Goals

**Goals:**
- Both accounting bugs fixed at their root, each covered by a regression test
  encoding issue #8's expected numbers.
- A lifecycle test suite that later changes (benchmark trial, rewrite) can
  lean on as the correctness oracle.

**Non-Goals:**
- No public API change; no schema redesign beyond the one trigger predicate.
- No packaging migration (pyproject/uv project setup is its own change).
- No coverage tooling; no benchmark work; no docs rewrite.

## Decisions

1. **Trigger fix: match on `order_id`.**
   `where idNum in (new.bid_order, new.ask_order)` becomes
   `where order_id in (new.bid_order, new.ask_order)`.
   *Alternative rejected:* store `idNum` values into `trade.bid_order/ask_order`
   instead — would require changing `processMatchesDB` and every balance arm
   of the trigger that already keys on `order_id`; strictly more churn.

2. **Python fix: subtract prior fills in `processMatchesDB` (issue #8's
   verified patch).** `modifyOrder` adds `fulfilled=fulfilled` to
   `orderUpdate.update(...)`; `processMatchesDB` uses
   `qtyToExec = quote["qty"] - quote.get("fulfilled", 0)`.
   Fresh orders carry no `fulfilled` key, so that path is untouched.
   *Alternative rejected:* clamp inside the SQL `matches` query — spreads
   lifecycle state into the query layer and changes the on-trial SQL surface.

3. **Drop the `trade_delete` trigger entirely.** It has never executed
   successfully (`new.qty` in a DELETE trigger; confirmed by review probe E1),
   and ADR-0001 forecloses SQL-side matching logic, so a fill-reversal
   trigger has no future. Deleting a `trade` row was never a supported
   operation; removing the trigger makes that explicit instead of erroring.
   *Alternative rejected:* rewrite it with `old.*` and fix its aliasing bug —
   effort spent on a path ADR-0001 retires.

4. **Regenerate the committed `src/lob.db` from the fixed schema (schema-only,
   no seed rows).** The committed DB embeds the buggy trigger; leaving it means
   anyone using the shipped DB resurrects bug 1. `example.py` seeds its own
   traders/instrument, and the `./verify` smoke stage rebuilds from
   `create_lob.sql` anyway, so an empty schema-only DB loses nothing.
   *Alternative rejected:* keep the old DB as historical data — a tracked
   artifact that silently contradicts the tracked schema.

5. **Tests run via `uvx` with pinned pytest, no install step.**
   `tests/conftest.py` prepends `src/` to `sys.path`; each test builds a fresh
   DB from `src/create_lob.sql` in `tmp_path` (the proven smoke-stage pattern).
   Invoke as `uvx pytest@<pin> -q -o addopts= tests` — `-o addopts=` overrides
   setup.cfg's stale `--cov` options instead of dragging in `pytest-cov`.
   *Alternative rejected:* editable install / uv project — belongs to the
   modernization change; doing it here couples two changes.

6. **`./verify` gains `stage "test" ...` only on explicit approval.** The
   proposal flags this decision point; tasks make it its own step so approval
   is visible in the flow, per the amendment rule in CLAUDE.md.

## Risks / Trade-offs

- [Trigger fix shifts which rows accrue fills; anything that depended on the
  buggy attribution changes behavior] → the lifecycle suite plus example.py
  smoke run guard the intended paths; expected numbers come from issue #8's
  verified outputs, not from current behavior.
- [Regenerating `src/lob.db` discards its 10 historical trades] → they are
  reproducible demo output, and the old file remains in git history.
- [`-o addopts=` silently disables future intentional addopts] → temporary;
  the modernization change owns pytest config and removes the override.
- [Issue #8's bug-1 repro depends on forcing idNum/order_id divergence; if its
  exact setup proves version-sensitive, the test may need adaptation] → assert
  on invariants (fills credited to participating orders only) rather than on
  incidental row layout.

## Open Questions

- None blocking. The pytest version pin is chosen at implementation time
  (latest stable at time of writing the task).
