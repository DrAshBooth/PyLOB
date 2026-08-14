# Design: sink-records-queue-order

## Context

See `proposal.md` for the question and the probes. What shapes the approach:

- `priority` is one counter on `OrderBook` (`_next_priority`), stamped in
  `create_order` and re-stamped by a non-passive `modifyOrder`. It is the only
  tie-break in matching: orders sort by `(price, priority)`. Probed, it
  interleaves across instruments (FAKE 1, OTHER 2, FAKE 3, OTHER 4), it is
  consumed by market orders that never rest, it is never reissued after a
  cancel, and two fresh engines both start at 1.
- The value reaches a reader in three places: `Order.priority` on the engine's
  own objects, `Accepted.priority` / `Modified.priority` in the stream, and
  `orders.priority` in the sink — the last being the one a researcher meets,
  because the README's recipe for a finished run is
  `select * from resting_order`.
- The ratified text mentions priority three times and never as a number:
  `order-lifecycle` requires "a total order (arrival sequence number)" as the
  tie-break, requires a non-passive modify to move an order "to the back of its
  price level's queue", and `book-queries` requires a snapshot to list each
  resting order's "priority position", "ordered by matching priority". Position,
  queue, order — never a value to read off.
- The acceptance surface follows suit. No test under `tests/acceptance/` reads
  `.priority` as a number; the adapter's `snapshot` returns orders whose tuple
  position *is* the answer.
- `recording-sink` requires "(a) order history … queryable by SQL after the
  session" and "(b) the persisted stream is sufficient to reconstruct the book
  state … at end of session". (b) is about the *stream*, and replay satisfies
  it in Python. Nothing joins the two: no requirement says the SQL projections
  preserve the book's order.
- `openspec/specs/` is frozen. Wording lands as a delta first.

## Goals / Non-Goals

**Goals:**

- The property a recording's reader actually depends on becomes a contract, in
  the capability that owns recordings.
- The *non*-promises become a contract too, so that renumbering, re-scoping or
  dropping the value is a change someone has to argue for rather than a tidy-up
  someone can land — and so that a reader who mistakes it for arrival order is
  contradicted by the spec rather than only by a careful reading of the engine.
- Decision 5 of `identifiers-unique-per-engine` gets an answer rather than a
  second deferral.

**Non-Goals:**

- No behaviour change, no schema change, and no test that would fail today. If
  a task turns out to need a code change, that is a bug this change found and
  it is filed rather than folded in.
- No decision about the counter's scope. Decision 2.
- Nothing about the other two gaps decision 5 reported (trade identifiers) or
  raised (`book-queries`' loose wording, which
  `book-queries-name-the-instrument` has since taken).

## Decisions

1. **The ordering is ratified; the number is not.** The two halves are one
   decision, and stating only the first would be worse than stating neither.
   "The sink records a priority for each order" invites exactly the readings
   the probes falsify — that the values count something, that they are
   comparable across instruments, that sorting a recording by them recovers the
   order in which the engine saw the orders. Probed, in one 120-operation
   session: the values in `orders` ran 1..100 with six holes, each hole a value
   orphaned by a repricing modify; the resting subset was sparser again
   (15, 23, 24, 25, 28, …); and sorting the table by the value put 59 of 94
   orders somewhere other than their acceptance order.

   The ordering, by contrast, is exact and uniquely available. `ORDER BY price,
   priority` within one instrument and side reproduced `snapshot()` order for
   order across a randomised session, and the two candidate substitutes each
   fail on a minimal case: `accepted_seq` on an order repriced back onto a
   level that filled up behind it, `last_seq` on a passive quantity decrease,
   which keeps the order's place and still writes a row. A fill moves
   `last_seq` too. So the column is not a convenience over some other column;
   it is the queue.

   *Alternative rejected: ratify nothing, and record the decision in an ADR.*
   This was the live alternative — the brief for this change said so — and it
   is a real position: the value is schema, the schema documents itself in its
   own `CREATE` statements, ADR-0007 already governs how readers survive a
   version bump, and a researcher who wants guaranteed queue order can replay
   the log and call `snapshot()`. It loses on the last point. Replaying is the
   thing the projections exist to spare the reader — the sink's own docstring
   says the projections turn "reconstruct the book from 200k JSON rows" into
   `SELECT * FROM resting_order`, and the README hands a pandas user exactly
   that line. A recording whose SQL layer answers "which orders rest" but not
   "in what order" delivers half of its stated purpose by contract and the
   other half by luck. An ADR would record that we looked; it would not stop
   the next schema change from taking the property away, because ADRs bind
   decisions and `./verify` reads specs.

   *Alternative rejected: ratify the value's replay-stability as well.* Probed,
   it holds exactly: replaying a 94-order session reproduced every stamp
   identically, because allocation is deterministic over the command stream and
   the stream is the replay input. It is also nothing a reader needs — what
   they compare across a reload is the book, not the stamps — and promising it
   would forbid a future engine from renumbering on reload for no benefit. The
   scenario keeps the reload case at the level that matters: the two recordings
   read back in the same order.

