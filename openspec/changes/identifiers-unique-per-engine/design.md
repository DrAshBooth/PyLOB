# Design: identifiers-unique-per-engine

## Context

See `proposal.md` for the ambiguity and the probe. What shapes the approach:

- Both readings of "the book" are coherent designs. Per-instrument identifier
  spaces are what a venue with one sequence per symbol does, and a reader who
  arrived at them from this requirement would not have misread it. That is what
  makes this a clarification with a *decision* in it rather than a typo.
- `_orders` and `_next_idNum` are one map and one counter on `OrderBook`, not
  on `InstrumentBook`. The high-water mark crosses instruments: id 500 supplied
  on one instrument makes the next bare submission on another 501. There is no
  per-instrument counter to disagree with.
- `cancelOrder(side, idNum, time)` and `modifyOrder(idNum, orderUpdate, time)`
  take no instrument, and the public API's shape is a standing constraint
  changed only by ADR. Whatever the spec says, the implementation it is allowed
  to have cannot look up an order by (instrument, identifier).
- The SQLite sink's `orders.idNum INTEGER PRIMARY KEY` is a hard dependency on
  the global reading, not a stylistic one: probed, a second instrument reusing
  an identifier raises `UNIQUE constraint failed`, which the sink's loss path
  records as lost events rather than a crash — the failure mode is a hole in
  history, discovered later.
- `openspec/specs/` is frozen. Wording lands as a delta first.

## Goals / Non-Goals

**Goals:**
- One reading of the requirement under the scope block the specs are now
  written against.
- The word "book" out of this requirement wherever it carried the ambiguity,
  including inside a scenario.
- The rules that had only an implementation get a scenario, so that the
  reference matcher and the engine agree because a spec says so and not because
  both authors thought the same thing.

**Non-Goals:**
- No behaviour change, and no test that would fail today. If a task here turns
  out to need a code change, that is a bug the clarification found and it is
  filed rather than folded in.
- No new exception type, no new rejection. `DuplicateOrderID` already covers
  every refusal named here.
- No statement about *how* identifiers are allocated. "Unique" is the contract;
  the counter is not. A future engine may allocate sparsely, or from a pool, or
  hand out identifiers a caller supplied, and this requirement stays true.
- Nothing about the other three under-determined things this review turned up
  (decision 5).

## Decisions

