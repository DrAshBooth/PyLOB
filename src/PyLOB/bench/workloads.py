"""Seeded, deterministic benchmark workloads.

A workload is fully determined by `(name, seed)`: the same pair produces the
identical order stream on every run and every machine, which is what makes two
measurements comparable at all and what lets the differential harness drive an
engine and a reference matcher on byte-identical input.

This is a library module, not a script. The benchmark runner imports it and so
does the differential harness; a generator that only existed inside a `if
__name__ == "__main__"` block would have to be copied to be reused, and two
copies of a workload named `mixed-v1` is exactly the failure the versioning
rule below exists to prevent.

Versioning: a name is a promise about a composition
---------------------------------------------------

A baseline records "this engine did N orders/sec on `mixed-v1`". That sentence
is only true while `mixed-v1` means what it meant when the number was
recorded. So **changing a workload's composition means a new name, never an
edit** -- `mixed-v2`, alongside `mixed-v1`, with the old baselines still
pointing at the old shape.

That rule is enforced rather than merely written down: `WORKLOADS` records a
checksum of each workload's canonical stream, and `generate` raises if the
stream it just built does not match. Retune the mix and every run fails until
the name changes with it. A *new* workload is cheap -- add a spec, record its
checksum -- which is the point: making the honest path the easy one.

`mixed-v1`
----------

The shape the trial measurement used (2026-08-10, 20,000 orders): 70% passive
limits, 20% crossing limits, 10% market orders, 20 traders with commissions, a
mid that drifts. It is a general-purpose "is the engine still fast" workload,
not a stress case -- the pathological shapes (`lob-rp4`'s self-match walk, a
ten-thousand-level sparse book) belong in workloads of their own, under names
of their own.

The composition is exact, not probabilistic: the generator builds a list of
14,000 / 4,000 / 2,000 kind tags and shuffles it. Drawing each kind from
`rng.random()` would make the 70/20/10 split a per-seed accident, so two seeds
would measure subtly different workloads and the seed would become a hidden
parameter of the number. Shuffling a fixed multiset keeps the seed controlling
only the *order* and the prices, which is the variation that is wanted.

`mixed-v1` is also traffic the differential harness checks. Its `benchmark`
profile replays this stream through the engine and through the spec-derived
reference matcher side by side, comparing both books, both trade logs and
every trader's ledger after every order -- at 200 orders rather than 20,000,
since a per-operation comparison is quadratic in the length of the run. So a
throughput number here is measured on traffic that has been verified rather
than merely executed. That harness is a *consumer* of this module and nothing
flows the other way: this file has no knowledge of it, and imports nothing
from `PyLOB`, so the engine can never influence the inputs it is judged on.
"""

from __future__ import annotations

import random
from typing import Final, NamedTuple

__all__ = ["WORKLOADS", "Op", "WorkloadSpec", "checksum", "generate"]

#: The instrument and currency every workload here trades. One instrument:
#: the engine holds many, but a throughput number wants one hot book rather
#: than several warm ones, and multi-instrument behaviour is the differential
#: suite's business, not the benchmark's.
INSTRUMENT: Final = "FAKE"
CURRENCY: Final = "USD"

#: The kinds an `Op` may be labelled with, and their contribution to a stream
#: checksum. Fixed values: renumbering them would move every pinned checksum
#: without any workload having changed.
_KIND_CODES: Final[dict[str, int]] = {"passive": 1, "crossing": 2, "market": 3}


class Op(NamedTuple):
    """One order to submit.

    A NamedTuple for the same reason `Trade` is one (ADR-0004): the runner
    unpacks a few hundred thousand of these inside the timed region on the
    larger sizes, and tuple unpacking is free where attribute access on a
    dataclass is not. `price` is None for a market order.

    `kind` is the generator's own label -- "passive", "crossing" or "market" --
    and the runner ignores it. It is carried so that a workload's composition
    is a property of the stream rather than of the code that made it: whether
    a limit order crosses depends on the book it meets, so nothing downstream
    could recover the intended 70/20/10 split by inspection, and a test that
    re-derived it from the generator's arithmetic would be asserting that the
    generator agrees with itself.
    """

    tid: int
    side: str
    order_type: str
    qty: int
    price: float | None
    kind: str


class WorkloadSpec(NamedTuple):
    """One named, frozen workload."""

    name: str
    #: Orders in the canonical run. `generate` accepts an override for tests
    #: and quick passes; the checksum only pins the canonical size.
    orders: int
    tick_size: float
    traders: int
    #: Commission schedule applied to every trader, as
    #: (minimum, max percent, per unit). Commissions are on because the trial
    #: measurement had them on and because a fill that skips the commission
    #: branch is not the fill most callers pay for.
    commission: tuple[float, float, float]
    checksum: int
    description: str


