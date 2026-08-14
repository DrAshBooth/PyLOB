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

#: The same experiment on both sides, so the arithmetic tests below are about
#: the arithmetic. The tests that vary a fingerprint say so.
SAME = baselines.Fingerprint(
    workload_checksum=1234,
    calibration_checksum=5678,
    interpreter={"implementation": "CPython", "version": "3.11.11", "build": "b"},
)


def compare(measured, calibration_seconds, **kwargs):
    """A run of `measured` orders/sec on a machine of `calibration_seconds`.

    The work index is formed the way `runner` forms it -- the run's own
    throughput times the run's own calibration -- so these tests exercise the
    quantity the harness actually judges on rather than a hand-set one.
    """
    kwargs.setdefault("measured_work_index", measured * calibration_seconds)
    kwargs.setdefault("measured_fingerprint", SAME)
    kwargs.setdefault("baseline_fingerprint", SAME)
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


def test_a_run_that_did_not_hold_still_is_low_confidence():
    """The signal that sees the case the machine-distance ratio cannot.

    A run whose judged quantity moved further across its own repeats than the
    band the verdict is drawn at did not measure one thing three times, and its
    verdict could have gone either way. The two calibration figures can be
    identical while that happens, which is why a ratio of them is not on its
    own a confidence signal.
    """
    steady = compare(BASE_OPS, BASE_CALIB, dispersion=0.05)
    unsteady = compare(BASE_OPS, BASE_CALIB, dispersion=0.50)

    assert steady.confident
    assert steady.speed_ratio == pytest.approx(unsteady.speed_ratio), (
        "the machine looks identical to the ratio in both runs"
    )
    assert not unsteady.confident
    assert any(
        "did not measure one thing" in note for note in unsteady.confidence_notes
    )


def test_the_dispersion_threshold_is_the_tolerance_in_force():
    """Not a constant: the question is whether the verdict could have flipped.

    Dispersion matters relative to the band separating "ok" from "regression",
    so a run tolerated at 40% may disperse further than one judged at 5%.
    """
    assert compare(BASE_OPS, BASE_CALIB, dispersion=0.10, tolerance=0.40).confident
    assert not compare(BASE_OPS, BASE_CALIB, dispersion=0.10, tolerance=0.05).confident


def test_each_reason_for_low_confidence_carries_its_own_number():
    """A boolean says "distrust this" and stops. The report has to say why.

    Both signals can fire at once and they mean different things -- one about
    the machine's distance from the baseline, one about the run's own
    steadiness -- so they are reported as a list rather than collapsed.
    """
    result = compare(BASE_OPS * 0.5, BASE_CALIB / 0.5, dispersion=0.9)

    assert len(result.confidence_notes) == 2
    assert result.dispersion == pytest.approx(0.9)
    assert any("90%" in note for note in result.confidence_notes)
    assert any("0.50x" in note for note in result.confidence_notes)


# --------------------------------------------------------------------------
# fingerprints: a baseline that measured something else cannot judge this run
# --------------------------------------------------------------------------


def test_a_baseline_of_a_different_workload_refuses_to_judge():
    """The hole the recorded checksums were meant to close and did not.

    `mixed-v1`'s composition can be edited without renaming it; the key still
    matches, so the baseline is found and scaled and a verdict comes out. The
    demonstration was a 44% change in trade count reported as `ok`. The
    checksum was recorded on both sides the whole time and nothing compared
    them.
    """
    result = compare(
        BASE_OPS,
        BASE_CALIB,
        baseline_fingerprint=SAME._replace(
            workload_checksum=SAME.workload_checksum + 1
        ),
    )

    assert result.status == "incomparable"
    assert not result.failed, "not a regression: an inability to tell"
    assert result.normalised is None
    assert any("different order stream" in why for why in result.mismatches)


def test_a_baseline_of_a_different_calibration_computation_refuses_to_judge():
    result = compare(
        BASE_OPS,
        BASE_CALIB,
        baseline_fingerprint=SAME._replace(calibration_checksum=9),
    )

    assert result.status == "incomparable"
    assert any("reference computation" in why for why in result.mismatches)


