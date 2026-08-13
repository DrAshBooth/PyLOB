# ADR-0004: `Trade` is a `NamedTuple`

Status: Accepted
Date: 2026-08-13

## Context

`openspec/config.yaml`'s `context:` block keeps the public API fixed "unless it
proves a limiter of performance or clarity", and says changing it needs an ADR.
This is that ADR, written after the fact: the performance pass following
ADR-0003 changed `Trade` from a frozen slotted dataclass to a `NamedTuple`, and
an adversarial reviewer correctly flagged that the change had shipped without
one.

`Trade` is exported in `PyLOB.__all__` and is what `submit`, `modifyOrder` and
`processOrder` hand back, so its type is public surface.

## Decision

`Trade` is a `NamedTuple`.

It proved a limiter of performance, measured rather than assumed. A frozen
dataclass's `__init__` routes every field through `object.__setattr__`, which
at ten fields cost 730ns per construction — 5.4x a mutable slotted dataclass
and 4.5x a `NamedTuple`. Trades are constructed once per execution on the
matching path, so this was 9.1% of a sinkless run on the mixed workload.
Independently re-measured after the change: **+8.7% on the mixed shape, +10.4%
on one-tick**, and about zero on four of the seven benchmark shapes, which is
what one would expect from a change that only pays where trades are frequent.

## Consequences

**The type widens.** A `Trade` is now a `tuple`: it unpacks positionally,
compares equal to a plain 10-tuple, and is accepted anywhere a sequence is. A
frozen dataclass did none of those. Widening is the safe direction for existing
callers — attribute access, `repr`, and immutability are unchanged — but it is
a real semantic change and code can now come to depend on the tuple-ness.

**It restores something the in-memory engine had taken away.** The legacy
engine's `processOrder` returned a list of plain tuples, and the pre-retirement
review recorded the switch to dataclasses as an undocumented break for porting
callers (`lob-49r`). A `NamedTuple` unpacks the way legacy's tuples did, so a
`for bid, ask, t, p, q in trades` loop works again — with the field *order*
still differing, which is why `lob-49r`'s migration note is still owed.

**Frozen-ness was checked before it was dropped**, not after. Nothing in
`events.py` or the sink depends on `Trade` being a dataclass; the events the
sink consumes are separate frozen dataclasses and are unchanged. `Order`
remains a mutable slotted dataclass, as it must, being the engine's live
record of a resting order.

## Alternatives considered

- **Keep the frozen dataclass.** Rejected: 9% of the hot path for an
  immutability guarantee that `NamedTuple` also provides. The frozen-ness was
  never load-bearing — it was a default, not a decision.
- **A mutable slotted dataclass** (135ns, the fastest option). Rejected: it is
  faster than `NamedTuple` by a little and gives up immutability entirely.
  Handing callers a mutable record of a completed execution invites exactly the
  class of bug the review found with `Order` (`lob-k3h`), where mutating a
  returned object corrupts engine state.
- **Revert and take the loss.** Rejected once the number was independently
  reproduced. The config.yaml clause exists to make this kind of change
  deliberate and recorded, not to forbid it.
