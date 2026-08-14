# Proposal: identifiers-unique-per-engine

> **Approved by the maintainer, 2026-08-14.** Accepted as proposed. No
> behaviour change: this ratifies the reading both implementations already
> took. Not yet converted into beads.

## Why

`order-lifecycle`'s "Order identifiers are unique and stable" opens:

> Each accepted order SHALL receive an identifier unique within the book's
> lifetime, including across reloads of persisted state. Operations addressing
> an identifier SHALL affect at most one order.

"the book" named one thing when the project's scope was one instrument. It
names two now. `openspec/config.yaml`'s scope block says one engine holds many
instruments and "a book springs into being on first mention", so *the book* is
either one instrument's book or the engine holding all of them — and the two
readings are not the same rule. Under the first, `FAKE` #1 and `OTHER` #1 may
both exist; under the second, the second submission is refused.

The engine takes the second reading. Probed on a fresh `OrderBook` holding
`FAKE` and `OTHER`:

```
ids issued across two instruments:  FAKE 1,2,3,4,5   OTHER 6,7,8,9
supplying FAKE's id 1 on OTHER:     DuplicateOrderID: idNum 1 is already in use
supplying a cancelled order's id:   DuplicateOrderID: idNum 2 is already in use
supplying a filled order's id:      DuplicateOrderID: idNum 1 is already in use
id 500 supplied on FAKE, then a bare submission on OTHER:  501
after replay of a two-instrument session, first fresh id:  5
```

Nothing here violates the ratified text — both readings satisfy it, which is
exactly the problem. Three independent artifacts picked the same reading and
none of them is entitled to:

- `OrderBook` keeps one `_orders` map and one `_next_idNum` high-water mark for
  the whole engine, and its module docstring justifies never pruning them by
  quoting the ambiguous clause back: identifiers unique *"within the book's
  lifetime"*.
- `tests/reference/matcher.py` — a second implementation that shares no code
  with the engine (ADR-0003) — keeps one `orders` dict and one `_next_idNum`
  for the same reason, citing the same clause in the same words.
- The SQLite sink makes `orders.idNum` an `INTEGER PRIMARY KEY` with
  `trade.bid_idNum`/`ask_idNum` referencing it, and inserts with a plain
  `INSERT`. `instrument` is an ordinary column, not part of the key. Per
  instrument identifiers would not merely read oddly there; the second one
  raises `UNIQUE constraint failed: orders.idNum` and the session loses events.

The reading is also the only one the API can satisfy. `cancelOrder(side, idNum)`
and `modifyOrder(idNum, orderUpdate)` name no instrument — probed, cancel reaches
an order on the engine's second instrument by identifier alone — so "operations
addressing an identifier SHALL affect at most one order" is *unsatisfiable* under
per-instrument identifiers without a signature change the public-API constraint
would need an ADR to make.

So the behaviour is right, load-bearing, and unstated. Someone implementing this
requirement from its text, or reviewing a change against it, can reach the other
reading honestly. That is what this change closes. Filed as `lob-xjc` by the
agent implementing `lob-3mo`, which stopped at the implementation boundary
rather than reading the intent into the spec.

## What Changes

- `order-lifecycle`'s "Order identifiers are unique and stable" says which
  scope: every instrument the engine holds, for that engine's lifetime.
  Per-instrument identifiers are ruled out in as many words, because "unique
  across the engine" alone still lets a reader wonder whether the engine merely
  *happens* to allocate that way.
- The same requirement's second sentence gains the clause that makes it
  satisfiable: an operation reaches its order without being told the
  instrument. This is the reason the scope has to be global, and it is what a
  reader implementing cancel from the requirement needs to know.
- "the book" leaves the requirement entirely, including the one place it was
  doing a second piece of ambiguous work: the "Externally supplied duplicate"
  scenario refused an identifier "already present in the book", which under a
  resting-only reading would permit reuse after a cancel — while the
  requirement's own *lifetime* clause forbids it and the engine (probed) rejects
  it. The scenario now says what the requirement already required.
- One clause, non-normative, on what the scope is *not*: the process. Two
  engines both issue 1 (probed), and since ADR-0006 makes an episode a fresh
  `OrderBook`, a researcher running episodes back to back is precisely the
  person who might otherwise assume otherwise.
- Four scenarios pin the rules that had no scenario: allocation across two
  instruments, a supplied duplicate crossing instruments, a finished order's
  identifier, and cancel reaching one instrument's order without naming it.

**No behaviour changes.** The engine, the reference matcher and the sink all
already do this; the probe above is of today's code. What changes is that they
are now doing it because a requirement says so. The work is the wording, the
acceptance tests those new scenarios owe the suite, and two source comments that
quote the old clause verbatim.

Deliberately not in scope: `book-queries`' two requirements that say "the book"
where their neighbours say "per instrument"; the scope of the `priority` counter
(engine-wide, probed, and unobservable through matching); and trade identifiers,
which no requirement mentions at all. `design.md` says why each is left alone.

## Capabilities

### New Capabilities

<!-- none -->

### Modified Capabilities

- `order-lifecycle`: identifier uniqueness names its scope — the engine and
  every instrument in it, for the engine's lifetime — so the requirement means
  one thing under the multi-instrument scope it is now read against.

## Impact

- `openspec/specs/order-lifecycle/spec.md` — one MODIFIED requirement
- `tests/acceptance/test_order_lifecycle.py` — one test per new scenario, the
  convention the suite follows; `engine_factory(instruments=...)` already
  exists for exactly this ("a scenario that has to distinguish 'for an
  instrument' from 'for the book'")
- `src/PyLOB/engine.py` — the "Identity" section of the module docstring quotes
  the superseded clause; comment only
- `tests/reference/matcher.py` — the `orders` field comment quotes it too;
  comment only, the allocation is already engine-wide
- No delta for `recording-sink`: its sink schema *depends* on this scope rather
  than restating it, and the dependency becomes legitimate once this is
  ratified
- Constraints respected: no public API change (no ADR needed); no runtime
  dependency; nothing crosses instruments; matching stays in-memory; no SQL on
  the matching path