def test_a_baseline_recorded_on_another_interpreter_refuses_to_judge():
    """P1-B, answered honestly rather than completely.

    The judged quantity is a ratio of two Python programs and two CPython
    builds do not agree on it -- the same deliberate defect has earned opposite
    verdicts on two supported builds. Making it portable is a larger job;
    saying so is this one.
    """
    other = dict(SAME.interpreter, build="main Jan 1 2024")

    result = compare(
        BASE_OPS, BASE_CALIB, baseline_fingerprint=SAME._replace(interpreter=other)
    )

    assert result.status == "incomparable"
    assert any("different interpreter" in why for why in result.mismatches)


def test_a_version_difference_alone_is_enough():
    other = dict(SAME.interpreter, version="3.12.1")

    result = compare(
        BASE_OPS, BASE_CALIB, baseline_fingerprint=SAME._replace(interpreter=other)
    )

    assert result.status == "incomparable"


def test_a_baseline_that_records_no_fingerprint_cannot_be_shown_to_match():
    """Absence of evidence is not a pass.

    A record with no checksums is not "compatible with anything"; it is a
    record that cannot say what it measured, and scaling by it is a guess.
    """
    for field in ("workload_checksum", "calibration_checksum", "interpreter"):
        result = compare(
            BASE_OPS, BASE_CALIB, baseline_fingerprint=SAME._replace(**{field: None})
        )

        assert result.status == "incomparable", field


def test_matching_fingerprints_judge_normally():
    """The guard is not a wall: identical experiments compare as before."""
    assert compare(BASE_OPS, BASE_CALIB).status == "ok"
    assert compare(BASE_OPS * 0.5, BASE_CALIB).status == "regression"


# --------------------------------------------------------------------------
# the tolerance is a band, not an off switch
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tolerance", [1.0, 1.5, 0.0, -0.1])
def test_a_tolerance_that_is_not_a_band_is_refused(tolerance):
    """At 1.0 the floor is zero and every run passes, which looks like a setting.

    A guard silently switched off by a plausible-looking number is worse than
    one that is absent, because the absence is visible.
    """
    with pytest.raises(ValueError, match="between 0 and 1"):
        baselines.validate_tolerance(tolerance)

    with pytest.raises(ValueError, match="between 0 and 1"):
        compare(BASE_OPS * 0.01, BASE_CALIB, tolerance=tolerance)


# --------------------------------------------------------------------------
# absent baselines
# --------------------------------------------------------------------------


def test_no_baseline_is_an_absence_and_not_a_regression():
    result = baselines.compare(
        measured=123.0,
        measured_work_index=6.0,
        measured_calibration_seconds=BASE_CALIB,
        measured_fingerprint=SAME,
        baseline=None,
        baseline_work_index=None,
        baseline_calibration_seconds=None,
        baseline_fingerprint=None,
    )

    assert result.status == "no-baseline"
    assert not result.failed
    assert result.normalised is None


def test_a_placeholder_entry_reads_as_no_baseline():
    """A placeholder is an absence. A half-written record is not.

    The second half of that sentence is a change: `entry` used to return None
    for a record that was missing its work index, which reports as "no baseline
    recorded" and exits 0. A file that had lost half its contents would
    therefore turn the guard off and say nothing at all -- the failure the
    marking exists to prevent, arriving by another door. A record that claims
    to be a measurement is now returned whatever state it is in, and
    `record_problems` says what is wrong with it, so a corruption is an error
    and only a genuine absence is benign.
    """
    document = {
        "schema": 1,
        "baselines": {
            "k": {"placeholder": True, "sinkless_orders_per_sec": None},
            "partial": {"placeholder": False, "sinkless_orders_per_sec": 1.0},
        },
    }

    assert baselines.entry(document, "k") is None
    assert baselines.entry(document, "missing") is None
    assert baselines.entry(document, "partial") is not None
    assert any(
        "work_index" in problem
        for problem in baselines.record_problems(baselines.entry(document, "partial"))
    ), "a record without a work index must be named as broken, not as absent"