def _mixed(spec: WorkloadSpec, seed: int, orders: int) -> list[Op]:
    """`mixed-v1`'s generator: 70% passive, 20% crossing, 10% market.

    The mid is a bounded random walk on the tick grid. Bounded because an
    unbounded walk would spread the book over an ever-wider price range and
    turn a throughput measurement into a measurement of how many price levels
    a 20,000-order run happened to create; the band is 500 ticks wide, which
    is deep enough that passive orders land on hundreds of distinct levels and
    narrow enough that the levels get reused.

    Passive orders rest away from the mid on their own side, at a depth that
    is usually shallow and occasionally deep -- most quoting is near the
    touch, and the tail is what exercises level creation. Crossing orders
    reach through the opposing side by a few ticks, so they walk one or two
    levels rather than sweeping the book. Market orders are smaller, because
    an unbounded market order in a workload that is 70% passive would simply
    empty the book on the first pass and measure an empty book thereafter.
    """
    rng = random.Random(seed)
    tick = spec.tick_size

    n_market = orders // 10
    n_crossing = orders // 5
    n_passive = orders - n_market - n_crossing
    kinds = ["passive"] * n_passive + ["crossing"] * n_crossing + ["market"] * n_market
    rng.shuffle(kinds)

    ops: list[Op] = []
    mid_ticks = 10_000  # 100.00 on a 0.01 tick
    low, high = 9_750, 10_250
    for kind in kinds:
        mid_ticks += rng.randint(-1, 1)
        if mid_ticks < low:
            mid_ticks = low
        elif mid_ticks > high:
            mid_ticks = high
        side = "bid" if rng.random() < 0.5 else "ask"
        sign = 1 if side == "bid" else -1
        # A trader id in [1, traders]. Traders do not allow self-matching (the
        # engine's default), so a taker stepping over its own resting order is
        # generated at the rate 20 traders produce naturally -- a real cost on
        # the match path, and not the pathological one `lob-rp4` describes,
        # which wants a workload of its own.
        tid = rng.randint(1, spec.traders)
        if kind == "market":
            ops.append(Op(tid, side, "market", rng.randint(1, 20), None, kind))
            continue
        if kind == "passive":
            # Shallow most of the time, deep in the tail: 1-8 ticks off the
            # touch on four draws in five, up to 120 on the fifth.
            depth = rng.randint(1, 8) if rng.random() < 0.8 else rng.randint(9, 120)
            price_ticks = mid_ticks - sign * depth
        else:
            # Through the opposing touch by 0-3 ticks.
            price_ticks = mid_ticks + sign * rng.randint(0, 3)
        ops.append(
            Op(
                tid,
                side,
                "limit",
                rng.randint(1, 100),
                round(price_ticks * tick, 10),
                kind,
            )
        )
    return ops


_MIXED_V1 = WorkloadSpec(
    name="mixed-v1",
    orders=20_000,
    tick_size=0.01,
    traders=20,
    commission=(2.5, 1.0, 0.01),
    # Pinned; see the module docstring. `checksum(generate("mixed-v1", 42))`.
    checksum=15240231136383276365,
    description="70% passive limits, 20% crossing, 10% market; drifting mid",
)

WORKLOADS: Final[dict[str, WorkloadSpec]] = {_MIXED_V1.name: _MIXED_V1}

#: name -> generator. Kept beside `WORKLOADS` rather than inside `WorkloadSpec`
#: so a spec stays plain data that can be printed and compared.
_GENERATORS: Final = {"mixed-v1": _mixed}

#: The seed the pinned checksums are taken at. Any seed produces a valid
#: stream; only this one is checked, because checking one is enough to catch
#: an edit and checking all of them is not possible.
CANONICAL_SEED: Final = 42


def checksum(ops: list[Op]) -> int:
    """A stable fingerprint of an order stream.

    Integer-only after the price is put on the tick grid, so it does not
    depend on float formatting. Used to pin a workload's identity and to
    assert two generated streams are the same one.
    """
    acc = 1469598103934665603
    for tid, side, order_type, qty, price, kind in ops:
        acc = (acc * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        acc ^= tid * 31 + (1 if side == "bid" else 2) * 7
        acc ^= (1 if order_type == "limit" else 2) * 1_000_003
        acc ^= qty * 65_537
        acc ^= 0 if price is None else int(round(price * 1e6))
        acc ^= _KIND_CODES[kind] * 2_147_483_647
    return acc


def generate(
    name: str = "mixed-v1",
    seed: int = CANONICAL_SEED,
    orders: int | None = None,
    *,
    verify: bool = True,
) -> list[Op]:
    """The order stream for `(name, seed)`, materialised.

    A list and not a generator: the runner must not pay generation cost inside
    the timed region, and a stream that is built once and replayed N times is
    the only way best-of-N measures the same work N times.

    `verify` checks the canonical stream against the pinned checksum. It only
    applies when the request *is* the canonical one -- same seed, same size --
    since that is the only stream a checksum was recorded for.
    """
    spec = WORKLOADS.get(name)
    if spec is None:
        raise ValueError(
            "unknown workload %r; known: %s" % (name, ", ".join(sorted(WORKLOADS)))
        )
    count = spec.orders if orders is None else orders
    if count <= 0:
        raise ValueError("orders must be positive, got %r" % (count,))
    ops = _GENERATORS[name](spec, seed, count)
    canonical = seed == CANONICAL_SEED and count == spec.orders
    if verify and canonical and spec.checksum:
        actual = checksum(ops)
        if actual != spec.checksum:
            raise ValueError(
                "workload %s at seed %d produced checksum %d, expected %d: the "
                "composition has changed, so every baseline recorded against "
                "this name measured a different workload. Give the changed "
                "workload a new name." % (name, seed, actual, spec.checksum)
            )
    return ops


def composition(ops: list[Op]) -> dict[str, int]:
    """How many of each kind the stream holds, read off the stream itself."""
    counts = dict.fromkeys(_KIND_CODES, 0)
    for op in ops:
        counts[op.kind] += 1
    return counts
