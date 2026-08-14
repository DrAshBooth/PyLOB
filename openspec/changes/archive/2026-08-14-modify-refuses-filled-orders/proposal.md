# Proposal: modify-refuses-filled-orders

## Why

`cancelOrder` and `modifyOrder` disagree about what "finished" means. An order
with `qty=10, fulfilled=10` is terminal to one and not to the other:

```python
book.cancelOrder("bid", idNum)                          # InvalidOrder: fully filled
book.modifyOrder(idNum, dict(side="bid", qty=25, ...))  # accepted
```

The modify raises the stated quantity to 25, and because a quantity increase
is not a passive change, the order takes a fresh priority stamp, crosses as a
taker and rests what is left: a completed order is back in the book with 15
available, at the back of a queue it had already left, carrying the fills,
commission and timestamp of the trade it finished.

`docs/engine-review-2026-08.md` (P2, `lob-8r6`) found this independently
twice. `order-lifecycle` does not settle it: the requirement "Modify is
validated and priority-aware" enumerates modify's validations and says nothing
about an order that is already filled. Its clamp scenario — a quantity
reduction below `fulfilled` leaves the order "treated as fully filled" —
*reads* terminal but never says so, which is how the two operations came to
differ without either one contradicting the spec.

It is defensible either way, so it is a decision and not a bug fix, which is
why it arrives as a delta. The maintainer took it in conversation on
2026-08-14: **modify refuses too.** `design.md` records the alternative that
was rejected.

## What Changes

- `order-lifecycle`'s "Modify is validated and priority-aware" gains a
  refusal: a fully filled order is finished, modify raises a library
  exception, and the order and the book are unchanged. Two scenarios pin it —
  the plain fully-filled order, and the order that reached the same state
  through the clamp, which is the path a reader of the old text would have to
  reason about.
- `OrderBook.modifyOrder` gains the guard, next to the refusals it already
  makes (side change, cancelled order, market order), phrased like
  `cancelOrder`'s and naming the remedy: submit a new order.
- The spec-derived reference matcher (`tests/reference/matcher.py`) follows
  the ratified spec, as it does for every other rule.

Not breaking in shape: no signature changes, no new exception type. Breaking
in behavior for exactly one call — modify of an order with nothing left to
trade — which previously returned a resurrected order.

`cancelOrder` is untouched. Its refusal of a fully filled order stays what it
is today: implemented, tested, and deliberately *not* stated by any spec (the
reference matcher names it as such). Ratifying it is a separate decision and
this change does not take it.

## Capabilities

### New Capabilities

<!-- none -->

### Modified Capabilities

- `order-lifecycle`: modify's validation contract gains one refusal, so that
  "fully filled" means the same thing to modify as it does to cancel.

## Impact

- `openspec/specs/order-lifecycle/spec.md` — one MODIFIED requirement
- `src/PyLOB/engine.py` — `modifyOrder` guard and docstring
- `tests/test_engine_boundaries.py` — the refusal, both routes into it
- `tests/acceptance/test_order_lifecycle.py` — one test per new scenario, the
  convention the suite already follows
- `tests/reference/matcher.py` — the oracle follows the spec
- Constraints respected: public API shape unchanged (no ADR needed); no
  runtime dependency; nothing crosses instruments; matching stays in-memory
