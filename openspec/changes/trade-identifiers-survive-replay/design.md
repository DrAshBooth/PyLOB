# Design: trade-identifiers-survive-replay

## Context

See `proposal.md` for the gap and the probe. What shapes the approach:

- This is an **ADDED** requirement about something nobody asked for, which is a
  weaker starting position than a clarification. `identifiers-unique-per-engine`
  (archived) had two live readings of existing text and three artifacts silently
  agreeing on one of them; here there is no text and no disagreement. The bar is
  therefore higher, and the first decision below is whether to write anything.
- The identifier exists with no sink attached. `_execute` calls
  `next_trade_id()` outside the `if self.recording:` gate, and `Trade.trade_id`
  is returned to a sinkless caller. Anything ratified has to be true of an
  engine that never records.
- `replay()` re-matches. `Filled` is not replayable, so no recorded `trade_id`
  is ever read back by the library; the identifiers a replay reports are
  arrived at again, not restored. Whether they agree with the recording is a
  real property of the engine, not a property of the file format.
- `openspec/specs/` is frozen. Wording lands as a delta first.

## Goals / Non-Goals

**Goals:**
- A caller who joins a replayed run against the recording it came from may do so
  on `trade_id`, because a requirement says the join holds.
- The engine's own documentation stops promising this on borrowed authority.
- The smallest text that covers what callers depend on, with the properties left
  free stated in the requirement rather than merely omitted from it.

**Non-Goals:**
- No behaviour change, and no test that would fail today. A task that turns out
  to need an engine change has found a bug: file it and stop.
- No statement about *how* identifiers are allocated. A counter is not the
  contract.
- No trade identifier in the engine-neutral adapter surface, and none in the
  reference matcher (decision 4).
- Nothing about the sink's schema. `trade.trade_id INTEGER PRIMARY KEY` is the
  sink's own design; it *depends* on this requirement rather than restating it,
  which is the relationship `identifiers-unique-per-engine` left `orders.idNum`
  in.

## Decisions

1. **`order-matching` owns it. Neither `recording-sink` nor `order-lifecycle`
   would.**

   The identifier is created by `_execute` — the act of matching — once per
   execution, and it names an execution. `order-matching` is the capability
   whose subject is what happens when a trade executes; it already says which
   orders a fill is credited to, and naming the fill is the same subject as
   attributing it.

   *`recording-sink` rejected.* Every requirement there is conditioned on a sink
   or on the stream: the engine emits, the sink persists, a sink observes and
   does not act. A trade identifier placed in that capability would read as a
   property of recording, and would license precisely the implementation that
   breaks the guarantee — one that allocates identifiers only when a sink is
   attached, leaving `Trade.trade_id` meaningless for the sinkless default the
   throughput baselines are measured against and the sweep workflow runs in
   (ADR-0002, superseded on the target by ADR-0005). It would also put a
   promise about a value the *engine* returns inside the one capability a user
   can decline to use. That the sink's `trade` table is the identifier's most
   visible consumer is not ownership; the sink consumes many things it does not
   define.

   *`order-lifecycle` rejected*, though it is the tempting home because its
   "Order identifiers are unique and stable" is the requirement this one is
   shaped after. Its purpose is an order's life — what submissions are accepted,
   how orders are identified, how cancel and modify behave. A trade is not an
   order; it is what two orders produce. Filing both identifier spaces under a
   capability about orders would invite exactly the conflation `events.py`
   already made when it cited the order-identifier clause to justify a trade
   identifier.

   The replay clause goes in the same requirement rather than in
   `recording-sink` beside "State reconstructs from the log". That requirement
   is about the **stream's sufficiency**; this clause is about **what the engine
   re-derives from it**, which is a determinism statement of the same kind as
   `order-lifecycle`'s "the earlier-submitted order matches first, on every
   replay". Splitting the subject across two capabilities would leave neither
   half answering "what does a trade identifier promise?" —
   `identifiers-unique-per-engine` decision 2 reached the same conclusion from
   the other direction.

