"""The runner: what a measurement reports, and that it reports it about one run.

The benchmarking spec's second requirement -- a run reports orders processed,
trades executed, wall-clock time and orders/sec, tagged with workload and seed
-- plus ADR-0005's provenance list, which is what lets a surprising number be
explained rather than merely disbelieved.

Nothing here asserts a speed. The engine's throughput is the thing the
harness exists to measure and the last thing a correctness suite should have
an opinion about: a test that demanded 100k orders/sec would fail on a slow
machine, which is exactly the false regression ADR-0005 was written to stop.

`engine identity` in the spec's scenario has no field. ADR-0003 left one
engine, so there is nothing to tag: the commit recorded in the provenance is
the identity, and it is a better one than a name that could only ever say
"new".
"""

import statistics

import pytest
from PyLOB.bench import runner, workloads

#: Small enough to keep the suite fast, large enough that the book fills, the
#: crossing orders find something to cross, and the sink writes more than a
#: buffer's worth.
ORDERS = 600


def measure(**kwargs):
    kwargs.setdefault("orders", ORDERS)
    kwargs.setdefault("repeats", 1)
    # Leave the process's scheduling class alone: the benchmark asks for a
    # performance core, a test run has no business doing so.
    kwargs.setdefault("request_performance_core", False)
    return runner.measure(**kwargs)


@pytest.fixture(scope="module")
def result():
    """One plain measurement, shared by everything that describes one.

    Module-scoped because a measurement costs a calibration pass and a
    core-class probe, and the tests below are assertions about a single run's
    report rather than about repetition. The ones that genuinely need a second
    run -- determinism, a different seed, a different configuration -- take
    one.
    """
    return measure()


def test_a_run_reports_everything_the_spec_asks_for(result):
    assert result.workload == "mixed-v1"
    assert result.seed == workloads.CANONICAL_SEED
    assert result.orders == ORDERS
    assert result.sinkless.orders == ORDERS
    assert result.sinkless.trades > 0
    assert result.sinkless.seconds > 0
    assert result.sinkless.orders_per_sec > 0
    assert result.sinkless.orders_per_sec == pytest.approx(
        ORDERS / result.sinkless.seconds
    )


def test_the_sinkless_figure_and_the_sink_figure_are_both_measured(result):
    """ADR-0005 keeps ADR-0002's substance: sinkless gates, the sink reports.

    Both are taken over the identical order stream, so the pair is a statement
    about what recording costs rather than about two different runs.
    """
    assert result.sinkless.sink == "none"
    assert result.sink is not None
    assert result.sink.sink == "sqlite"
    assert result.sink.orders == result.sinkless.orders
    assert result.sink.trades == result.sinkless.trades


def test_the_sink_can_be_skipped():
    result = measure(with_sink=False)

    assert result.sink is None
    assert result.sinkless.trades > 0


def test_the_same_workload_and_seed_produce_the_same_run(result):
    """Determinism end to end, not merely in the generator.

    Identical input is worth little if the engine's behaviour on it varies:
    what makes two throughput numbers comparable is that the same work was
    done both times. Trade counts are the strongest cheap evidence of that --
    they depend on every matching decision the run made.
    """
    again = measure()

    assert again.workload_checksum == result.workload_checksum
    assert again.sinkless.trades == result.sinkless.trades
    assert again.sink.trades == result.sink.trades


def test_a_different_seed_is_a_different_run(result):
    other = measure(seed=7)

    assert other.workload_checksum != result.workload_checksum


def test_the_calibration_runs_alongside_and_is_reported():
    result = measure(repeats=2)

    assert result.calibration.name == "calib-v1"
    assert result.calibration.seconds > 0
    assert result.calibration_spread >= 0


def test_every_repeat_pairs_a_calibration_with_the_engine_pass_that_followed():
    """The work index is only meaningful if its two factors share a moment."""
    result = measure(repeats=3)

    assert len(result.samples) == 3
    for sample in result.samples:
        assert sample.calibration_seconds > 0
        assert sample.sinkless.orders == ORDERS
        assert sample.work_index == pytest.approx(
            sample.sinkless.orders_per_sec * sample.calibration_seconds
        )


def test_the_work_index_is_the_median_of_the_repeats(result):
    """Median, not best-of: see `runner`'s docstring for why they differ."""
    indices = sorted(sample.work_index for sample in result.samples)

    assert result.work_index == pytest.approx(statistics.median(indices))
    assert result.work_index > 0


def test_the_reported_throughput_is_the_best_repeat_not_the_median():
    """The two summaries are of the same repeats and are not the same number.

    `orders_per_sec` is best-of-N because it answers "how fast is this
    engine"; `work_index` is a median because it answers "did it change".
    """
    result = measure(repeats=3)
    best = max(sample.sinkless.orders_per_sec for sample in result.samples)

    assert result.sinkless.orders_per_sec == pytest.approx(best)


def test_provenance_records_what_adr_0005_asks_for(result):
    """Machine, CPU, cores, Python, commit, load average, power source.

    Each may be None on a platform that cannot answer -- best effort is the
    contract, because a benchmark that refused to run because it could not
    read a battery would be the worse tool.
    """
    context = result.provenance

    for field in (
        "machine",
        "cpu",
        "cores",
        "python",
        "power_source",
        "loadavg",
        "commit",
        "core_class",
        "effective_ghz",
        "qos",
    ):
        assert field in context, field

    assert context["python"].startswith("CPython")
    assert set(context["cores"]) == {"logical", "performance", "efficiency"}
    assert context["power_source"] in ("ac", "battery", "unknown")
    assert set(context["commit"]) == {"sha", "branch", "dirty"}


def test_core_class_is_never_a_guess(result):
    """It says "performance", "efficiency", or nothing at all.

    The bands leave a deliberate gap for a throttled performance core, and an
    honest `None` there is worth more than a coin flip -- the field exists to
    explain a surprising number, and a wrong explanation is worse than none.
    """
    assert result.provenance["core_class"] in (None, "performance", "efficiency")


def test_an_unknown_workload_is_refused_before_anything_is_measured():
    with pytest.raises(ValueError, match="unknown workload"):
        measure(workload="no-such-workload")


def test_repeats_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        measure(repeats=0)
