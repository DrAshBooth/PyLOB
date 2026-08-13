"""The calibration workload: deterministic, versioned, and shaped like the engine.

The calibration is the denominator ADR-0005 divides by. Three properties have
to hold for it to be worth anything, and each has a test here.

**It must compute the same thing every time.** A denominator that drifted with
the input would make every normalised comparison meaningless. The checksum is
pinned for the same reason the workloads' are: a changed reference computation
means a changed name, or every recorded baseline is scaled by a figure taken
from a computation that no longer exists.

**It must not import the engine.** This is the property that is easiest to
lose by accident and most destructive when lost: if a change to `PyLOB.engine`
could move the calibration, an engine made 30% slower would slow the
denominator too and read as no regression at all. The guard is a source-level
check, because "does not import" is a fact about the module, not about a run.

**No one phase may dominate.** The mix exists so the denominator tracks the
same machine characteristics the engine's hot path depends on. A calibration
that had quietly become 90% `decimal` would measure `decimal` and correct for
the wrong thing.

Nothing here asserts a *duration*. How long a pass takes is the machine's
business, and a test that pinned it would be a test of the machine.
"""

import ast
from pathlib import Path

import pytest
from PyLOB.bench import calibration


def test_a_pass_computes_the_same_checksum_every_time():
    results = [calibration.run("calib-v1") for _ in range(3)]

    assert len({result.checksum for result in results}) == 1


def test_the_checksum_is_pinned_to_the_name():
    """If this fails, `calib-v1` is not the computation baselines were scaled by.

    The fix is a new name -- `calib-v2` -- which changes every baseline key and
    reads as "no baseline recorded" until they are taken again. That is the
    intended cost of changing the reference computation, and it is why the
    constant here is not simply updated.
    """
    result = calibration.run("calib-v1", verify=False)

    assert result.checksum == calibration.CALIBRATIONS["calib-v1"].checksum


def test_a_changed_computation_is_refused_at_run_time(monkeypatch):
    spec = calibration.CALIBRATIONS["calib-v1"]
    monkeypatch.setitem(
        calibration.CALIBRATIONS, "calib-v1", spec._replace(checksum=spec.checksum + 1)
    )

    with pytest.raises(ValueError, match="reference computation has changed"):
        calibration.run("calib-v1")


def test_the_calibration_does_not_import_the_engine():
    """The denominator must not move when the numerator does.

    `_phase_decimal` deliberately reproduces the *shape* of `engine._quantize`
    rather than calling it, so that making the engine's quantizer faster does
    not make the yardstick faster too.
    """
    source = Path(calibration.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # `ast.walk` reaches imports inside functions too, which is where an
    # accidental dependency would most plausibly appear.
    imported = set()
    dynamic = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A bare `from . import x` has no module name but is still an
            # import out of this package.
            imported.add(node.module or "PyLOB.bench")
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("__import__", "import_module"):
                dynamic.append(name)

    assert not [name for name in imported if name.split(".")[0] == "PyLOB"], (
        "calibration imported %r: the reference computation must be "
        "independent of the code it is used to measure" % (sorted(imported),)
    )
    assert not dynamic, (
        "calibration imports dynamically (%r), which the check above cannot "
        "see through" % (dynamic,)
    )
    # Belt and braces: nothing from this project reached the module's globals
    # by any other route either.
    leaked = {
        name
        for name, value in vars(calibration).items()
        if getattr(value, "__module__", "").startswith("PyLOB")
        and not name.startswith("_")
        and getattr(value, "__module__", "") != calibration.__name__
    }
    assert not leaked, "calibration holds %r from the project it measures" % (leaked,)


def test_rate_is_the_reciprocal_of_the_pass_time():
    """Comparisons scale by `rate`, so it must move the way throughput does."""
    result = calibration.CalibrationResult(name="x", seconds=0.05, checksum=1)

    assert result.rate == pytest.approx(20.0)


def test_no_phase_dominates_the_pass():
    """Every phase stays within a factor of two of its intended share.

    Deliberately loose. The exact split is tuning and drifts with the
    interpreter; what must hold is that the mix is still a mix, so that the
    denominator tracks interpreter dispatch, dicts, heaps, floats and decimals
    together rather than becoming a measurement of whichever one grew. A tight
    assertion here would be a test of how busy the machine is.
    """
    budget = calibration.phase_budget()
    best = {}
    for _ in range(3):
        for phase, seconds in calibration.profile("calib-v1").items():
            best[phase] = min(best.get(phase, float("inf")), seconds)
    total = sum(best.values())

    assert set(best) == set(budget)
    for phase, seconds in best.items():
        share = seconds / total
        assert budget[phase] / 2 <= share <= budget[phase] * 2, (
            "phase %r took %.1f%% of the pass, intended %.1f%%"
            % (phase, share * 100, budget[phase] * 100)
        )


def test_every_phase_does_some_work():
    """A phase reduced to a no-op would silently stop measuring its cost."""
    spec = calibration.CALIBRATIONS["calib-v1"]

    for phase, (function, rounds) in spec.phases.items():
        assert rounds > 0, phase
        assert function(rounds) != function(rounds // 2), (
            "phase %r returned the same checksum for half the work: it is not "
            "doing what its iteration count says" % (phase,)
        )


def test_unknown_calibration_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="calib-v1"):
        calibration.run("no-such-calibration")
