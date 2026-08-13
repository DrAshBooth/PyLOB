"""The calibration workload: a reference computation that measures the machine.

ADR-0005 judges throughput against a *calibrated* baseline. The engine's
orders/sec is the numerator; this module is the denominator. A run and a
baseline are compared after scaling by the ratio of their calibration figures,
so a machine that is uniformly 30% slower reads as no regression rather than
as a 30% one.

For that to work the denominator has to have three properties, and every
decision here follows from one of them.

**It must not import the engine.** If a change to `PyLOB.engine` could move the
calibration, the calibration would cancel out the very thing it is measuring:
an engine made 30% slower would slow the denominator too and read as no
regression at all. So this module imports the standard library and nothing
else, and it is a deliberate near-miss of `engine.py` rather than a call into
it -- `_phase_decimal` reproduces the *shape* of `engine._quantize` (divide,
quantize, multiply, in a `Context`) without importing it, and is free to drift
from it, because what it measures is how fast this machine does that kind of
arithmetic, not what the engine does with the answer.

**It must track what the engine's hot path is made of.** A denominator that
measured, say, memory bandwidth would correct for the wrong thing: a machine
with slow RAM and a fast core would read as a false improvement. The five
phases are the five costs a submission actually pays, and their time budget
(`_BUDGET`) is set to roughly the proportions the engine pays them in:

    dispatch   30%   bytecode dispatch, attribute loads, bound-method calls --
                     the per-order Python overhead that dominates everything
    dict       30%   `_levels`, `_orders`, `_grid`, `PriceLevel._orders`: the
                     engine is a pile of dicts and the hot path is dict work
    heap       15%   `BookSide._best` / `_worst`, including the pop-until-live
                     shape lazy deletion gives them
    float      15%   price x qty, running values, the settlement arithmetic
    decimal    10%   `clipPrice` on a memo miss; small because it *is* memoized

**It must not be sensitive to anything the engine is not.** No I/O, no
threading, no transcendental functions, no allocation large enough to change
the collector's behaviour, and no random numbers at run time. Every working
set here is a few thousand small objects, which is the scale the engine's own
structures live at.

Determinism and versioning
--------------------------

`run` returns a checksum alongside the timing, and `CALIBRATIONS` records the
checksum each named calibration must produce. A checksum mismatch is a hard
error: it means the computation is not the one the baselines were recorded
against, and the recorded ratios no longer mean anything.

That is the enforcement behind ADR-0005's versioning rule. Editing this file's
arithmetic changes the checksum, which fails every run until the name changes
too -- so "changing a calibration means a new name" cannot be forgotten, only
overruled on purpose. It is the same rule `workloads.py` follows, for the same
reason: a silently edited denominator makes every recorded baseline a lie
about a computation that no longer exists.

The checksums are derived from integers and from floats truncated to integers.
IEEE-754 addition, multiplication and division are deterministic given the
same operation sequence, and `decimal` is deterministic given an explicit
`Context`, so the same checksum is expected on any platform CPython runs on --
which is what lets a baseline recorded on one machine be checked on another.
"""

from __future__ import annotations

import time
from decimal import ROUND_HALF_EVEN, Context, Decimal
from heapq import heappop, heappush
from typing import Callable, Final, NamedTuple

__all__ = ["CALIBRATIONS", "CalibrationResult", "CalibrationSpec", "run"]

_MASK: Final = (1 << 61) - 1


class _Cell:
    """A small mutable object with a method, for the dispatch phase.

    `__slots__` because the engine's `Order` and `PriceLevel` have them: a slot
    store and a dict store are different instructions and this is meant to
    measure the one the engine executes.
    """

    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = value

    def bump(self, step: int) -> int:
        value = self.value = (self.value + step) & _MASK
        return value