1. **The scope is the engine and every instrument in it, said explicitly, not
   "the book" replaced by "the engine".** The straight substitution —
   "unique within the engine's lifetime" — is true and still leaves work for
   the reader: it is satisfiable by an engine whose per-instrument counters
   simply never collide, and it does not tell an implementer that the collision
   is *forbidden* rather than merely absent. The text names both halves: the
   scope (every instrument the engine holds, for the engine's lifetime) and the
   prohibition (identifiers are not scoped to an instrument, and not reissued
   after one is finished).

   *Alternative rejected: per-instrument identifiers, with operations taking an
   instrument.* This is a real design and worth stating why it loses here. It
   costs a signature change to `cancelOrder` and `modifyOrder`, which the
   public-API constraint puts behind an ADR; it breaks the sink's `orders`
   primary key and every foreign key into it, so recorded history would need a
   composite key and a schema migration; it makes `Trade`'s `bid_idNum` /
   `ask_idNum` ambiguous without an instrument to read them with. Against all
   that, it buys a researcher nothing this library is for: identifiers here are
   handles onto orders in one simulated session, not tickets a venue hands back.
   The engine, the reference matcher and the sink all chose the global space
   independently, which is evidence about which reading survives contact with
   the rest of the system.

2. **The delta is `## MODIFIED Requirements`, not `## ADDED`.** "Order
   identifiers are unique and stable" is the requirement that *states the
   uniqueness rule*; it is the sentence being read ambiguously. A new
   requirement saying "identifiers are unique across instruments" would leave
   the original standing, unamended, still saying "within the book's lifetime"
   — two requirements on one subject, one of them the one a reader hits first,
   and the ambiguity survives in the text that owns the topic. The same
   reasoning `modify-refuses-filled-orders` used, reached from the other
   direction: there, the requirement enumerated modify's refusals and would have
   read complete-but-wrong; here, the requirement *is* the uniqueness rule and
   would read authoritative-but-ambiguous. The whole block is copied and edited,
   all three existing scenarios included verbatim except the one named in
   decision 3, so archive-time replacement loses nothing.

3. **The "Externally supplied duplicate" scenario is reworded in the same
   breath.** It refused an identifier "already present in the book" — the same
   ambiguous word, doing a second piece of ambiguous work: "present in the
   book" reads as *resting*, which would permit reusing a cancelled order's
   identifier. The requirement's own "within the ... lifetime" clause already
   forbids that, and the engine (probed) rejects it, so the scenario was
   narrower than the requirement it illustrates. It now says "an identifier the
   engine has already issued", and a new scenario covers the finished order
   directly.

   *Alternative rejected: leave the scenario alone and fix only the opening
   sentence.* Minimal, and it leaves the word "book" inside the requirement
   whose ambiguity is the entire subject of the change — the next reader finds
   it and has to work out for themselves that it means something else.
   Rewording it ratifies nothing new: it is the already-ratified lifetime clause
   applied to the scenario that illustrates it, and it is why this stays a
   clarification rather than becoming a decision about cancel semantics.

4. **One non-normative clause says the scope is not the process.** Two
   independent engines each start at 1 (probed). Under ADR-0006 an episode is a
   fresh `OrderBook` and there is no `reset()`, so a researcher sweeping
   episodes holds several engines' identifiers at once and is exactly the person
   who might read "unique for the lifetime" as a promise across them. It is
   phrased as scope rather than as a SHALL because it obliges no implementation:
   a process-global counter would satisfy the requirement too. No scenario, for
   the same reason — there is nothing to fail.

5. **The three other under-determined things found in the sweep are reported,
   not proposed.** The brief for this change asked whether the ambiguity is
   anywhere else. It is, in weaker forms, and none of them belongs in a change
   about identifiers:

   - **`book-queries`.** "Best and worst prices reflect resting limit orders"
     and "Volume at price answers the marketable question" say "the book" and
     "a side" where their two neighbours in the same spec say "per instrument"
     and "for an instrument". The asymmetry is real, but the ambiguity is not
     live: every one of those queries takes an instrument
     (`getBestBid(instrument)`, `getVolumeAtPrice(instrument, side, price)`),
     prices from two instruments are not comparable so a pooled answer is not a
     design anyone could have meant, and the acceptance suite already loads a
     second instrument (sorting *before* the default, so an engine answering
     from the wrong book answers visibly wrong). Cosmetic; a maintainer may want
     the wording evened up, and it should be its own change so it can touch both
     requirements without an identifier decision riding along.
   - **The `priority` counter.** Engine-wide, interleaved across instruments
     (probed: FAKE 1, OTHER 2, FAKE 3, OTHER 4). "Price-time priority is
     deterministic" says "a total order (arrival sequence number)" and never
     says whether that counter is per engine or per instrument. Unlike
     identifiers, both readings are behaviourally identical — matching only ever
     compares orders within one instrument's book, and the acceptance surface
     exposes priority as queue *position*, not as a number. The one place the
     value escapes is the sink (`orders.priority`, the `resting_order` view), so
     the open question is whether that column is a contract at all. That is a
     decision about what the sink promises, not a clarification, and this change
     has no standing to take it.
   - **Trade identifiers.** `_next_trade_id` is engine-wide and the sink makes
     `trade.trade_id` a primary key, but no ratified requirement mentions trade
     identifiers in any spec. That is a gap, not an ambiguity: there is no text
     to modify, and closing it means an ADDED requirement about what a trade
     identifier promises — a capability question nobody has asked.

   Checked and clear: `recording-sink`'s "monotonically increasing sequence
   number" is stated of *the engine* and so already names its scope.

6. **No ADR.** The rule in `CLAUDE.md`: write one when a decision constrains a
   proposal not yet written, rejects an option that would otherwise leave no
   trace, or supersedes an ADR. This supersedes nothing. The rejected option —
   per-instrument identifier spaces — is recorded in decision 1, in the change
   that rejected it, which is the trace. And it constrains nothing that the
   ratified requirement will not constrain more directly and in the place a
   reader is already looking: the decision *is* the spec text here, which is the
   case where an ADR would be a second copy of a rule that has a home.

   The one thing that would change this: if the maintainer wants the global
   identifier space to bind work that is not a change to `order-lifecycle` —
   sharding the engine per instrument, or running books concurrently, where the
   constraint would be met as an obstacle rather than read as a requirement.
   That decision is not on the table, and an ADR taken speculatively would be
   asserting a boundary nobody has surveyed.

## Risks / Trade-offs

- [The wording ratifies today's engine and forecloses per-instrument
  identifiers] → deliberate, and the point. Reversing it later costs an ADR and
  a sink schema migration, which is the correct price for a change that would
  break every recorded database.
- [Four new scenarios are four new acceptance tests for behaviour that already
  works] → they are the only artifact that stops the engine and the reference
  matcher drifting apart on a rule they currently share by coincidence, and
  `engine_factory(instruments=...)` means each is a handful of lines.
- [The `priority` and trade-identifier gaps stay open] → per decision 5, both
  need a maintainer decision this change has no mandate for. They are in the
  handoff so they are not rediscovered from scratch.
- [A source comment quoting the old clause is easy to miss] → two are named in
  `tasks.md` by file, and the search is the literal phrase "within the book's
  lifetime".

## Open Questions

- None blocking. Whether `book-queries`' two loose requirements get the same
  treatment, and whether `orders.priority` is a contract, are left with the
  maintainer as their own changes.
