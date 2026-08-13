"""Recorded baselines, and the calibrated comparison ADR-0005 judges against.

A baseline is a number plus the calibration figure it was taken with. A run is
compared to it *after scaling by the ratio of the two calibrations*, so a
machine that is uniformly 30% slower reads as no regression rather than as a
30% one. The regression question is "did the engine get slower relative to the
machine it ran on", which is a question about the code; "is this number lower
than that number" is a question about two laptops.

Every function here is pure and takes numbers, not clocks. That is deliberate:
the central claim of ADR-0005 is an arithmetic one, and it should be provable
by a test that never measures anything and therefore never flakes.

The normalisation, in one line
------------------------------

Calibration `rate` is passes per second, so it moves the same way orders/sec
does -- a slower machine has a lower rate *and* a lower throughput. Scaling by
the ratio cancels the machine::

    normalised = measured x (baseline_rate / measured_rate)
               = measured x (measured_seconds / baseline_seconds)

On a machine 30% slower than the one that recorded the baseline, `measured` is
0.7x the baseline and the ratio is 1/0.7, so `normalised` lands back on the
baseline and the run passes. On the *same* machine, the ratio is 1 and the
comparison is the plain one.

Where it stops working, and what to do about it
-----------------------------------------------

Normalisation is a first-order correction. It assumes the machine's slowdown
is roughly uniform across the kinds of work the calibration mixes; that holds
under mild contention and breaks under severe contention, thermal throttling,
or a run that landed on a different class of core. ADR-0005 is explicit that
such a run "should be reported as low-confidence, not silently scaled".

`CONFIDENCE_BAND` is where that line is drawn: a run passes as confident while
its calibration is within 25% of the baseline's, either way.

Twenty-five percent is chosen from the two things it has to sit between.

- **Below it: ordinary jitter.** Best-of-N calibration on a lightly loaded
  machine repeats within a few percent. A band of 25% does not fire on noise,
  so `LOW CONFIDENCE` stays a signal rather than furniture.
- **Above it: the P/E core gap.** ADR-0005 quotes ~40% for a run that lands on
  an efficiency core, and this machine measures the two clusters at 3.16 GHz
  against 1.04 GHz. A P/E crossing is exactly where uniform-slowdown fails --
  the efficiency core's smaller caches and narrower issue penalise the dict
  and heap phases more than the float ones, so the correction is wrong by an
  amount that depends on the mix. Setting the band well inside 40% means every
  such crossing is flagged rather than scaled.

So the band is not "how much slowdown is acceptable" -- it is "how far the
linear correction can be trusted". A low-confidence run still reports its
normalised figure, and still fails on a regression, because suppressing a real
regression because the machine was busy is how a guard becomes decorative.
What changes is that the report says so, loudly, and `--fail-on-low-confidence`
lets anyone who needs an admissible measurement demand one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, NamedTuple

__all__ = [
    "CONFIDENCE_BAND",
    "DEFAULT_TOLERANCE",
    "Comparison",
    "baseline_key",
    "compare",
    "load",
    "quiet_machine_complaints",
    "save",
]

#: The change's own decision, restated by ADR-0005: 20% by default, applied to
#: the normalised value, overridable on the command line. Laptop jitter is real
#: and a tighter band would cry wolf; a looser one would let a real regression
#: through as noise.
DEFAULT_TOLERANCE: Final = 0.20

#: How far the calibration may move before the linear correction stops being
#: trustworthy. See the module docstring for the derivation.
CONFIDENCE_BAND: Final = 1.25

SCHEMA: Final = 1

#: What `--rebaseline` insists on before it will record a number, unless
#: `--force`. Each is a condition ADR-0005 names as a reason a measurement is
#: not worth keeping.
MAX_LOAD_PER_CORE: Final = 0.5
MAX_CALIBRATION_SPREAD: Final = 0.10


def baseline_key(workload: str, seed: int, orders: int, calibration: str) -> str:
    """The (workload, config) key a baseline is filed under.

    The calibration name is part of the key, not part of the value. ADR-0005
    makes a renamed calibration require a re-baseline; putting the name in the
    key makes that happen by itself -- a run under `calib-v2` simply does not
    find `calib-v1`'s baseline, and reports "no baseline recorded" instead of
    silently scaling by a figure from a different reference computation.

    The seed and the order count are in the key for the same reason: they
    change what was measured, so they must not share a slot with it.
    """
    return "%s|seed=%d|orders=%d|calib=%s" % (workload, seed, orders, calibration)


class Comparison(NamedTuple):
    """The verdict on one run against one baseline."""

    #: None when nothing is recorded for this key, or the recorded value is a
    #: placeholder. Not a regression -- an absence.
    status: str  # "ok" | "regression" | "no-baseline"
    measured: float
    baseline: float | None
    normalised: float | None
    #: measured machine speed / baseline machine speed, by calibration.
    #: Below 1 means this machine is slower than the one that recorded it.
    speed_ratio: float | None
    floor: float | None
    tolerance: float
    confident: bool
    reason: str

    @property
    def failed(self) -> bool:
        return self.status == "regression"


def compare(
    *,
    measured: float,
    measured_work_index: float,
    measured_calibration_seconds: float,
    baseline: float | None,
    baseline_work_index: float | None,
    baseline_calibration_seconds: float | None,
    tolerance: float = DEFAULT_TOLERANCE,
    confidence_band: float = CONFIDENCE_BAND,
) -> Comparison:
    """Judge `measured` against `baseline`, normalised by calibration.

    `measured` and `baseline` are orders/sec, reported for humans. The
    judgment is made on the *work indices* -- orders processed per calibration
    pass -- because each of those is a ratio of two figures measured in the
    same moment, and it is that pairing rather than the arithmetic that makes
    contention cancel. `runner` explains what goes wrong when the two factors
    come from different moments.

    The calibration seconds are used only for the confidence ratio: how
    different this machine is from the one that recorded the baseline.
    """
    if baseline is None or not baseline_work_index or not baseline_calibration_seconds:
        return Comparison(
            status="no-baseline",
            measured=measured,
            baseline=None,
            normalised=None,
            speed_ratio=None,
            floor=None,
            tolerance=tolerance,
            confident=True,
            reason="no baseline recorded for this key",
        )
    if measured_calibration_seconds <= 0:
        raise ValueError("calibration seconds must be positive")
    if measured_work_index <= 0:
        raise ValueError("work index must be positive")

    speed_ratio = baseline_calibration_seconds / measured_calibration_seconds
    # The run's throughput expressed in the baseline machine's units.
    normalised = baseline * (measured_work_index / baseline_work_index)
    floor = baseline * (1.0 - tolerance)
    confident = (1.0 / confidence_band) <= speed_ratio <= confidence_band

    if normalised < floor:
        reason = (
            "normalised %.0f/s is below the %.0f/s floor "
            "(baseline %.0f/s less %.0f%% tolerance)"
            % (normalised, floor, baseline, tolerance * 100)
        )
        status = "regression"
    else:
        reason = "normalised %.0f/s clears the %.0f/s floor" % (normalised, floor)
        status = "ok"
    if not confident:
        reason += (
            "; LOW CONFIDENCE: this machine measures %.2fx the baseline's speed, "
            "outside the %.2fx band in which scaling can be trusted"
            % (speed_ratio, confidence_band)
        )
    return Comparison(
        status=status,
        measured=measured,
        baseline=baseline,
        normalised=normalised,
        speed_ratio=speed_ratio,
        floor=floor,
        tolerance=tolerance,
        confident=confident,
        reason=reason,
    )


def quiet_machine_complaints(
    provenance: dict[str, Any], calibration_spread: float | None
) -> list[str]:
    """Reasons this machine should not be recording a baseline right now.

    ADR-0005's whole justification for recording baselines on a maintainer's
    laptop is that calibration makes them portable. That argument holds only
    if the recorded number was itself taken under decent conditions -- a
    baseline recorded on a throttled efficiency core is a bad denominator for
    everyone forever, and no amount of later normalisation repairs it.

    Empty means go ahead. `--rebaseline` refuses on a non-empty list unless
    `--force`.
    """
    complaints: list[str] = []
    if provenance.get("power_source") == "battery":
        complaints.append("running on battery: sustained clocks are not the mains ones")
    loadavg = provenance.get("loadavg")
    cores = (provenance.get("cores") or {}).get("logical") or 1
    if loadavg and loadavg[0] / cores > MAX_LOAD_PER_CORE:
        complaints.append(
            "load average %.2f over %d cores is above %.2f per core"
            % (loadavg[0], cores, MAX_LOAD_PER_CORE)
        )
    if provenance.get("core_class") == "efficiency":
        complaints.append("the run landed on an efficiency core")
    if calibration_spread is not None and calibration_spread > MAX_CALIBRATION_SPREAD:
        complaints.append(
            "calibration varied %.1f%% across repeats, above %.0f%%: the machine "
            "is not holding still"
            % (calibration_spread * 100, MAX_CALIBRATION_SPREAD * 100)
        )
    commit = provenance.get("commit") or {}
    if commit.get("dirty"):
        complaints.append(
            "the working tree is dirty, so the baseline names a commit that "
            "does not describe the code measured"
        )
    return complaints


_PLACEHOLDER_NOTE: Final = (
    "PLACEHOLDER -- no baseline has been recorded. Every value below is null "
    "on purpose. ADR-0005 makes a baseline only meaningful alongside the "
    "calibration figure taken with it, and both must come from a quiet "
    "machine on mains power; recording them is a deliberate act on such a "
    "machine, not a side effect of writing the harness. Until then the "
    "harness reports NO BASELINE and exits 0 rather than pretending to guard. "
    "To record: `uv run python -m PyLOB.bench --rebaseline`, which refuses on "
    "a contended machine and rewrites this file so the change is reviewable "
    "in the diff."
)


def empty(note: str = _PLACEHOLDER_NOTE) -> dict[str, Any]:
    """A baselines document with no measurements in it."""
    return {"schema": SCHEMA, "note": note, "baselines": {}}


def load(path: Path) -> dict[str, Any]:
    """The baselines document at `path`, or an empty one if it is not there."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty()
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("%s is not valid JSON: %s" % (path, exc)) from exc
    if not isinstance(document, dict) or "baselines" not in document:
        raise ValueError("%s is not a baselines document" % (path,))
    return document


def entry(document: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The recorded entry for `key`, or None when it is absent or a placeholder.

    A placeholder reads as an absence rather than as a zero, which is the
    difference between "nothing to compare against" and "you have regressed to
    nothing".
    """
    record = document.get("baselines", {}).get(key)
    if not isinstance(record, dict):
        return None
    if record.get("placeholder"):
        return None
    # A record missing either figure cannot be compared against: the
    # orders/sec is what gets reported and the work index is what gets judged.
    if record.get("sinkless_orders_per_sec") is None:
        return None
    if record.get("work_index") is None:
        return None
    return record


def save(path: Path, document: dict[str, Any]) -> None:
    """Write a baselines document, formatted for review in a diff.

    Two spaces of indent, sorted keys and a trailing newline: a re-baseline is
    meant to be read in a pull request, and a one-line JSON blob is not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