def well_formed(**overrides):
    """A recorded baseline with every field a comparison needs."""
    record = {
        "placeholder": False,
        "sinkless_orders_per_sec": BASE_OPS,
        "sink_orders_per_sec": 80_000.0,
        "trades": 4_000,
        "work_index": BASE_WORK,
        "work_index_spread": 0.02,
        "calibration": "calib-v1",
        "calibration_seconds": BASE_CALIB,
        "calibration_best_seconds": BASE_CALIB,
        "calibration_checksum": 4717557328431862,
        "calibration_spread": 0.01,
        "workload_checksum": 15240231136383276365,
        "interpreter": {
            "implementation": "CPython",
            "version": "3.11.11",
            "build": "main Mar 11 2025",
        },
        "repeats": 3,
        "tolerance": 0.20,
        "recorded_utc": "2026-08-13T09:00:00+00:00",
        "provenance": {"machine": "somewhere"},
    }
    record.update(overrides)
    return record


def test_a_well_formed_record_has_no_problems():
    assert baselines.record_problems(well_formed()) == []


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"work_index": None}, "work_index is null"),
        ({"sinkless_orders_per_sec": None}, "sinkless_orders_per_sec is null"),
        ({"calibration_seconds": None}, "calibration_seconds is null"),
        ({"interpreter": None}, "interpreter is null"),
        ({"workload_checksum": None}, "workload_checksum is null"),
        ({"work_index": "5000"}, "not int or float"),
        ({"work_index": 0}, "not above zero"),
        ({"calibration_seconds": -1.0}, "not above zero"),
        ({"calibration_seconds": 900.0}, "unit error"),
        ({"tolerance": 1.0}, "tolerance must be between 0 and 1"),
        ({"repeats": 0}, "not above zero"),
        ({"trades": -1}, "negative"),
        (
            {"interpreter": {"implementation": "CPython"}},
            "implementation and a version",
        ),
        ({"work_index": BASE_WORK * 1_000}, "do not describe one run"),
    ],
)
def test_every_way_a_record_is_not_a_baseline_is_named(overrides, expected):
    """A recorded baseline is complete or it is broken; there is no middle.

    Each case here is a file that could plausibly exist -- half-written by an
    interrupted run, hand-edited, truncated, or recorded in milliseconds -- and
    every one of them must be an error rather than something the guard shrugs
    at.
    """
    problems = baselines.record_problems(well_formed(**overrides))

    assert any(expected in problem for problem in problems), problems


def test_a_missing_field_is_named_rather_than_crashing():
    record = well_formed()
    del record["calibration_checksum"]

    assert any(
        "calibration_checksum is missing" in problem
        for problem in baselines.record_problems(record)
    )


def test_the_shipped_baselines_file_is_a_placeholder_or_a_real_baseline():
    """`benchmarks/baselines.json` must never be *half* of either.

    This test used to assert the shipped file **is** a placeholder. That was
    right about the danger and wrong about where it lives: a placeholder must
    never masquerade as a real baseline, but "the file is a placeholder" is a
    fact about where the project has got to, not a property of the system, and
    it stops being true the moment the maintainer records on a quiet machine.
    Pinned as an assertion it made the recording run turn `./verify` red --
    the guard's own test forbidding the guard from being armed.

    So the property, stated as one: **every entry is either a marked
    placeholder holding no measurements, or a recorded baseline complete enough
    to judge a run against.** Both states are legitimate, and the file is never
    allowed to sit between them -- which is what would let a null read as a
    number, or a number hide behind a placeholder marking.
    """
    path = Path(__file__).resolve().parent.parent / "benchmarks" / "baselines.json"
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["schema"] == baselines.SCHEMA
    assert document["baselines"], "the file should still document the key format"
    for key, record in document["baselines"].items():
        if baselines.is_placeholder(record):
            assert baselines.placeholder_problems(record) == [], key
            assert "PLACEHOLDER" in document["note"], (
                "a file that guards nothing has to say so where nobody can miss it"
            )
            assert baselines.entry(document, key) is None, key
        else:
            assert baselines.record_problems(record) == [], key
            assert baselines.entry(document, key) is not None, key