def _phase_dispatch(rounds: int) -> int:
    """Bytecode dispatch: attribute loads, bound-method calls, small-int work.

    The per-order overhead that dominates a Python matching engine. Nothing
    here is interesting arithmetically; the point is the interpreter loop.
    """
    cell = _Cell(1)
    total = 0
    for i in range(rounds):
        value = cell.bump(i)
        if value > i:
            total += value & 0xFFFF
        else:
            total -= 1
        total &= _MASK
    return total


def _phase_dict(rounds: int) -> int:
    """Dict work with float keys, on a working set the size of a price ladder.

    Insert, look up, mutate and delete -- the four things the engine does to
    `_levels` and to a `PriceLevel`'s order dict, with float keys because a
    price is a float and float hashing is not int hashing.

    512 levels is a book a few hundred ticks deep on each side: big enough
    that the dict is a real hash table rather than a handful of slots, small
    enough to stay in cache, which is where the engine's own dicts live.
    """
    width = 512
    grid = [round(100.0 + n * 0.01, 2) for n in range(width)]
    levels: dict[float, int] = {}
    total = 0
    for i in range(rounds):
        key = grid[i % width]
        depth = levels.get(key)
        if depth is None:
            levels[key] = 1
        elif depth < 4:
            levels[key] = depth + 1
            total += depth
        else:
            del levels[key]
            total += 4
        total &= _MASK
    return (total + len(levels)) & _MASK


def _phase_heap(rounds: int) -> int:
    """Heap pushes and pops over prices, in the shape lazy deletion gives them.

    `BookSide` keeps two heaps of prices and deletes from them lazily, so the
    read is "pop until you find one that is still live", not a plain pop. The
    `live` set reproduces that without reproducing the book.
    """
    heap: list[float] = []
    live: set[float] = set()
    total = 0
    for i in range(rounds):
        price = 100.0 + ((i * 7) % 401) * 0.01
        heappush(heap, price)
        live.add(price)
        if i % 3 == 2:
            while heap:
                top = heappop(heap)
                if top in live:
                    live.discard(top)
                    total += int(top * 100.0)
                    break
        total &= _MASK
    return (total + len(heap)) & _MASK


def _phase_float(rounds: int) -> int:
    """Float arithmetic: the price x qty and running-value work of a fill.

    Multiplication, division, addition and comparison, with the operands kept
    small and bounded so the accumulator stays far inside the exactly-additive
    range and the checksum is a property of the operation sequence rather than
    of where the rounding happened to land.
    """
    total = 0.0
    peak = 0.0
    for i in range(rounds):
        qty = float(1 + (i % 97))
        price = 100.0 + (i % 211) * 0.01
        value = price * qty
        total += value
        average = total / float(i + 1)
        if average > peak:
            peak = average
    return (int(total) + int(peak * 1000.0)) & _MASK


def _CTX_factory() -> Context:
    return Context(prec=40, rounding=ROUND_HALF_EVEN)


def _phase_decimal(rounds: int) -> int:
    """Tick quantization, in `engine._quantize`'s shape and not its code.

    Divide by the tick in a wide context, round the quotient to an integer,
    multiply back -- plus the `Decimal(str(price))` conversion, which is a
    float-to-string and is a real part of what a memo miss costs.

    A deliberate copy rather than an import: see the module docstring. If the
    engine's quantizer changes, this must not follow it, because a denominator
    that moves with the numerator measures nothing.
    """
    ctx = _CTX_factory()
    one = Decimal(1)
    tick = Decimal("0.01")
    total = 0
    for i in range(rounds):
        price = 100.0 + (i % 337) * 0.005
        multiples = ctx.divide(Decimal(str(price)), tick).quantize(
            one, rounding=ROUND_HALF_EVEN, context=ctx
        )
        total += int(ctx.multiply(multiples, tick) * 100)
        total &= _MASK
    return total


class CalibrationSpec(NamedTuple):
    """One named, frozen calibration computation.

    `checksum` is the value `run` must produce. It is what makes the name
    binding: change the arithmetic and the checksum moves, and every run fails
    until the name moves with it.
    """

    name: str
    #: phase name -> (function, iteration count)
    phases: dict[str, tuple[Callable[[int], int], int]]
    checksum: int
    description: str


