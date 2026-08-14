# Design: sink-re-entrancy-contract

## Context

See `proposal.md` for the finding and both reproductions. What shapes the
approach:

- `OrderBook.emit` calls `EventSink.consume` directly, from wherever the event
  was built. For a `Filled` that is inside `_execute`, inside `match`'s
  per-level loop, inside the `try` that owns an open `match_levels` walk.
- `BookSide.match_levels` pops a price off `_best` before yielding its level
  and pushes back the survivors in its `finally`. So *while a level is being
  walked*, the level is live in `_levels` and absent from `_best`. `_peek`
  reads `_best`; `levels()` and `volume_at` read `_levels`. That divergence is
  the whole of failure mode one, and it is not a bug in either — it is what
  lets the walk step over a level it may not consume.
- `match`'s skip cursor is correct on the stated premise that "orders skipped
  by the gate stay at the front of the level and stay put", written in the
  loop's own comment. A sink cancelling one falsifies the premise. Nothing
  else in the engine can.
- `book-queries` says nothing about *when* its queries are asked. Every one of
  its scenarios reads as a question put to a settled book ("with no
  intervening operations"), and every caller who is not a sink is holding a
  settled book by construction, since the engine is single-threaded and
  synchronous.
- `recording-sink` constrains the engine's emissions and the sink's placement
  ("off the hot path"). It says nothing about what a sink may do while
  consuming, which is the gap.
- `openspec/specs/` is frozen. Ratifying anything here is a delta first.

## Goals / Non-Goals

**Goals:**

- The prohibition becomes a contract, in the capability that owns sinks.
- The *non*-enforcement becomes a contract too, so that adding a guard is a
  change someone has to argue for rather than a tidy-up someone can land.
- `book-queries` stops reading as violated, without being edited.

**Non-Goals:**

- No guard, no buffering, no code change of any kind. Decisions 1 and 2.
- No ratification of what a raising sink leaves behind (`lob-k3h` (c)). It is
  documented in `engine.Order.resting` and pinned nowhere, because writing it
  into a spec would ratify a torn engine state on the back of a decision about
  re-entrancy. It is a real question and it is somebody's, later.
- No `Order` immutability decision (`lob-k3h` (b)). Same reasoning: freezing
  `Order` is a change to a public type and to every write path in
  `engine.py`, and it is not what this change examined.
- No change to `EventSink`'s shape. It stays a one-method `Protocol`.

## Decisions

1. **Document, do not enforce. The prohibition is stated; the engine keeps
   answering.**

   A re-entrancy guard is genuinely arguable, and the argument was taken
   seriously: `book-queries` forbids the state a reading sink observes, so
   making that state unobservable can be read as a bug fix rather than a new
   refusal. Three things decided against it.

   *It refuses the observation rather than fixing the disagreement.* The book
   really is mid-update while the walk is open; `_best` and `_levels` really do
   disagree. A guard does not reconcile them. It makes it an error to ask —
   which is a new refusal, and no spec names it.

   *The cheap form manufactures the worst failure mode from the mildest.* A
   guard that raises raises from inside `consume`, which is inside `emit`,
   which is inside `_execute`, inside the walk. The engine has no recovery
   there: that is failure mode (c), the aborted submission with its fills
   already settled and its `Accepted` already in the stream. So a sink whose
   only sin is calling `getBestAsk` for a log line would go from getting one
   wrong answer to tearing the submission in half. That is a worse outcome for
   the same program, and it is not a defensible reading of "fix".

   *It changes outcomes for a class of program that works today.* A sink that
   reads the book and tolerates the answer — a progress meter, a mid-walk
   tracer — runs today. `recording-sink` requires that attaching a sink not
   change matching outcomes; a guard makes attaching a *particular* sink turn a
   completed submission into an exception. That is the criterion
   `modify-refuses-filled-orders`' design used to call its own change a
   ratified decision rather than a fix, and it applies here with more force,
   because that change broke one call and this would break a whole style of
   sink.

   *Alternative rejected: buffer emissions and drain them at walk exit.* This
   is the only enforcement that is actually sound — it removes the mid-update
   window rather than policing it, so a reading sink gets true answers and a
   mutating one mutates a settled book. It is also the largest change on the
   table. `events.py` fixes emission order as contract ("in the order the
   transitions occurred"; `Modified` before the fills its new price causes;
   `Accepted` before matching), and while buffering preserves the *order*, it
   changes *when* every sink sees every event relative to engine state — which
   is precisely the thing sinks that read are reading. It adds a list
   allocation and a drain to the accept path that ADR-0002 measures, on every
   submission, to serve sinks that are contractually not supposed to be
   looking. And it has its own torn case: an exception during the drain leaves
   events unemitted for transitions that happened. Worth proposing on its own
   merits if the maintainer wants sinks to be able to read; not worth folding
   into a decision about whether to write the prohibition down.

   *Alternative rejected: a guard that returns stale values instead of
   raising.* Snapshot the queries at operation entry and answer a re-entrant
   read from the snapshot. No exception, no torn submission — and a sink now
   reads a book that is neither the one before nor the one after, with no way
   to tell which it got. Two answers to the same question is the defect; a
   third answer is not the fix.

2. **The requirement states the engine's non-enforcement, not just the sink's
   obligation.**

   "A sink SHALL NOT call into the engine" alone would leave the guard as an
   unremarkable hardening of a stated rule — exactly the tidy-up this change
   exists to prevent someone landing. Saying that the engine SHALL NOT detect
   or refuse it makes the guard a spec change, which is what it is. The cost is
   a requirement that reads oddly, since it obliges the engine to *not* check
   something; that is the honest shape of a decision not to enforce, and it is
   the same shape `engine.book` uses in prose for read-creates-a-book.

3. **`recording-sink` gains an ADDED requirement; `book-queries` is not
   touched.**

   The scoping clause — that `book-queries`' guarantees are about state
   observed between operations — is carried *by reference* from the new
   requirement rather than written into `book-queries` itself. One delta
   against one capability, in the capability that owns the thing causing the
   exception.

   *Alternative rejected: also MODIFY `book-queries`' snapshot requirement to
   say "taken between operations".* It is probably where the specs want to end
   up, and it would let `book-queries` be read standalone. It is also a second
   frozen capability edited on the back of a decision about sinks, and its
   requirement about snapshot/query agreement was written for the read side and
   has nothing to do with sinks — narrowing it here would put a qualifier
   nobody asked for into a requirement nobody re-examined. That is the mistake
   `modify-refuses-filled-orders`' design.md declined to make with
   `cancelOrder`'s unstated refusals, and it is left open for the maintainer
   the same way.

   *Alternative rejected: put it in a new capability, `sink-contract`.* The
   obligation is one requirement. A capability for it would be a heading with
   one thing under it, and `recording-sink`'s purpose line already says it is
   "the contract between the matching core and persistence" — which is exactly
   this.

4. **No ADR.** The rule in `CLAUDE.md`: write one when a decision constrains an
   unwritten proposal, rejects an option that would otherwise leave no trace,
   or supersedes an ADR. This supersedes nothing. It rejects three options, and
   all three are recorded above — a trace, in the change that made the
   decision. The one that comes closest to earning an ADR is the deferred-
   emission design, because it constrains a proposal nobody has written; it is
   named in Open Questions instead, so that whoever writes it starts from the
   objection rather than rediscovering it.

## Risks / Trade-offs

- [Ratifying non-enforcement makes the guard harder to add later, and the
  guard might turn out to be right] → that is the intent, and the cost is one
  delta rather than a veto. The reproductions, the rejected designs and the
  measurements are all in this change, so the next person argues against a
  stated position instead of an absence.
- [A spec requirement that describes a hazard rather than a guarantee reads
  strangely, and could be mistaken for blessing the crossed book] → the
  requirement's own text puts the obligation on the sink and calls the
  resulting states undefined, not permitted. The tests in
  `tests/test_engine_boundaries.py` say the same thing in their docstrings, and
  say that they are the tests which should fail first if enforcement is ever
  ratified.
- [Someone writes a sink that reads the book, does not read the spec, and
  ships a wrong number] → unchanged by this proposal either way; that is the
  state today. What changes is that the prohibition is now in three docstrings
  and one requirement rather than one comment.
- [`tests/test_engine_boundaries.py` now pins behaviour the engine does not
  promise] → deliberate, and stated in the module docstring. Those tests are
  the tripwire for an unratified enforcement landing quietly.

## Open Questions

- **Should emissions be deferred to walk exit?** Not blocking, and not
  proposed. It is the only sound enforcement and it is a change to when sinks
  see events, to the accept path's cost, and to the failure behaviour of the
  drain. If it is ever wanted, it is its own change and it supersedes decision
  1 rather than extending it.
- Whether `book-queries`' snapshot requirement should say "between operations"
  in its own words is left to the maintainer, per decision 3.
- Whether the aftermath of a raising sink (`lob-k3h` (c)) should be specified
  at all, or left as engine behaviour the way `cancelOrder`'s terminal
  refusals are, is open and deliberately untouched here.