def test_a_placeholder_carrying_a_measurement_is_refused():
    """The half the original test was right about, kept exactly.

    A marked placeholder with a number in it is the masquerade that makes a
    marking worthless: the harness reads the marking and reports NO BASELINE
    while the file looks, to a reader, like a recorded measurement.
    """
    problems = baselines.placeholder_problems(
        {"placeholder": True, "sinkless_orders_per_sec": 100_000.0, "work_index": None}
    )

    assert any("sinkless_orders_per_sec" in problem for problem in problems)


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

#: Small enough to keep these tests well under a second, and still a real run
#: of the real engine through the real entry point.
SMOKE = ["--orders", "400", "--repeats", "1", "--no-sink", "--no-qos"]

KEY = baselines.baseline_key("mixed-v1", 42, 400, "calib-v1")


#: The text a recording run wrote, keyed by the arguments it was given. See
#: `record_baseline`.
_RECORDED: dict[tuple[str, ...], str] = {}


def record_baseline(path, *extra):
    """Record a baseline at `path` and return the document and its record.

    `--force` because the machine running the suite is not promised to be
    quiet; the gate that would otherwise refuse has its own tests above.

    The recording run happens once per set of arguments and its output is
    replayed to later callers. A recording is the most expensive thing in this
    file -- a real engine pass, a real 60 ms calibration pass, and a provenance
    capture that shells out to git -- and a dozen tests below want *a* recorded
    baseline to mutate and judge a later run against, not a *freshly* recorded
    one. They ask for it with identical arguments against an identical empty
    path, so the eleven repeats re-ran one code path on one input.

    Nothing here is stubbed. The first caller performs the whole recording for
    real, and the text every other caller gets is that recording's own output --
    the same text the shipped-file contract is asserted against below. Each
    caller parses its own document from its own file, so the mutations below
    still cannot reach another test. What stopped being repeated is the
    measurement, not any assertion about it.
    """
    text = _RECORDED.get(extra)
    if text is None:
        assert main(
            [*SMOKE, "--baselines", str(path), "--rebaseline", "--force", *extra]
        ) in (
            0,
            1,
        )
        text = _RECORDED[extra] = path.read_text(encoding="utf-8")
    else:
        path.write_text(text, encoding="utf-8")
    document = json.loads(text)
    return document, document["baselines"][KEY]


def scale(path, document, record, factor):
    """Claim the recorded run was `factor` times faster than it was.

    Both figures move together, because both describe one run: an engine four
    times faster does four times the work per calibration pass *and* reports
    four times the throughput. Moving only the work index would leave a record
    whose fields contradict each other, which `record_problems` now refuses --
    correctly, since no measurement could produce it.

    The factors below are twentyfold and not fourfold, and deliberately so:
    two consecutive runs of this 400-order smoke measurement on a contended
    laptop have been seen a factor of 2.2 apart, so a claim only four times the
    truth is a verdict this machine can overturn by being busy. Twenty is
    outside anything jitter reaches, which keeps these assertions arithmetic --
    the property this file's docstring claims for them.
    """
    record["work_index"] *= factor
    record["sinkless_orders_per_sec"] *= factor
    baselines.save(path, document)


def test_rebaseline_records_and_a_later_run_compares_against_it(tmp_path, capsys):
    """The recorded number is what a later run is judged against."""
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)

    assert record["placeholder"] is False
    assert record["sinkless_orders_per_sec"] > 0
    assert record["work_index"] > 0
    assert record["calibration_seconds"] > 0
    assert record["provenance"]["commit"]["sha"] is not None
    assert baselines.record_problems(record) == [], (
        "a freshly recorded baseline must satisfy the contract the shipped "
        "file is held to"
    )

    # A four-fold claim about the baseline is beyond any jitter either way, so
    # these two verdicts are arithmetic rather than luck.
    scale(path, document, record, 20)
    assert main([*SMOKE, "--baselines", str(path)]) == 1
    assert "REGRESSION" in capsys.readouterr().err

    scale(path, document, record, 1 / 400)
    assert main([*SMOKE, "--baselines", str(path)]) == 0


