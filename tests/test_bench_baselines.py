"""Calibrated comparison, tolerance, confidence, and the re-baseline flow.

The benchmarking spec's third requirement -- regression is judged against a
recorded baseline, and baselines change only by an explicit act -- plus what
ADR-0005 added to it: the comparison is *normalised* by calibration first.

Most of this file measures nothing. ADR-0005's central claim ("a machine that
is uniformly 30% slower reads as no regression") is an arithmetic claim, and
arithmetic can be asserted exactly. Tests that ran the engine and hoped the
machine cooperated would prove the same thing worse, and would fail on a busy
laptop for reasons unconnected to the code -- which is the failure ADR-0005
exists to stop, so it would be a poor thing to build into its own test suite.

The two tests at the end do run the harness end to end, at a size chosen to
stay under a second. They exist to keep the command-line path and the exit
codes honest, not to measure anything: the baseline they compare against is
doctored by a known factor, so their verdict is determined by arithmetic too.
"""

import json
from pathlib import Path

import pytest
from PyLOB.bench import baselines
from PyLOB.bench.__main__ import main

#: A baseline taken on some machine: 100k orders/sec, 50 ms per calibration
#: pass, and therefore 5,000 orders processed per calibration pass.
BASE_OPS = 100_000.0
BASE_CALIB = 0.050
BASE_WORK = BASE_OPS * BASE_CALIB


def compare(measured, calibration_seconds, **kwargs):
    """A run of `measured` orders/sec on a machine of `calibration_seconds`.

    The work index is formed the way `runner` forms it -- the run's own
    throughput times the run's own calibration -- so these tests exercise the
    quantity the harness actually judges on rather than a hand-set one.
    """
    kwargs.setdefault("measured_work_index", measured * calibration_seconds)
    return baselines.compare(
        measured=measured,
        measured_calibration_seconds=calibration_seconds,
        baseline=BASE_OPS,
        baseline_work_index=BASE_WORK,
        baseline_calibration_seconds=BASE_CALIB,
        **kwargs,
    )


# --------------------------------------------------------------------------
# normalisation: the claim ADR-0005 is built on
# --------------------------------------------------------------------------


def test_a_uniformly_slower_machine_is_not_a_regression():
    """The whole point. A machine 30% slower reads as no regression.

    Both figures fall together -- the engine does 70% of the throughput and
    the calibration takes 1/0.7 as long -- so the ratio cancels and the
    normalised figure lands back on the baseline.
    """
    result = compare(BASE_OPS * 0.7, BASE_CALIB / 0.7)

    assert result.normalised == pytest.approx(BASE_OPS)
    assert result.status == "ok"
    assert result.speed_ratio == pytest.approx(0.7)


def test_a_uniformly_faster_machine_is_not_an_improvement():
    """The correction is symmetric, which is what stops it hiding regressions.

    A one-sided correction would let a fast machine bank headroom and carry a
    real regression through it.
    """
    result = compare(BASE_OPS * 1.2, BASE_CALIB / 1.2)

    assert result.normalised == pytest.approx(BASE_OPS)
    assert result.status == "ok"


def test_the_same_machine_compares_plainly():
    """With calibration unchanged, normalisation is the identity."""
    result = compare(BASE_OPS * 0.5, BASE_CALIB)

    assert result.normalised == pytest.approx(BASE_OPS * 0.5)
    assert result.speed_ratio == pytest.approx(1.0)
    assert result.status == "regression"


def test_a_slower_engine_on_a_slower_machine_is_still_a_regression():
    """Normalisation corrects for the machine and only for the machine.

    The machine is 30% slower *and* the engine is 40% slower on top: the
    calibration cancels the first and the second survives, which is the
    behaviour that makes the scheme a guard rather than an excuse.
    """
    result = compare(BASE_OPS * 0.7 * 0.6, BASE_CALIB / 0.7)

    assert result.normalised == pytest.approx(BASE_OPS * 0.6)
    assert result.status == "regression"
    assert "below" in result.reason


def test_normalisation_does_not_rescue_a_regression_that_exceeds_it():
    """A 25% engine regression on an unchanged machine fails at 20% tolerance."""
    result = compare(BASE_OPS * 0.75, BASE_CALIB)

    assert result.failed


# --------------------------------------------------------------------------
# tolerance
# --------------------------------------------------------------------------


def test_the_tolerance_band_is_twenty_percent_by_default():
    assert baselines.DEFAULT_TOLERANCE == 0.20

    assert not compare(BASE_OPS * 0.81, BASE_CALIB).failed
    assert compare(BASE_OPS * 0.79, BASE_CALIB).failed


def test_the_tolerance_is_overridable():
    slowed = BASE_OPS * 0.85

    assert not compare(slowed, BASE_CALIB, tolerance=0.20).failed
    assert compare(slowed, BASE_CALIB, tolerance=0.05).failed