2. **Ratify two clauses, and only two.** The test applied to each candidate
   property was: *does a caller depend on it, and does breaking it fail
   silently?*

   | property | today | ratified? | why |
   | --- | --- | --- | --- |
   | an execution has an identifier, distinct from every other in the engine | yes | **yes** | the sink's primary key and `trade_leg`'s per-trade attribution are wrong without it, and a collision costs a lost `Filled` and an `IntegrityError` at `close()` — a hole in history discovered late (probed) |
   | the identifier is there with no sink | yes | **yes** | it is what stops the guarantee being read as a recording artifact; `Trade.trade_id` is public and returned to a sinkless caller |
   | a replay re-derives the recorded identifiers | yes | **yes** | the join between a replayed run and its recording; breaks silently and returns wrong rows |
   | dense, no gaps | yes | no | nobody counts trades by subtracting identifiers; a future engine that allocated per instrument-and-merged, or in blocks, would still serve every consumer |
   | increasing along the stream | yes | no | `seq` is the ratified ordering key and sits in the same row of the same table; ordering by `trade_id` is a habit, not a need |
   | starts at 1 | yes | no | a starting value is worth nothing to a caller and forecloses a great deal |
   | an integer | yes | no | the sink's `INTEGER PRIMARY KEY` is the sink's constraint on itself; a different identifier type is a sink change, not a broken promise |
   | comparable across two engines | no | no | stated as a non-guarantee, matching `order-lifecycle`'s "the scope is one engine, not the process" — a researcher sweeping episodes holds several engines' identifiers at once (ADR-0006) |

   The four "no"s are written into the requirement as a non-normative paragraph
   rather than left unsaid. Silence about density is how a reader concludes that
   "unique, for the engine's lifetime" means "1, 2, 3, …" — the reading the
   engine currently satisfies, and the one a change to the engine would then be
   accused of breaking.

3. **Ratifying nothing was the serious alternative, and this is why it loses.**
   It is the cheaper option and it stays honest about the brief: an
   implementation detail with no ratified backing is not a defect, and
   `tests/test_replay.py` does assert the replay property today.

   What decides it is *what the test's authority rests on*. `trade_log()` is a
   helper that turns trades into comparable tuples, and `trade_id` is one field
   in its list. Under this repo's own triage rule — behaviour that violates a
   ratified spec is a bug; behaviour no spec covers is not — an agent whose
   change renumbered trades would find that assertion failing, find no
   requirement behind it, and be entitled to narrow the tuple to "the fields
   that matter". Nothing in the helper distinguishes the field that carries a
   caller-visible promise from the ones that are there for a better failure
   message. That is a guarantee defended by a comma.

   The second consideration is that the documentation route has already been
   run: `events.py` states the promise, in the module a sink author reads first,
   and had to borrow `order-lifecycle`'s order-identifier clause to justify it.
   Proposing "state it in the docstring instead" is proposing the thing that
   produced the miscitation.

   *Where the reasoning would have gone if this had been rejected:* not into an
   ADR — a decision not to ratify constrains nothing — but into this design.md,
   as `identifiers-unique-per-engine` decision 5 did when it declined this same
   gap. That is why the archived change already reads "a capability question
   nobody has asked": the trace exists, and re-opening it a third time is what
   this change ends either way.