def test_a_recorded_baseline_keeps_the_shipped_files_contract(tmp_path):
    """The blocker, end to end: recording must not break `./verify`.

    A recorded baseline is written by the harness and then held to exactly the
    check `tests` applies to the shipped file. If these two ever disagree, the
    maintainer's recording run turns the suite red and the only way out is to
    un-record it.
    """
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)

    assert not baselines.is_placeholder(record)
    assert baselines.record_problems(record) == []
    assert baselines.entry(document, KEY) is not None
    assert "PLACEHOLDER" not in document["note"]


def test_a_faster_run_does_not_silently_rebaseline(tmp_path):
    """The spec's scenario, and the alternative its design rejected.

    Auto-updating on improvement is an invisible ratchet: it hides variance
    and it means nobody ever reviews the number the guard is set at.
    """
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)
    scale(path, document, record, 1 / 20)
    before = path.read_bytes()

    assert main([*SMOKE, "--baselines", str(path)]) == 0
    assert path.read_bytes() == before, "a run without --rebaseline must not write"


def test_an_unknown_workload_is_a_usage_error(capsys):
    assert main(["--workload", "no-such", "--repeats", "1"]) == 2
    assert "unknown workload" in capsys.readouterr().err


# --------------------------------------------------------------------------
# the guard failing to apply is not the guard passing
# --------------------------------------------------------------------------


def test_a_mislocated_baselines_path_is_an_error_and_not_an_empty_guard(
    tmp_path, capsys
):
    """A typo in `--baselines` used to read as "nothing recorded" and exit 0.

    Which is the worst outcome available: the guard is off, the exit code says
    everything is fine, and the report says so too.
    """
    assert main([*SMOKE, "--baselines", str(tmp_path / "typo.json")]) == 3
    assert "no baselines file" in capsys.readouterr().err


def test_a_corrupt_baselines_file_does_not_read_as_a_regression(tmp_path, capsys):
    """Exit 3, not 1: "I cannot tell" is not "you made it slower"."""
    path = tmp_path / "baselines.json"
    path.write_text("{ this is not json", encoding="utf-8")

    assert main([*SMOKE, "--baselines", str(path)]) == 3
    assert "not valid JSON" in capsys.readouterr().err


def test_a_baselines_path_that_is_a_directory_is_not_a_regression_either(tmp_path):
    """`exists()` is true of a directory, and reading one raises OSError.

    Left uncaught it would be a traceback; caught as a regression it would be a
    lie. It is the same event as a corrupt file: no baseline to apply.
    """
    directory = tmp_path / "baselines.json"
    directory.mkdir()

    assert main([*SMOKE, "--baselines", str(directory)]) == 3


def test_a_half_written_baseline_is_an_error_rather_than_an_absence(tmp_path, capsys):
    """The other way a file can disarm the guard while looking fine."""
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)
    del record["work_index"]
    baselines.save(path, document)

    assert main([*SMOKE, "--baselines", str(path)]) == 3
    assert "not usable" in capsys.readouterr().err


def test_an_edited_workload_fails_loudly_instead_of_reporting_ok(tmp_path, capsys):
    """The demonstrated hole, closed.

    Editing `mixed-v1`'s composition without renaming it changed the trade
    count by 44% and the throughput by 26%, and the harness reported `ok` and
    exited 0 -- the baseline's workload checksum and the run's stream
    fingerprint both sat in the process, uncompared. Here the recorded
    checksum is changed instead, which is the same event from the file's side:
    the baseline measured a stream this run did not.
    """
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)
    record["workload_checksum"] += 1
    baselines.save(path, document)

    assert main([*SMOKE, "--baselines", str(path)]) == 3
    assert "different order stream" in capsys.readouterr().err


def test_a_baseline_from_another_interpreter_refuses_rather_than_misjudging(
    tmp_path, capsys
):
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)
    record["interpreter"] = dict(record["interpreter"], version="3.9.0")
    baselines.save(path, document)

    assert main([*SMOKE, "--baselines", str(path)]) == 3
    assert "different interpreter" in capsys.readouterr().err


