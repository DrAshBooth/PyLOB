"""Running a workload against the engine, best-of-N, calibration interleaved.

One engine. ADR-0003 retired the SQL one, so the `--engine new|legacy`
selection the change's design decision 4 called for has nothing to select
between; there is no engine axis here and no flag for one.

What is inside the timed region
-------------------------------

The submission loop and nothing else. The order stream is generated and
materialised first, the book and its traders are configured first, and the
loop that remains does what a caller's loop does: unpack an order, submit it,
count the trades it caused.

Counting trades is inside the region on purpose. `submit` returns the list
either way, so `len()` on it is a few nanoseconds against a submission's
microseconds, and the benchmarking spec requires the trade count to be
reported -- a number that came from a run other than the timed one would be
describing a different run.

For the sink-attached figure, `close()` is inside the region too. The sink
buffers, so a measurement that stopped before the flush would be timing a list
append and calling it persistence.

Why calibration is interleaved and not taken once
-------------------------------------------------

ADR-0005's own evidence is a single interleaved loop that saw 49k to 114k
orders/sec on one workload. A calibration taken once at startup would describe
the machine at startup, and the correction would then be applied to a
measurement taken in a different world. So each repeat is
calibration-then-engine, and the spread across repeats is reported: it is the
most direct evidence available of whether the machine held still, and
`--rebaseline` refuses to record when it did not.

Nor supplied from outside. `measure` could accept a calibration figure from
its caller rather than taking one, which would save about 1.5 seconds of
`tests/test_bench_baselines.py`, where every recording runs the real thing.
**Declined** (lob-ig3, 2026-08). The saving is real, and it is bought by
breaking each of the three properties ADR-0005 hangs the comparison on: the
calibration pass runs *alongside every benchmark run*, so no run can report a
machine speed that nothing measured; the pair is taken microseconds apart,
which is what makes their ratio a statement about the engine rather than about
the moment; and the calibration is versioned *by name*, so a drifting
reference computation forces a re-baseline, where a bare figure arrives with
no name on it and nothing to check it against.

The consequence is not confined to a test. A supplied figure flows straight
into `baselines.save`, and neither `record_problems` nor
`quiet_machine_complaints` can tell a denominator that was measured from one
that was merely asserted -- so the seam ends at a recorded baseline whose
denominator never happened, in the one file whose entire value is that it can
be trusted. The end-to-end shape of those tests is the point of them, not an
accident of how they were written.

The suite already has the better answer to its own cost, and it is in the file
that would have paid for this one: `record_baseline` performs one real
recording and replays its text to the dozen tests that want *a* recorded
baseline rather than a *freshly* recorded one. What stops being repeated there
is the measurement, not the truth of it.

Two figures, taken two different ways
-------------------------------------

Best-of-N is right for the *reported* orders/sec: it is the cleanest
observation of what this machine can do, and it is what the change's plan
asked for.

The *comparison* is made on a different quantity. Each repeat yields

    work index = orders/sec in that repeat x calibration seconds in that repeat
               = orders processed per calibration pass

and `measure` reports the median across repeats. Two reasons, and the second
is weaker than it looks, so both are stated:

- The two factors in a work index were measured microseconds apart, under the
  same conditions. Taking the best engine pass and the best calibration pass
  independently, as this first did, forms a ratio out of two extremes that
  need not have happened in the same world.
- The work index is *meant* to be contention-invariant -- that is the whole
  claim of ADR-0005 -- so the right summary of several observations of it is
  the robust middle, not the luckiest one. Chasing an uncontended sample is
  what calibration exists to make unnecessary.

**What the measurements actually showed.** Ten paired trials under five busy
background processes, each estimator computed from the same runs: median-work-
index 0.912 of the unloaded value, best-of-N 0.915, mean absolute error 0.091
against 0.100, spread 0.86-1.09 against 0.82-1.10. The two are
indistinguishable at this sample size on this machine. The change is kept
because it is the better-founded of two estimators that measure the same, not
because it was shown to be better.

Both, though, are far better than no correction at all: the raw figure landed
at 0.753 of the unloaded value with 9 of those 10 trials below the 20% floor,
against 0 of 10 for either calibrated estimator.

**A residual bias, named because it is still there.** Both estimators
under-corrected by about 9% under that load, and the reason is duration, not
timing: a calibration pass is ~60 ms where an engine pass over 20,000 orders
is ~110 ms, and the shorter measurement is preempted less often. Pairing them
in time does not fix that, because the asymmetry is in how long each one is
exposed. Sizing the calibration to the engine pass would, at the cost of
roughly doubling a run and of changing what a calibration figure means -- a
decision for ADR-0005's author rather than for this module. Nine percent sits
comfortably inside the 20% tolerance, so it is a known bias and not a defect.

What is reported about how much to trust it
-------------------------------------------

Three summaries come out of the same repeats and they answer three questions.
`sinkless.orders_per_sec` is best-of-N and answers "how fast is this engine".
`work_index` is a median and answers "did it change". `work_index_spread` --
`(max - min) / median` of the per-repeat indices -- answers "could this verdict
have gone the other way", and it is the only one of the three that is about the
measurement rather than about the engine.

It is reported because it is the one within-run number that sees a machine
that did not hold still. Read it against the tolerance: a run whose judged
quantity moved by more across three repeats than the band the verdict is drawn
at has not measured one thing three times. `calibration_median_seconds` exists
for the same reason -- the comparison's machine-speed figure is taken from the
same statistic as its verdict, rather than pairing a median work index with the
luckiest calibration pass of the run.
"""

