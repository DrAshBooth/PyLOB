# Design: modify-refuses-filled-orders

## Context

See `proposal.md` for the finding. What shapes the approach:

- `Order.filled` is `fulfilled >= qty` and `Order.resting` is
  "limit, not cancelled, and `fulfilled < qty`". Both are derived, so the
  refusal can read the same property `cancelOrder` reads and cannot drift
  from it.
- `modifyOrder` already refuses three things — a side that is not the
  order's, a cancelled order, and a market order — each before the clock
  moves, each with a message that names the reason. `cancelOrder` refuses the
  same first two plus the fully filled order. The gap is exactly one clause.
- The clamp (`new_qty = max(..., order.fulfilled)`) *produces* fully filled
  orders. Whatever the rule is, it has to hold for an order that arrived at
  `fulfilled == qty` that way as well as by trading.
- The reference matcher (`tests/reference/matcher.py`, ADR-0003) is derived
  from these specs and shares no code with the engine. A ratified rule that
  lands in one and not the other leaves the differential oracle asserting an
  older spec than the one in `openspec/specs/`.
- `openspec/specs/` is frozen. A behavior change here is a delta first.

## Goals / Non-Goals

**Goals:**
- One meaning of "finished" across cancel and modify.
- The refusal stated in `order-lifecycle`, not merely implemented.
- Both routes into the filled state covered: filled by trading, and filled by
  a clamp.

**Non-Goals:**
- No change to `cancelOrder`, and no ratification of its own unspecified
  refusals (already-cancelled, nothing-left-to-cancel). They are correct and
  tested; stating them is a separate change, and folding them in here would
  put behavior nobody re-examined into a frozen spec on the back of a
  decision about modify.
- No new exception type. `InvalidOrder` is what every neighbouring refusal
  raises.
- No change to what a *partially* filled order may do. Reducing to the
  fulfilled amount still clamps and still finishes the order; that is the
  last modification it accepts, not one that is now refused.

## Decisions

1. **A fully filled order refuses modification. Maintainer decision, taken in
   conversation on 2026-08-14** (recorded here because the conversation is
   not an artifact and would otherwise leave no trace).

   *Alternative rejected: resurrection is legitimate, because the order's
   **stated** quantity changed.* The argument is real and was considered:
   `modifyOrder(qty=25)` does not claim the old fills never happened, it
   states a new size, and an order of 25 with 10 fulfilled genuinely has 15
   left to trade — no accounting invariant is broken, and `order-matching`'s
   "never trades beyond its unfulfilled remainder" is still satisfied.

   Rejected because the quantity is not the only thing that comes back. A
   filled order has already left the book, so there is nothing to modify *in
   place*: the operation is an insertion wearing an old identifier, and it
   carries the old order's fills, its commission, its arrival timestamp and
   its trade history into a queue position it did not earn. Priority is the
   engine's scarcest guarantee, and `order-lifecycle` spends a whole
   requirement on who holds it. Worse, the disagreement itself is the defect
   a caller trips over: the same order answers "am I finished?" differently
   depending on which method asks, so a strategy that cancels-then-resubmits
   and one that modifies diverge on the same book. The intent the rejected
   reading serves is expressible exactly, with one call, and honestly: submit
   a new order. It gets its own identifier, its own stamp, its own place in
   the queue, and the finished order stays finished.

2. **The guard reads `order.filled`, and sits after the market-order check.**
   Check order becomes side → cancelled → market → filled, which is
   `cancelOrder`'s order with modify's extra check kept where it already was.
   That placement matters: a market order that filled completely satisfies
   both predicates, and "is a market order and never rested" tells its caller
   more than "is fully filled" would.

   *Alternative rejected: one `if not order.resting` covering all of it.*
   `resting` is false for a cancelled order, a market order and a filled one
   alike, so a single check is cheaper to write and collapses three
   diagnoses into one message. The review praised these messages for
   teaching; this would spend that.

3. **The delta is `## MODIFIED Requirements`, not `## ADDED`.** "Modify is
   validated and priority-aware" is the requirement that *enumerates* what
   modify rejects — a side change, a quantity below the fills. A refusal
   added anywhere else leaves that enumeration reading as complete while
   being wrong, so someone implementing modify from the requirement builds
   the resurrection back in. The two also interact directly: the clamp clause
   lives in this requirement and is one of the two ways an order reaches the
   state now being refused. The whole requirement block is copied and edited
   rather than excerpted, so archive-time replacement keeps all four existing
   scenarios.

   *Alternative rejected: an ADDED requirement, "a fully filled order is
   terminal", covering cancel and modify together.* It reads well and is
   where the capability probably wants to end up. It also silently ratifies
   `cancelOrder`'s two refusals, which no spec states today (the reference
   matcher says so in as many words) and which this decision did not
   examine — a bigger spec change than the one that was approved, arriving
   without being asked for.

4. **No ADR.** The `docs/adr/` rule is: write one when a decision constrains
   an unwritten proposal, rejects an option that would otherwise leave no
   trace, or supersedes an ADR. This supersedes nothing and constrains
   nothing beyond the requirement it edits, and the rejected option is
   recorded above — a trace, in the change that made the decision.

5. **The reference matcher follows.** It is derived from the specs, so a
   ratified rule belongs in it; leaving it out would mean the differential
   harness's oracle enforces a superseded contract, held harmless only by the
   fact that the generator happens never to produce the input. Nothing in the
   harness needs to change: `_modify` draws its target from a book snapshot,
   and a filled order is not in the book.

## Risks / Trade-offs

- [A caller that modified a filled order now gets an exception where it
  previously got a resurrected order] → the intended replacement is one call
  (`submit`), the message names it, and the search below found no such caller
  in this repo. It is a real breaking change for anyone outside it, which is
  what makes it a ratified decision rather than a fix.
- [The generators could grow this input later and start failing] → they draw
  cancel and modify targets from a book snapshot, which is already written
  down as a constraint in `tests/test_differential.py`
  (`CONSTRAINTS["cancel and modify address a live resting order"]`) and
  followed by `tests/test_sink_projections.py` and `tests/test_sink_equality.py`.
  A generator that stops doing that would see the refusal immediately, in
  every profile, rather than subtly.
- [`cancelOrder`'s refusals stay unstated] → deliberate, per Non-Goals; the
  asymmetry in *behavior* is what `lob-8r6` filed, and it is gone.

## Open Questions

- None blocking. Whether to state cancel's own terminal refusals in
  `order-lifecycle` is left open for the maintainer, as its own change.