class CalibrationResult(NamedTuple):
    """One calibration measurement.

    `seconds` is the wall time for one whole pass over every phase. `rate` is
    its reciprocal -- passes per second -- which is the figure comparisons
    scale by, because it moves the same way orders/sec does: a slower machine
    has a lower rate and a lower throughput, and their ratio is what cancels.
    """

    name: str
    seconds: float
    checksum: int

    @property
    def rate(self) -> float:
        """Calibration passes per second. Higher is a faster machine."""
        return 1.0 / self.seconds if self.seconds > 0 else float("inf")


#: Iteration counts chosen so that a pass takes roughly 60 ms on an unloaded
#: 2020-era laptop core and the phases land near `_BUDGET`'s proportions.
#: 60 ms is long enough that the clock's resolution and one scheduler tick are
#: both negligible, and short enough that running it three times alongside the
#: benchmark costs nothing anyone will notice.
_CALIB_V1 = CalibrationSpec(
    name="calib-v1",
    phases={
        "dispatch": (_phase_dispatch, 156_000),
        "dict": (_phase_dict, 146_000),
        "heap": (_phase_heap, 34_000),
        "float": (_phase_float, 81_000),
        "decimal": (_phase_decimal, 7_500),
    },
    # Pinned; see the module docstring. Every phase is integer, IEEE-754 or
    # `decimal` arithmetic with an explicit context, so this is expected to
    # hold on any platform CPython runs on -- which is what lets a baseline
    # recorded on one machine be checked on another at all.
    checksum=4717557328431862,
    description=(
        "interpreter dispatch, dict, heap, float and decimal work in the "
        "proportions the engine's hot path pays them"
    ),
)

CALIBRATIONS: Final[dict[str, CalibrationSpec]] = {_CALIB_V1.name: _CALIB_V1}

#: What each phase is *meant* to be, as a fraction of a pass. Not enforced at
#: run time -- a machine with an unusual `decimal` is allowed to have an
#: unusual split -- but `tests/test_bench_calibration.py` checks the intended
#: shape still roughly holds, so a phase cannot quietly come to dominate and
#: turn the denominator into a measure of one thing.
_BUDGET: Final[dict[str, float]] = {
    "dispatch": 0.30,
    "dict": 0.30,
    "heap": 0.15,
    "float": 0.15,
    "decimal": 0.10,
}


def phase_budget() -> dict[str, float]:
    """The intended per-phase share of a calibration pass."""
    return dict(_BUDGET)


def run(name: str = "calib-v1", *, verify: bool = True) -> CalibrationResult:
    """One calibration pass, timed.

    Raises `ValueError` on an unknown name, and (when `verify`) on a checksum
    mismatch -- which means the computation is not the one the baselines were
    recorded against, so no comparison against them is meaningful.
    """
    spec = CALIBRATIONS.get(name)
    if spec is None:
        raise ValueError(
            "unknown calibration %r; known: %s"
            % (name, ", ".join(sorted(CALIBRATIONS)))
        )
    checksum = 0
    start = time.perf_counter()
    for phase, (fn, rounds) in spec.phases.items():
        checksum = (checksum * 31 + fn(rounds)) & _MASK
    seconds = time.perf_counter() - start
    if verify and spec.checksum and checksum != spec.checksum:
        raise ValueError(
            "calibration %s produced checksum %d, expected %d: the reference "
            "computation has changed, so every recorded baseline scaled by it "
            "is meaningless. Give the changed computation a new name."
            % (name, checksum, spec.checksum)
        )
    return CalibrationResult(name=name, seconds=seconds, checksum=checksum)


def profile(name: str = "calib-v1") -> dict[str, float]:
    """Per-phase seconds for one pass. For tuning the iteration counts."""
    spec = CALIBRATIONS[name]
    timings = {}
    for phase, (fn, rounds) in spec.phases.items():
        start = time.perf_counter()
        fn(rounds)
        timings[phase] = time.perf_counter() - start
    return timings