4. **The engine-neutral surface and the reference matcher are left alone,
   deliberately.** `tests/harness/surface.py`'s `Trade` is `(bid, ask, price,
   qty)` and `tests/reference/matcher.py`'s `RefTrade` has no identifier at all.
   Widening the surface would make `tests/test_differential.py` compare trade
   identifiers between the two implementations, which means giving the reference
   matcher a counter that exists only to be compared against the engine's.

   The precedent is `seq`: `recording-sink` ratifies a monotonically increasing
   sequence number, the reference matcher does not model it, and nobody
   considers that a hole. ADR-0003 makes the reference matcher a *spec-derived
   matcher* — it models the book and the money, not the recording surface. A
   trade identifier is closer to `seq` than to `fulfilled`: it is a name for an
   event, and the differential oracle exists to catch disagreements about
   matching and accounting, which is where its five surviving mutants lived.

   The cost is real and is named in the risks: the new requirement has no
   independent implementation checking it. It is accepted because the property
   is one counter and one increment in each implementation, so a differential
   comparison would be checking that two trivial counters count.

5. **The replay clause is about replay, not about determinism in general.** The
   general form — "two engines given the same commands assign the same
   identifiers" — is also true today and is strictly stronger. It is not
   ratified because no caller needs it: the join that matters is between a
   replay and the recording it was built from, and that is what the narrow form
   says. The narrow form is also the one whose failure a reader can picture.

6. **`tests/test_sink_projections.py` stops ordering by `trade_id`.** Its
   `assert_trades_match` reads the `trade` table `ORDER BY trade_id` and compares
   it to the engine's trade list in order — which relies on identifiers
   increasing along the stream, exactly the property decision 2 declines to
   ratify. Leaving it would mean this change ratified a narrow contract and left
   a test asserting a wider one, which is the situation it exists to end.
   `ORDER BY seq` is the ratified key and is already in the row.

7. **No ADR.** The rule in `CLAUDE.md`: write one when a decision constrains a
   proposal not yet written, rejects an option that would otherwise leave no
   trace, or supersedes an ADR. This supersedes nothing. The options rejected —
   ratifying nothing, the two other capabilities, the four properties left free,
   and giving the reference matcher a counter — are all recorded above, in the
   change that rejected them, which is the trace. And the constraint itself is
   the spec text: an ADR would be a second copy of a rule that has a home a
   reader is already looking at.

   The one thing that would change this: a proposal to make replay a *restore*
   rather than a re-match — rebuilding a book from a snapshot instead of
   re-issuing commands. That would have to restore the trade counter explicitly,
   and it is the case where this constraint would be met as an obstacle rather
   than read as a requirement. No such proposal exists, the current design is
   documented in `replay.py` as deliberate ("what makes a replay a check on
   determinism rather than a restore"), and an ADR taken speculatively would be
   drawing a boundary nobody has surveyed.

## Risks / Trade-offs

- [The new requirement has no independent implementation checking it] → decision
  4, accepted. The `seq` precedent, and the property is one counter in each. If
  the maintainer would rather the oracle covered it, that is a widening of
  `harness/surface.py` and a counter in `tests/reference/matcher.py`, and it
  should be its own change so the differential suite's comparison getting
  stricter is a decision somebody took rather than a side effect.
- [Ratifying replay reproducibility forecloses a snapshot-restore replay] → it
  does not forbid one; it obliges it to restore the counter. Named in decision 7
  as the one thing that would want an ADR later.
- [Ratifying forecloses "allocate a trade identifier only when recording", one
  increment per execution off the sinkless hot path] → deliberate, and
  already foreclosed in substance: `recording-sink` requires trades to be
  identical with and without a sink, and `Trade` carries the identifier. This
  makes explicit what was implied by a field's membership of a tuple.
- [Two scenarios are two tests for behaviour that already works] → one of them
  already exists as an assertion inside `trade_log()`; the work is making it say
  what it is for. The other is a handful of lines beside its neighbours.
- [An ADDED requirement nobody asked for is scope the maintainer did not
  request] → true, and the reason `proposal.md` puts the "should this be
  ratified at all" question first and the answer's cost in a table. If the
  answer is no, decision 3 says where the reasoning lives so it is not
  rediscovered a third time.

## Open Questions

- None blocking. Whether the differential oracle should learn trade identifiers
  (decision 4) is left with the maintainer as its own change.
- Noted, not proposed: `Filled.trade_id` is recorded and never read back by the
  library — `is_replayable` filters `Filled` out, and nothing computes
  `max(trade_id)` anywhere. Its only consumers are the caller's `Trade` and SQL
  against the sink. That is fine as it stands and is worth knowing before anyone
  "restores" a counter from it.