2. **The counter's scope stays undecided, and this is what closes the
   question.** `identifiers-unique-per-engine` framed decision 5 as "is the
   column a contract at all?", having established that the scope makes no
   behavioural difference. Answering that question at the level of the
   *ordering* dissolves the scope question rather than deferring it again: a
   promise about resting orders within one instrument and one side is satisfied
   by an engine-wide counter and by a per-instrument one alike, so an engine
   that later shards per instrument breaks no recording's reader, and one that
   keeps a single counter breaks none either. The requirement says so in the
   text, in the place a reader is already looking, so the next person to notice
   the interleaving finds the answer instead of re-opening it.

   *Alternative rejected: ratify the engine-wide scope, mirroring the
   identifiers decision.* Identifiers had to be decided because they are
   *addressed*: `cancelOrder(idNum)` takes no instrument, the sink's `orders`
   primary key is global, and per-instrument identifiers would need a composite
   key and a schema migration. Priority is addressed by nobody. No public call
   takes one, no key is built from one, and nothing outside the engine's own
   sort reads two of them together. Deciding it would be taking a position on
   an implementation detail because it happened to be visible, which is the
   thing this repository's specs otherwise avoid — `identifiers-unique-per-
   engine`'s own decision 1 makes the opposite call for identifiers precisely
   because there the scope escapes into the API.

3. **`recording-sink` gains an ADDED requirement, not a MODIFIED one.** The
   requirement that owns SQL-queryability — "The SQLite sink persists
   replayable, queryable history" — enumerates what is queryable (order
   history, trade history, balances, commissions) and what the stream suffices
   to reconstruct. It is not ambiguous about queue order; it is silent, and its
   list does not read as exhaustive of every property of a recording. That is
   the distinction `identifiers-unique-per-engine`'s decision 2 drew from the
   other side: MODIFY when the existing sentence *is* the thing being misread,
   ADD when there is no text. Folding this into that requirement would also
   grow the repository's densest requirement by a paragraph of non-promises
   that have nothing to do with balances or replay.

   *Alternative rejected: state it in `book-queries` instead, beside the
   snapshot ordering it mirrors.* The snapshot requirement is about the engine's
   read side, answered from live structures, and `book-queries` says nothing
   about recordings anywhere. Putting a sink obligation there splits the sink's
   contract across two capabilities to save a cross-reference.

4. **The requirement names no table, column or view.** House style in this
   capability: the metadata requirement says "persist it inside the recorded
   database, queryable by SQL … and readable back through the library" without
   naming `session_meta` or `read_meta`; the per-leg requirement says "one row
   per (trader, symbol) movement" without naming `trade_leg`. So the wording
   here is "a value that places that order in its price level's queue", read
   "best price first, and that value ascending within a price". The property is
   checkable and the sink keeps the freedom to rename. `sink-re-entrancy-
   contract` names `consume`, which is the counter-example — but `consume` is
   the `EventSink` protocol, a name the engine's callers implement, not a
   column in one sink's schema.

5. **The event stream's `priority` field is left alone.** `Accepted.priority`
   and `Modified.priority` are the same number and reach anyone reading the log
   or holding a `ListSink`. Three reasons not to reach for it here. No
   requirement in any capability says what any event's *fields* are — the
   stream's contract is stated in terms of what events exist, what they carry
   in the abstract ("both sides' identifiers, price, quantity") and what order
   they come in, and starting a field-level register on this field would be
   odd. A non-promise about the field could be read as licence to remove it,
   which is a `STREAM_VERSION` decision and a break for every recorded log. And
   the field is what the sink writes its column from, so a promise about the
   recording already reaches back to it as far as it needs to.

6. **No ADR.** The rule in `CLAUDE.md`: write one when a decision constrains a
   proposal not yet written, rejects an option that would otherwise leave no
   trace, or supersedes an ADR. This supersedes nothing — ADR-0007 is about
   which schema versions readers accept, which is orthogonal to what a column
   means, and it is not weakened or narrowed by anything here. It rejects three
   options and all three are recorded above, in the change that rejected them,
   which is the trace.

   The interesting case is the constraint test, because this change is
   deliberately a decision *not* to decide the counter's scope, and a decision
   not to decide is exactly the kind that leaves no trace in a spec. It does
   leave one here: the requirement's own text says the scope is not promised
   either way and that both allocations satisfy it. That is the constraint, in
   normative text, in the document a reader hits first — the case where an ADR
   would be a second copy of a rule that has a home.

   The one thing that would change this: if the maintainer wants the *absence*
   of a scope decision to bind work that is not a change to `recording-sink` —
   sharding the engine per instrument, or a second sink format — where the
   non-promise would be met as a licence rather than read as a requirement. No
   such work is on the table, and an ADR taken speculatively would assert a
   boundary nobody has surveyed. (Same conclusion, same reasoning, as
   `identifiers-unique-per-engine` decision 6.)

## Risks / Trade-offs

- [Ratifying the ordering makes a schema change to `orders`/`resting_order`
  more expensive] → that is the intent, and the price is one delta rather than
  a veto. The probes are in this change, so the next person argues against a
  stated position instead of rediscovering that six studies depended on a
  column nobody had promised.
- [`tests/test_sink_projections.py` asserts the recorded value *equals*
  `Order.priority` row by row, which is more than the requirement promises] →
  deliberate, and it does not contradict the non-promise. That assertion is
  projection fidelity — the sink copied what the engine had — checked inside
  one session against the engine that produced it. It is not a promise to a
  reader holding only a file, and it should not be loosened on the strength of
  this requirement: it is the assertion that would fail first if the fold
  started inventing values. A task says so.
- [A requirement whose second half is a list of things not promised reads
  oddly] → it is the honest shape, and it is the shape
  `sink-re-entrancy-contract` established in this same capability. The
  alternative is a requirement that ratifies a number by implication, which is
  the outcome this change exists to avoid.
- [Nobody has actually asked for queue position out of a recording] → queue
  position is standard LOB research, `resting_order` exposes the column for it,
  and `tests/test_sink_projections.py` was written to check exactly this
  reconstruction. The demand is evidenced by the code that already serves it.
- [Three new scenarios are up to one new test for behaviour that already works]
  → two are already covered (tasks 2.1, 2.2). The third is small, and it is the
  only artifact that would catch the ordering silently regressing to arrival
  order.

## Open Questions

- **Should `recording-sink` have an acceptance suite?**
  `sink-re-entrancy-contract` left this with the maintainer: the capability has
  no `tests/acceptance/` module and its scenarios live in the `tests/test_sink_*`
  suites, which read `SQLiteSink` directly rather than going through the
  engine-neutral adapter. This change adds three more scenarios in the same
  position and does not settle it.
- Whether `order-lifecycle`'s "Price-time priority is deterministic" should say
  in its own words that the total order is per instrument is left to the
  maintainer. Nothing here depends on it, and after this change nothing a
  reader can observe does either.
- Trade identifiers — the third gap `identifiers-unique-per-engine` reported —
  remain unspecified. Still a gap, still nobody's change.
