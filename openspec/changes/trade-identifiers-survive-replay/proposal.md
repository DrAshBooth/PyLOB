# Proposal: trade-identifiers-survive-replay

## Why

No requirement in any spec mentions trade identifiers. The concept is
nevertheless everywhere:

- `Trade.trade_id` is the **first field** of a public `NamedTuple` returned from
  `submit` and `modifyOrder`, so every caller receives it, and ADR-0004 makes
  field order part of the public surface.
- `_next_trade_id` is one counter on `OrderBook`, and `_execute` takes from it
  *outside* the `if self.recording:` gate, so a sinkless engine numbers its
  trades exactly as a recorded one does.
- `Filled.trade_id` carries it into the stream, and the SQLite sink makes
  `trade.trade_id` an `INTEGER PRIMARY KEY` — the column that makes `trade_leg`
  the *per-trade* attribution `recording-sink` requires ("for every recorded
  trade, the individual balance movements that trade caused").

This is a **gap, not an ambiguity**: there is no text saying the wrong thing,
so the honest first question is whether anything should be ratified at all. An
unratified implementation detail is not a defect, and "this exists and no spec
tests it" is not an argument for a contract.

Probed on today's code:

```
ids across two instruments of one engine:  FAKE 1, OTHER 2, FAKE 3  (one space, interleaved)
a fresh second engine's first trade:       1
one submission sweeping three levels:      1, 2, 3   (one per execution, dense)
a 208-trade sinkless run vs the same run
  re-executed with a SQLiteSink attached:  identical trade ids
that recording read back off disk and
  replayed into a fresh engine:            identical trade ids, tuple for tuple
a replay of that replay:                   identical again
the harness's reopen():                    identical
counter after replay of the 208 trades:    209, as in the original session
a forced collision, sink attached:         UNIQUE constraint failed: trade.trade_id
                                           engine made 2 trades, 1 recorded,
                                           1 row in event_loss, close() raised
```

**The replay result is the one that earns a requirement.** `replay()` re-matches
rather than restoring — `Filled` is engine *output* and is filtered out, so
nothing reads a recorded `trade_id` back — and yet every re-derived execution
comes out with the identifier the recording gave it. That makes a replayed run
joinable, on `trade_id`, against the `trade` table it was built from, which is
the natural move for a researcher who kept one recording out of a sweep and now
wants more detail than they recorded. If the ids drifted, that join would return
wrong rows and raise nothing.

Two things defend the property today, and neither is a promise:

- `tests/test_replay.py` asserts it, but only because `trade_log()` happens to
  list `trade_id` among the fields it compares. Nothing in that helper says the
  identifier is load-bearing rather than incidental, so a later tidy-up of the
  comparison tuple removes the only test of a caller-visible guarantee without
  anyone believing they changed a promise.
- `src/PyLOB/events.py` already *states* the promise — "the next trade
  identifier `max(trade_id) + 1`, so an order submitted after a reload cannot
  collide with one from before it" — and cites `order-lifecycle`'s identifier
  clause for its authority. That clause is about **order** identifiers and says
  nothing about trades. The documentation route has already been tried here, and
  what it produced was a promise leaning on a requirement that does not cover
  it. (The sentence is also wrong about the mechanism: nothing computes
  `max(trade_id)`; the counter arrives at `N + 1` because the replay re-derives
  all N executions.)

So: one property is worth ratifying, one is its precondition, and everything
else about trade identifiers is left free on purpose.

## What Changes

- `order-matching` gains one requirement, **"Trade identifiers are unique and
  reproducible"**, with two clauses that bind:
  - each execution carries an identifier that names it and no other, across
    every instrument the engine holds, for that engine's lifetime, and is
    reported to the caller with no sink attached;
  - replaying a recorded session assigns each re-derived execution the
    identifier the original assigned to the corresponding one.
- A third, non-normative paragraph says what is **not** promised — density, any
  ordering, any relation to order identifiers, priority stamps or event sequence
  numbers, and any meaning across two engines — so that a later reader cannot
  read those into "unique".
- Two scenarios, one per binding clause. Both are properties that hold today.
- Two citations in `src/` are corrected: `events.py`'s "Replay" section, which
  promises this while citing the wrong requirement and describing a restore step
  that does not exist, and `engine.py`'s `next_trade_id`, which is where a
  reader meets the counter.
- `tests/test_sink_projections.py` reads the trade table `ORDER BY trade_id`,
  which is a reliance on an ordering this change deliberately does not ratify;
  it becomes `ORDER BY seq`, the key `recording-sink` already guarantees is
  monotonically increasing.

**No behaviour changes.** The probe above is of today's code. What changes is
that a caller may now rely on it, and that a future change to how trades are
numbered has to come back through a spec.

Deliberately not ratified, though all four are true today: that identifiers are
dense (1, 2, 3, … with no gaps), that they increase along the stream, that they
start at 1, and that they are integers. `design.md` says why each is left free.

## Capabilities

### New Capabilities

<!-- none -->

### Modified Capabilities

- `order-matching`: gains the promise attached to the identifier matching
  assigns each execution. Today the capability governs what an execution does to
  the two orders' accounting and says nothing about how the execution is named.

## Impact

- `openspec/specs/order-matching/spec.md` — one ADDED requirement
- `tests/test_replay.py` — the replay scenario, asserted explicitly and cited,
  rather than surviving inside a comparison tuple's field list
- `tests/test_engine_bookkeeping.py` — the uniqueness scenario, beside
  `test_priority_is_a_strictly_increasing_arrival_stamp`, which is the same kind
  of test about the counter next door
- `src/PyLOB/events.py`, `src/PyLOB/engine.py` — comments only
- `tests/test_sink_projections.py` — one `ORDER BY`
- **No delta for `recording-sink`**, and none for `order-lifecycle`; `design.md`
  decision 1 says why neither owns this
- **No change to `tests/harness/surface.py` or `tests/reference/matcher.py`**:
  the neutral adapter surface's `Trade` carries no identifier and the reference
  matcher's `RefTrade` has none, and this change does not give them one
  (decision 4)
- Constraints respected: no public API change (no ADR needed); no runtime
  dependency; nothing crosses instruments; matching stays in-memory; no SQL on
  the matching path