def test_the_floor_is_reported_so_a_failure_can_be_read():
    result = compare(BASE_OPS * 0.5, BASE_CALIB)

    assert result.floor == pytest.approx(BASE_OPS * 0.8)
    assert result.baseline == BASE_OPS
    assert result.measured == pytest.approx(BASE_OPS * 0.5)


# --------------------------------------------------------------------------
# confidence: normalisation is a correction, not a cure
# --------------------------------------------------------------------------


def test_a_calibration_close_to_the_baselines_is_confident():
    for factor in (1.0, 1.2, 1 / 1.2):
        assert compare(BASE_OPS * factor, BASE_CALIB / factor).confident


def test_a_calibration_far_from_the_baselines_is_low_confidence():
    """Outside the band, the linear correction is reported as untrustworthy.

    ADR-0005: such a run "should be reported as low-confidence, not silently
    scaled". Half speed is roughly the performance-to-efficiency core drop
    this project measured, which is precisely the case where the correction's
    uniform-slowdown assumption fails.
    """
    result = compare(BASE_OPS * 0.5, BASE_CALIB / 0.5)

    assert not result.confident
    assert "LOW CONFIDENCE" in result.reason
    # Still normalised, still reported -- flagged, not suppressed.
    assert result.normalised == pytest.approx(BASE_OPS)
    assert result.status == "ok"


def test_the_confidence_band_sits_below_the_performance_efficiency_gap():
    """The band has to catch a P/E crossing and ignore ordinary jitter.

    ADR-0005 quotes ~40% for an efficiency-core run. A band at or above that
    would scale a P/E crossing silently, which is the specific thing it is
    there to prevent.
    """
    assert 1.05 < baselines.CONFIDENCE_BAND < 1.40


def test_low_confidence_does_not_hide_a_regression():
    """A busy machine is a reason to distrust a number, not to ignore it."""
    result = compare(BASE_OPS * 0.5 * 0.5, BASE_CALIB / 0.5)

    assert not result.confident
    assert result.failed


# --------------------------------------------------------------------------
# absent baselines
# --------------------------------------------------------------------------


def test_no_baseline_is_an_absence_and_not_a_regression():
    result = baselines.compare(
        measured=123.0,
        measured_work_index=6.0,
        measured_calibration_seconds=BASE_CALIB,
        baseline=None,
        baseline_work_index=None,
        baseline_calibration_seconds=None,
    )

    assert result.status == "no-baseline"
    assert not result.failed
    assert result.normalised is None


def test_a_placeholder_entry_reads_as_no_baseline():
    document = {
        "schema": 1,
        "baselines": {
            "k": {"placeholder": True, "sinkless_orders_per_sec": None},
            "partial": {"placeholder": False, "sinkless_orders_per_sec": 1.0},
            "real": {
                "placeholder": False,
                "sinkless_orders_per_sec": 1.0,
                "work_index": 2.0,
            },
        },
    }

    assert baselines.entry(document, "k") is None
    assert baselines.entry(document, "missing") is None
    assert baselines.entry(document, "partial") is None, (
        "a record without a work index cannot be judged against"
    )
    assert baselines.entry(document, "real") is not None


def test_the_shipped_baselines_file_is_a_marked_placeholder():
    """`benchmarks/baselines.json` must not pretend to hold a measurement.

    Recording real numbers is a deliberate act on a quiet machine. Until it
    happens the file documents the schema and guards nothing, and it must say
    so in terms nobody can miss.
    """
    path = Path(__file__).resolve().parent.parent / "benchmarks" / "baselines.json"
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["schema"] == baselines.SCHEMA
    assert "PLACEHOLDER" in document["note"]
    assert document["baselines"], "the file should still document the key format"
    for key, record in document["baselines"].items():
        assert record["placeholder"] is True, key
        assert record["sinkless_orders_per_sec"] is None, key
        assert record["work_index"] is None, key
        assert record["calibration_seconds"] is None, key
        assert baselines.entry(document, key) is None, key


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------


def test_the_key_carries_everything_that_changes_what_was_measured():
    key = baselines.baseline_key("mixed-v1", 42, 20_000, "calib-v1")

    assert key == "mixed-v1|seed=42|orders=20000|calib=calib-v1"
    assert key != baselines.baseline_key("mixed-v1", 43, 20_000, "calib-v1")
    assert key != baselines.baseline_key("mixed-v1", 42, 2_000, "calib-v1")
    assert key != baselines.baseline_key("mixed-v2", 42, 20_000, "calib-v1")


def test_a_renamed_calibration_orphans_the_baseline_rather_than_reusing_it():
    """ADR-0005 makes a changed calibration require a re-baseline.

    Because the name is in the key, that happens without anyone remembering
    to do it: the old entry is simply not found, and the run reports "no
    baseline" instead of scaling by a figure from a different computation.
    """
    document = {
        "baselines": {
            baselines.baseline_key("mixed-v1", 42, 20_000, "calib-v1"): {
                "placeholder": False,
                "sinkless_orders_per_sec": 1.0,
                "work_index": 2.0,
            }
        }
    }

    v2 = baselines.baseline_key("mixed-v1", 42, 20_000, "calib-v2")
    assert baselines.entry(document, v2) is None