from __future__ import annotations

import statistics
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

from . import calibration as calibration_module
from . import provenance as provenance_module
from . import workloads as workloads_module
from .workloads import CURRENCY, INSTRUMENT, Op, WorkloadSpec

__all__ = ["BenchResult", "RunResult", "Sample", "measure"]


class RunResult(NamedTuple):
    """One timed pass over the order stream."""

    sink: str
    orders: int
    trades: int
    seconds: float

    @property
    def orders_per_sec(self) -> float:
        return self.orders / self.seconds if self.seconds > 0 else float("inf")


class Sample(NamedTuple):
    """One repeat: a calibration pass and the engine pass that followed it.

    The two happened microseconds apart, which is what makes their ratio a
    statement about the engine rather than about the moment.
    """

    calibration_seconds: float
    sinkless: RunResult

    @property
    def work_index(self) -> float:
        """Orders processed per calibration pass, in this repeat.

        The machine-invariant quantity: both factors move together when the
        machine is contended, so their product does not. This is what a
        comparison is made on.
        """
        return self.sinkless.orders_per_sec * self.calibration_seconds


class BenchResult(NamedTuple):
    """Everything one `python -m PyLOB.bench` invocation measured."""

    workload: str
    seed: int
    orders: int
    repeats: int
    #: The gating figure (ADR-0005: the floor is measured sinkless). Best of N.
    sinkless: RunResult
    #: Reported, never gating. None when `--no-sink` skipped it.
    sink: RunResult | None
    #: Best of N, for the report. *Not* what the comparison uses: see
    #: `calibration_median_seconds`.
    calibration: calibration_module.CalibrationResult
    #: (max - min) / min of the calibration seconds across repeats.
    calibration_spread: float
    #: Median of the per-repeat calibration seconds -- the machine-speed figure
    #: a comparison is made on, and recorded with a baseline. It is a median
    #: because `work_index` is: a ratio built from the *luckiest* calibration
    #: pass and the *median* work index describes no run that happened, and
    #: that mismatch is how a run whose calibration varied by 94% across
    #: repeats used to report itself confident.
    calibration_median_seconds: float
    #: Median of the per-repeat work indices. The quantity a regression is
    #: judged on; see the module docstring for why it is not best-of-N.
    work_index: float
    #: (max - min) / median of the per-repeat work indices: how much the judged
    #: quantity itself moved while this run was being taken. The one within-run
    #: number that says whether the verdict could have gone the other way.
    work_index_spread: float
    samples: tuple[Sample, ...]
    provenance: dict[str, Any]
    workload_checksum: int


def _build(spec: WorkloadSpec, sink: Any = None):
    """A configured book, outside the timed region."""
    from PyLOB import OrderBook

    book = OrderBook(tick_size=spec.tick_size, sink=sink)
    book.configure_instrument(INSTRUMENT, CURRENCY)
    minimum, max_percent, per_unit = spec.commission
    for tid in range(1, spec.traders + 1):
        book.configure_trader(
            tid,
            name=str(tid),
            commission_min=minimum,
            commission_max_percnt=max_percent,
            commission_per_unit=per_unit,
        )
    return book


