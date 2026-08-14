# Proposal: sink-records-queue-order

## Why

`identifiers-unique-per-engine` (design.md, decision 5) left one question open
and named it: the engine's `priority` counter is engine-wide and interleaved
across instruments, `order-lifecycle`'s "Price-time priority is deterministic"
never says which, and — unlike identifiers — both readings match identically,
because matching only ever compares priority inside one instrument's book. It
concluded that the live question was not the counter's scope but whether the
one place the raw number escapes is a contract: the sink's `orders.priority`
column and the `resting_order` view over it.

Probed against the shipped engine and sink, the number is a poor thing to
promise and the *ordering* it induces is load-bearing.

**The number promises nothing worth having.** In a 120-operation session over
two instruments, the values surviving in `orders` were 1..100 with six holes
(5, 10, 17, 19, 60, 67) — every hole an order that a repricing modify
re-stamped, its old value orphaned. The resting subset is sparser still
(15, 23, 24, 25, 28, 34, …). Market orders that never rest consume values and
keep them. Two independent engines both start at 1. And `ORDER BY priority`
is *not* arrival order: in the same session 59 of 94 orders sat somewhere else
than their acceptance order put them, because every re-priced order carries a
later value than orders accepted after it.

**The ordering is the only route there is.** `book-queries` requires a snapshot
to be "ordered by matching priority", and the sink exists so that a researcher
gets that from SQL rather than by folding the log — the README hands them
`select * from resting_order` and the module docstring says the projections
turn "reconstruct the book from 200k JSON rows" into exactly that query. Read
best price first and by `priority` within a price, `resting_order` reproduces
`snapshot()` order for order. Nothing else recorded does:

```
two orders rest at 100.0; the first is repriced away and back
engine snapshot (queue order):                 [B, A]
ORDER BY price DESC, priority       -> [B, A]  matches
ORDER BY price DESC, accepted_seq   -> [A, B]  wrong: A arrived first
ORDER BY price DESC, last_seq       -> [B, A]  matches here, but:

one order's quantity is decreased passively, which keeps its place
engine snapshot (queue order):                 [A, B]
ORDER BY price DESC, priority       -> [A, B]  matches
ORDER BY price DESC, last_seq       -> [B, A]  wrong: a passive modify
                                               moves last_seq, not the queue
```

So the column carries a promise a researcher will rely on and that no
requirement makes. `tests/test_sink_projections.py::assert_resting_orders_match`
already asserts it — the same shape `sink-re-entrancy-contract` found, where
the tests existed and the contract did not. Today, dropping the column,
renumbering it densely, or moving to per-instrument counters would be a
tidy-up that leaves `./verify` green and silently breaks every study that
reads queue position out of a recording.

**The proposal is to ratify the ordering and to state plainly that the number
is not a contract** — which answers decision 5's question and, as a
side-effect, makes the counter's scope a non-question: a promise scoped to one
instrument and one side is satisfied by an engine-wide counter and by a
per-instrument one alike, so nothing here has to decide between them and
nothing later is blocked by them.

## What Changes

- `recording-sink` gains one requirement: a recording preserves the queue order
  of the orders still resting, readable per instrument and side as best price
  first then the recorded ordering value; the values are distinct so the
  ordering is total; and the value itself promises nothing — not contiguity,
  not density, not a starting point, not comparability across instruments, not
  arrival order, and nothing at all for an order that is not resting.
- No code changes. The engine and the sink already behave this way, and
  `tests/test_sink_projections.py` already checks the ordering property against
  the engine that produced it.

Not breaking, in shape or in behaviour: no schema change, no signature change,
no new refusal. What changes is what a future change has to argue against.

## Capabilities

### New Capabilities

<!-- none -->

### Modified Capabilities

- `recording-sink`: gains what the recorded projections promise about the order
  of resting orders. Today the capability requires "order history … queryable
  by SQL" and requires the *stream* to be sufficient to reconstruct the book,
  and says nothing about whether the SQL route preserves the one property that
  makes a book a book.

## Impact

- `openspec/specs/recording-sink/spec.md` — one ADDED requirement
- `tests/acceptance/test_recording_sink.py` — does not exist; `recording-sink`
  is exercised by `tests/test_sink_projections.py`,
  `tests/test_sink_equality.py`, `tests/test_sink_durability.py` and
  `tests/test_replay.py`. Two of the three scenarios are already covered
  there; see tasks.
- `src/PyLOB/*` — unchanged. `sinks/sqlite.py`'s `orders` and `resting_order`
  comments gain a citation, which is the only edit to `src/` this change
  permits.
- Constraints respected: public API shape unchanged (no ADR needed); no runtime
  dependency; matching stays in-memory; sinks stay optional and off the hot
  path; the schema is untouched, so ADR-0007's reader window is unaffected.

## Out of Scope

- **The counter's scope.** Whether `priority` is one series per engine or one
  per instrument stays undecided, deliberately. Ratifying the ordering makes
  the question moot for readers, which is the smallest answer available to
  decision 5's question and the one that forecloses least.
- **`order-lifecycle`'s "Price-time priority is deterministic".** It could gain
  the scope in its own words. It is a second frozen capability edited on the
  back of a decision about recordings, and it does not need it: nothing there
  is ambiguous about *behaviour*, only about a number it never promised to
  expose (`design.md`, decision 4).
- **`Accepted.priority` and `Modified.priority` in the event stream.** The same
  number, reachable by anyone reading the log. Saying anything about it means
  saying what the event *fields* promise, which no requirement does for any
  event, and it puts a `STREAM_VERSION` question on the table
  (`design.md`, decision 5).
- **Trade identifiers**, the third gap `identifiers-unique-per-engine` reported
  and did not close. Untouched here.