def test_a_tolerance_that_disables_the_guard_is_a_usage_error(tmp_path, capsys):
    path = tmp_path / "baselines.json"
    record_baseline(path)

    assert main([*SMOKE, "--baselines", str(path), "--tolerance", "1.0"]) == 2
    assert "between 0 and 1" in capsys.readouterr().err


def test_the_recorded_tolerance_is_used_when_none_is_given(tmp_path):
    """The field was written and never read, which made it a comment.

    A baseline recorded to be judged at a given band is judged at that band by
    a later run that does not say otherwise; a command-line tolerance still
    wins. The two bands are far apart, and the claimed baseline ten times the
    measured one, so which verdict comes out is decided by which tolerance was
    read and not by how busy the machine is.
    """
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)
    scale(path, document, record, 10)
    record["tolerance"] = 0.99
    baselines.save(path, document)

    assert main([*SMOKE, "--baselines", str(path)]) == 0, (
        "a floor at 1% of the baseline cannot be breached by a run a tenth of it"
    )
    assert main([*SMOKE, "--baselines", str(path), "--tolerance", "0.20"]) == 1, (
        "a floor at 80% of the baseline certainly can"
    )


# --------------------------------------------------------------------------
# --rebaseline must not bless a regression
# --------------------------------------------------------------------------


def test_rebaseline_refuses_to_move_the_floor_down(tmp_path, capsys):
    """It used to rewrite the floor while reporting REGRESSION in the same run.

    A floor that follows the measurement down is not a floor; whatever caused
    the drop is written into the guard and never seen again. Moving it *up*
    needs no ceremony -- that is the ordinary case.
    """
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)
    scale(path, document, record, 20)
    before = path.read_bytes()

    assert main([*SMOKE, "--baselines", str(path), "--rebaseline", "--force"]) == 3
    assert path.read_bytes() == before, "the floor must not have moved"
    err = capsys.readouterr().err
    assert "refusing to move the baseline down" in err
    assert "--rebaseline-down" in err


def test_force_does_not_carry_permission_to_move_the_floor_down(tmp_path):
    """`--force` is about the machine, not about the number.

    Conflating them would mean the flag every maintainer types on a laptop also
    silently authorises absorbing a regression.
    """
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)
    scale(path, document, record, 20)

    assert main([*SMOKE, "--baselines", str(path), "--rebaseline", "--force"]) == 3


def test_rebaseline_down_is_the_explicit_opt_in(tmp_path, capsys):
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)
    scale(path, document, record, 20)
    high = json.loads(path.read_text(encoding="utf-8"))["baselines"][KEY]["work_index"]

    # The verdict still stands: the run regressed against the floor it was
    # judged at, and rewriting the floor afterwards does not undo that.
    assert (
        main(
            [
                *SMOKE,
                "--baselines",
                str(path),
                "--rebaseline",
                "--rebaseline-down",
                "--force",
            ]
        )
        == 1
    )
    after = json.loads(path.read_text(encoding="utf-8"))["baselines"][KEY]["work_index"]
    assert after < high, "the floor should have moved down as asked"


def test_rebaseline_always_reports_how_far_the_floor_moved(tmp_path, capsys):
    """A number that changes without a delta is a number nobody reviews."""
    path = tmp_path / "baselines.json"
    document, record = record_baseline(path)
    capsys.readouterr()
    # Halve the recorded floor, so this run re-baselines upward.
    scale(path, document, record, 1 / 20)

    assert main([*SMOKE, "--baselines", str(path), "--rebaseline", "--force"]) == 0

    out = capsys.readouterr().out
    assert "rebaseline" in out
    assert "work index" in out
    assert "%" in out, "the move has to be reported as a proportion, not only raw"


def test_the_first_baseline_says_it_replaced_nothing(tmp_path, capsys):
    path = tmp_path / "baselines.json"

    main([*SMOKE, "--baselines", str(path), "--rebaseline", "--force"])

    assert "nothing replaced" in capsys.readouterr().out


def test_rebaseline_down_without_rebaseline_is_a_usage_error(capsys):
    assert main([*SMOKE, "--rebaseline-down"]) == 2
    assert "means nothing without --rebaseline" in capsys.readouterr().err
