# Proposal: sink-re-entrancy-contract

> **Approved by the maintainer, 2026-08-14.** Accepted as proposed,
> including the non-enforcement clause — a future re-entrancy guard is
> therefore a spec change, not a tidy-up. Not yet converted into beads.

## Why

`EventSink.consume` is called synchronously from inside the operation being
recorded, so a sink is the one observer that can catch the engine mid-update.
Two things follow, both reproduced against the shipped engine while
implementing `lob-k3h`.

A sink that **reads** the book gets an answer `book-queries` forbids. A match
walk lifts the level it is working off the best-price heap and leaves it in the
book, so from inside `consume`:

```
getBestAsk("FAKE")               -> None
getWorstAsk("FAKE")              -> 100.0
snapshot("FAKE", "ask")          -> one resting order at 100.0
getVolumeAtPrice("FAKE","ask",100.0) -> 2
```

`book-queries` requires a price to read `None` "only when that side of the book
is empty" and requires the snapshot to "agree with the price and volume queries
taken at the same moment". The same four queries agree the instant the
submission returns.

A sink that **writes** corrupts the walk. `match` steps over the taker's own
resting orders and counts them, because a fill invalidates its cursor over the
level's dict and the replacement is walked back over the skipped prefix. That
walk-back is sound only because a skipped order stays put. A sink cancelling
one from inside `consume` shortens the prefix, the replacement cursor lands one
order too far, and a maker the taker was entitled to trade with is never
offered. The taker rests against it: a book crossed between two traders,
permanently, with every event describing a session in which nothing went wrong.

Neither state is reachable through any public call, and neither is detected.
The prohibition that prevents both — a sink neither reads the engine nor calls
back into it — was prose in `src/PyLOB/events.py` with nothing behind it.
`lob-k3h` has since written it out properly, in `events.EventSink`,
`engine.OrderBook`, `engine.Order` and `engine.Order.resting`, and pinned both
failures in `tests/test_engine_boundaries.py`. That is documentation. Whether
the engine should *enforce* it is a decision no spec has taken, and this change
is where it gets taken.

**The proposal is to ratify the prohibition and the non-enforcement, and not to
add a guard.** `design.md` records the two enforcement options and why each was
rejected. The short form: a guard that raises fires from the middle of a match
walk, which converts the mildest failure (a stale read) into the worst one (an
aborted submission with fills already settled), and the only enforcement that
is actually safe — buffering emissions to walk exit — changes when every sink
sees every event.

## What Changes

- `recording-sink` gains one requirement stating that a sink observes and does
  not participate: `consume` runs inside the operation, a sink SHALL NOT call
  into the engine from it, and the engine SHALL NOT detect or refuse a sink
  that does. The values such a sink reads and the book states it produces are
  outside `book-queries`' guarantees, which hold for state observed between
  operations.
- No code changes. `src/PyLOB/events.py` and `src/PyLOB/engine.py` already say
  all of this in their docstrings (`lob-k3h`), and
  `tests/test_engine_boundaries.py` already pins both failure modes. What is
  missing is that none of it is ratified, so an agent reading only
  `openspec/specs/` finds `book-queries` promising something the engine does
  not deliver to a sink, and finds nothing telling it that adding the guard
  would be a contract change.

Not breaking, in shape or in behaviour: no signature changes, no new refusal,
no new exception type. What changes is what a future change has to argue
against.

## Capabilities

### New Capabilities

<!-- none -->

### Modified Capabilities

- `recording-sink`: gains the sink's own obligation, and the statement that the
  engine does not enforce it. Today the capability constrains the engine's
  emissions and says nothing about what a sink may do while consuming them.

## Impact

- `openspec/specs/recording-sink/spec.md` — one ADDED requirement
- `tests/acceptance/test_recording_sink.py` — does not exist; `recording-sink`
  is exercised by `tests/test_sink_equality.py`,
  `tests/test_sink_projections.py`, `tests/test_sink_durability.py` and
  `tests/test_replay.py`. The scenarios below are already covered by
  `tests/test_engine_boundaries.py`, section `lob-k3h`; see tasks.
- `src/PyLOB/*` — unchanged
- Constraints respected: public API shape unchanged (no ADR needed); no
  runtime dependency; matching stays in-memory; sinks stay optional and off
  the hot path.

## Out of Scope

- **Enforcement.** Not proposed, and `design.md` says why. If the maintainer
  wants it, it is a further change with a further delta — which is exactly what
  ratifying the non-enforcement buys.
- **Scoping `book-queries` itself.** Its snapshot requirement could gain
  "taken between operations" rather than being scoped by reference from here.
  That is a second delta against a second frozen capability, taken on the back
  of a decision about sinks, and it is left to the maintainer as its own
  change (`design.md`, decision 3).
- **`Order` immutability** (`lob-k3h` (b)) and the aftermath of a sink that
  raises (`lob-k3h` (c)). Both are documented, both are separate decisions, and
  neither is ratified here. Stating (c) would ratify a torn engine state that
  nobody has examined.