def _drive(book: Any, ops: list[Op]) -> tuple[int, float]:
    """The timed loop. Returns `(trades, seconds)`.

    `submit` and `perf_counter` are bound to locals before the clock starts:
    on a twenty-thousand-order run the global and attribute lookups they would
    otherwise cost are small, but they are cost belonging to the harness and
    not to the engine, and the harness should not appear in its own number.
    """
    from time import perf_counter

    submit = book.submit
    instrument = INSTRUMENT
    trades = 0
    start = perf_counter()
    for tid, side, order_type, qty, price, _kind in ops:
        _order, filled = submit(tid, instrument, side, order_type, qty, price)
        trades += len(filled)
    return trades, perf_counter() - start


def _run_sinkless(spec: WorkloadSpec, ops: list[Op]) -> RunResult:
    book = _build(spec)
    trades, seconds = _drive(book, ops)
    return RunResult(sink="none", orders=len(ops), trades=trades, seconds=seconds)


def _run_with_sink(spec: WorkloadSpec, ops: list[Op], directory: Path) -> RunResult:
    from time import perf_counter

    from PyLOB.sinks.sqlite import SQLiteSink

    path = directory / "bench.db"
    if path.exists():
        path.unlink()
    book = _build(spec, sink=SQLiteSink(path))
    trades, seconds = _drive(book, ops)
    start = perf_counter()
    book.close()
    seconds += perf_counter() - start
    return RunResult(sink="sqlite", orders=len(ops), trades=trades, seconds=seconds)


def measure(
    workload: str = "mixed-v1",
    seed: int = workloads_module.CANONICAL_SEED,
    orders: int | None = None,
    *,
    repeats: int = 3,
    calibration: str = "calib-v1",
    with_sink: bool = True,
    request_performance_core: bool = True,
    machine_label: str | None = None,
) -> BenchResult:
    """Run `workload` at `seed` `repeats` times.

    Reports the best pass as `orders_per_sec` and the median per-repeat work
    index as `work_index`; see the module docstring for why those are two
    different summaries of the same repeats.
    """
    if repeats < 1:
        raise ValueError("repeats must be at least 1, got %r" % (repeats,))
    spec = workloads_module.WORKLOADS.get(workload)
    if spec is None:
        raise ValueError(
            "unknown workload %r; known: %s"
            % (workload, ", ".join(sorted(workloads_module.WORKLOADS)))
        )

    qos = (
        provenance_module.request_performance_core()
        if request_performance_core
        else None
    )
    ops = workloads_module.generate(workload, seed, orders)

    calibrations: list[calibration_module.CalibrationResult] = []
    samples: list[Sample] = []
    sinked: list[RunResult] = []

    with tempfile.TemporaryDirectory(prefix="pylob-bench-") as directory:
        path = Path(directory)
        clock = provenance_module.CoreClock()
        with clock:
            for _ in range(repeats):
                # Calibration immediately before the engine pass, so the pair
                # shares a moment and their ratio can cancel it.
                calibrated = calibration_module.run(calibration)
                engine = _run_sinkless(spec, ops)
                calibrations.append(calibrated)
                samples.append(Sample(calibrated.seconds, engine))
                if with_sink:
                    sinked.append(_run_with_sink(spec, ops, path))

    best_calibration = min(calibrations, key=lambda result: result.seconds)
    slowest = max(result.seconds for result in calibrations)
    spread = (slowest - best_calibration.seconds) / best_calibration.seconds
    median_calibration = statistics.median(result.seconds for result in calibrations)
    indices = [sample.work_index for sample in samples]
    work_index = statistics.median(indices)
    # Relative, so it can be read against the tolerance the verdict is made at.
    index_spread = (max(indices) - min(indices)) / work_index if work_index else 0.0

    context = provenance_module.capture(machine_label)
    run_ghz = clock.ghz
    core_class, efficiency_reference = provenance_module.measure_core_class(run_ghz)
    context["qos"] = qos
    context["effective_ghz"] = None if run_ghz is None else round(run_ghz, 3)
    context["core_class"] = core_class
    context["efficiency_core_ghz"] = (
        None if efficiency_reference is None else round(efficiency_reference, 3)
    )

    return BenchResult(
        workload=workload,
        seed=seed,
        orders=len(ops),
        repeats=repeats,
        sinkless=min((sample.sinkless for sample in samples), key=_seconds),
        sink=min(sinked, key=_seconds) if sinked else None,
        calibration=best_calibration,
        calibration_spread=spread,
        calibration_median_seconds=median_calibration,
        work_index=work_index,
        work_index_spread=index_spread,
        samples=tuple(samples),
        provenance=context,
        workload_checksum=workloads_module.checksum(ops),
    )


def _seconds(result: RunResult) -> float:
    return result.seconds