# --------------------------------------------------------------------------
# the quiet-machine gate on recording
# --------------------------------------------------------------------------


def quiet():
    return {
        "power_source": "ac",
        "loadavg": [0.2, 0.3, 0.4],
        "cores": {"logical": 8},
        "core_class": "performance",
        "commit": {"sha": "abc1234", "dirty": False},
    }


def test_a_quiet_machine_may_record():
    assert baselines.quiet_machine_complaints(quiet(), 0.01) == []


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("power_source", "battery", "battery"),
        ("loadavg", [9.0, 9.0, 9.0], "load average"),
        ("core_class", "efficiency", "efficiency core"),
    ],
)
def test_each_condition_that_spoils_a_baseline_is_named(field, value, expected):
    context = quiet()
    context[field] = value

    complaints = baselines.quiet_machine_complaints(context, 0.01)

    assert any(expected in complaint for complaint in complaints), complaints


def test_an_unsteady_machine_may_not_record():
    complaints = baselines.quiet_machine_complaints(quiet(), 0.5)

    assert any("not holding still" in complaint for complaint in complaints)


def test_a_dirty_tree_may_not_record():
    context = quiet()
    context["commit"] = {"sha": "abc1234", "dirty": True}

    complaints = baselines.quiet_machine_complaints(context, 0.01)

    assert any("dirty" in complaint for complaint in complaints)


# --------------------------------------------------------------------------
# the file
# --------------------------------------------------------------------------


def test_a_missing_file_reads_as_empty_rather_than_raising(tmp_path):
    document = baselines.load(tmp_path / "nothing.json")

    assert document["baselines"] == {}


def test_a_file_that_is_not_a_baselines_document_is_refused(tmp_path):
    path = tmp_path / "b.json"
    path.write_text('{"something": "else"}', encoding="utf-8")

    with pytest.raises(ValueError, match="not a baselines document"):
        baselines.load(path)

    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        baselines.load(path)


def test_saved_files_are_readable_in_a_diff(tmp_path):
    path = tmp_path / "b.json"
    baselines.save(path, {"schema": 1, "note": "n", "baselines": {"b": 2, "a": 1}})
    text = path.read_text(encoding="utf-8")

    assert text.endswith("\n")
    assert text.count("\n") > 3, "a one-line blob cannot be reviewed"
    assert text.index('"a"') < text.index('"b"'), "keys should sort"


# --------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------

#: Small enough to keep these two tests well under a second, and still a real
#: run of the real engine through the real entry point.
SMOKE = ["--orders", "400", "--repeats", "1", "--no-sink", "--no-qos"]


def test_rebaseline_records_and_a_later_run_compares_against_it(tmp_path, capsys):
    """The recorded number is what a later run is judged against.

    `--force` because the machine running the suite is not promised to be
    quiet; the gate that would otherwise refuse has its own tests above.
    """
    path = tmp_path / "baselines.json"

    assert main([*SMOKE, "--baselines", str(path), "--rebaseline", "--force"]) == 0

    document = json.loads(path.read_text(encoding="utf-8"))
    key = baselines.baseline_key("mixed-v1", 42, 400, "calib-v1")
    record = document["baselines"][key]
    assert record["placeholder"] is False
    assert record["sinkless_orders_per_sec"] > 0
    assert record["work_index"] > 0
    assert record["calibration_seconds"] > 0
    assert record["provenance"]["commit"]["sha"] is not None

    # A four-fold claim about the baseline is beyond any jitter either way, so
    # these two verdicts are arithmetic rather than luck.
    record["work_index"] *= 4
    baselines.save(path, document)
    assert main([*SMOKE, "--baselines", str(path)]) == 1
    assert "REGRESSION" in capsys.readouterr().err

    record["work_index"] /= 16
    baselines.save(path, document)
    assert main([*SMOKE, "--baselines", str(path)]) == 0


def test_a_faster_run_does_not_silently_rebaseline(tmp_path):
    """The spec's scenario, and the alternative its design rejected.

    Auto-updating on improvement is an invisible ratchet: it hides variance
    and it means nobody ever reviews the number the guard is set at.
    """
    path = tmp_path / "baselines.json"
    main([*SMOKE, "--baselines", str(path), "--rebaseline", "--force"])

    document = json.loads(path.read_text(encoding="utf-8"))
    key = baselines.baseline_key("mixed-v1", 42, 400, "calib-v1")
    document["baselines"][key]["work_index"] /= 8
    baselines.save(path, document)
    before = path.read_bytes()

    assert main([*SMOKE, "--baselines", str(path)]) == 0
    assert path.read_bytes() == before, "a run without --rebaseline must not write"


def test_an_unknown_workload_is_a_usage_error(capsys):
    assert main(["--workload", "no-such", "--repeats", "1"]) == 2
    assert "unknown workload" in capsys.readouterr().err
