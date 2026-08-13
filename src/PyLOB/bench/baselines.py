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

What must match before any of that means anything
-------------------------------------------------

Scaling one number by another is only defensible when the two describe the
same experiment, so the fingerprints recorded with a baseline are *compared*
and not merely stored. A baseline whose workload checksum, calibration
checksum or interpreter identity differs from the run in front of it measured
something else, and the honest answer is "this baseline cannot judge this run"
-- `status="incomparable"`, its own exit code, no verdict. This is what makes
"changing a workload means a new name" enforcement rather than documentation:
the name is in the key, but a stream edited under an unchanged name keeps its
key and is caught here instead.

The interpreter is in that list because it is the one difference calibration
cannot correct for. The judged quantity is a ratio of two Python programs and
two CPython builds do not agree on it -- the same deliberate engine defect has
earned opposite verdicts on two supported builds. Refusing to compare across
builds is a smaller claim than making the quantity portable, and it is the
true one.

Where normalisation stops working, and what is reported about it
---------------------------------------------------------------

Normalisation is a first-order correction. It assumes the machine's slowdown
is roughly uniform across the kinds of work the calibration mixes; that holds
under mild contention and breaks under severe contention, thermal throttling,
or a run that landed on a different class of core. ADR-0005 is explicit that
such a run "should be reported as low-confidence, not silently scaled".

Two independent signals are reported, and they answer different questions.
Neither is a measurement of the correction's *error*, and this module does not
pretend to have one: the error is the gap between the true unloaded ratio and
the scaled one, and nothing inside a single run observes the true value. What
can be observed is stated, with its number, and left legible.

**Dispersion: did this run measure one thing?** `work_index_spread` is
`(max - min) / median` of the judged quantity across the run's own repeats. It
is compared against the tolerance the verdict is drawn at, because that is the
comparison that matters: a run whose judged quantity moved further across
three repeats than the band separating "ok" from "regression" has not measured
one thing three times, and its verdict could have gone either way. This is the
signal that sees a machine that would not hold still -- the case where the
correction is worst and where a ratio of two calibration figures, however far
apart, sees nothing at all.

**Machine distance: is the correction being asked to reach far?**
`CONFIDENCE_BAND` keeps ADR-0005's own prescription -- a run whose calibration
is more than 25% from the baseline's is flagged -- and it is deliberately
demoted to one named reason among others rather than being *the* confidence
signal, because on its own it is a poor one. It fires hardest on a uniformly
slower machine, which is precisely the case the correction handles perfectly,
and it is silent on a machine whose average speed matches while it thrashes.
It is kept for the one thing it does see: a performance-to-efficiency core
crossing, ~40% by ADR-0005 and 3.16 GHz against 1.04 GHz measured here, where
the E core's smaller caches and narrower issue penalise the dict and heap
phases more than the float ones, so the correction is wrong by an amount that
depends on the mix. Setting the band well inside 40% flags every such crossing.

Both are computed from the same statistic the verdict is: the median. A ratio
built from the *luckiest* calibration pass of a run and the *median* work
index describes no run that happened, and that mismatch is how a run whose
calibration varied by 94% across repeats used to report itself confident.

A low-confidence run still reports its normalised figure, and still fails on a
regression, because suppressing a real regression because the machine was busy
is how a guard becomes decorative. What changes is that the report names which
signal fired and prints its number, and `--fail-on-low-confidence` lets anyone
who needs an admissible measurement demand one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, NamedTuple

__all__ = [
    "CONFIDENCE_BAND",
    "DEFAULT_TOLERANCE",
    "Comparison",
    "Fingerprint",
    "baseline_key",
    "compare",
    "fingerprint_of_record",
    "interpreter_label",
    "is_placeholder",
    "load",
    "placeholder_problems",
    "quiet_machine_complaints",
    "record_problems",
    "save",
    "validate_tolerance",
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

#: A calibration pass is ~60 ms by design. Ten seconds is a machine 160x
#: slower than the one the counts were tuned on, which is not a machine
#: CPython is usefully run on -- so a recorded figure beyond it is a unit
#: error or a typo, not a slow laptop, and reads better as "malformed" than as
#: a baseline nothing can ever clear.
MAX_PLAUSIBLE_CALIBRATION_SECONDS: Final = 10.0

#: How far a record's work index may sit from `orders/sec x calibration
#: seconds` before the fields stop describing one run. Deliberately coarse:
#: the two are different summaries of the same repeats (best-of-N against a
#: median), so they are not equal even on a perfect run, and the check is here
#: to catch a hand-edited or half-written file rather than to police jitter.
MAX_WORK_INDEX_INCONSISTENCY: Final = 8.0


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


class Fingerprint(NamedTuple):
    """*What* was measured, as against what the measurement came to.

    Every field is something that, if it differs, makes one number unable to
    judge the other -- not "slower", but "about something else". They are
    recorded with a baseline and compared on every run; see `mismatches`.
    """

    #: `workloads.checksum` of the exact order stream that was submitted.
    #: The key carries the workload's *name*; this carries its content, which
    #: is what catches a stream edited without a rename.
    workload_checksum: int | None
    #: `calibration.run`'s checksum: the identity of the denominator.
    calibration_checksum: int | None
    #: `provenance.interpreter()`. The one difference calibration cannot
    #: correct for; see the module docstring.
    interpreter: dict[str, Any] | None

    def mismatches(self, other: Fingerprint) -> list[str]:
        """Every reason `self` and `other` are not measurements of one thing.

        `self` is the run, `other` the baseline. An unrecorded field on the
        baseline's side is a mismatch and not a pass: a baseline that cannot
        say what it measured cannot be shown to have measured this.
        """
        problems: list[str] = []
        if other.workload_checksum is None:
            problems.append(
                "the baseline records no workload checksum, so there is no "
                "evidence it measured this order stream"
            )
        elif self.workload_checksum != other.workload_checksum:
            problems.append(
                "workload checksum %s does not match the baseline's %s: the "
                "baseline measured a different order stream under the same "
                "name. A changed workload needs a new name (and a new "
                "baseline), not a new number."
                % (self.workload_checksum, other.workload_checksum)
            )
        if other.calibration_checksum is None:
            problems.append(
                "the baseline records no calibration checksum, so the figure "
                "it would be scaled by cannot be shown to be this computation"
            )
        elif self.calibration_checksum != other.calibration_checksum:
            problems.append(
                "calibration checksum %s does not match the baseline's %s: the "
                "reference computation is not the one this baseline was scaled "
                "by" % (self.calibration_checksum, other.calibration_checksum)
            )
        mine, theirs = _identity(self.interpreter), _identity(other.interpreter)
        if theirs is None:
            problems.append(
                "the baseline records no interpreter identity, and the judged "
                "quantity is not portable across interpreters"
            )
        elif mine != theirs:
            problems.append(
                "baseline recorded on a different interpreter: %s, this run is "
                "%s. The judged quantity is a ratio of two Python programs and "
                "two builds do not agree on it, so this baseline cannot judge "
                "this run."
                % (
                    interpreter_label(other.interpreter),
                    interpreter_label(self.interpreter),
                )
            )
        return problems


def _identity(interpreter: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """The part of an interpreter record that is compared, or None."""
    if not interpreter:
        return None
    implementation = interpreter.get("implementation")
    version = interpreter.get("version")
    if not implementation or not version:
        return None
    return (implementation, version, interpreter.get("build") or "")


def interpreter_label(interpreter: dict[str, Any] | None) -> str:
    """A one-line rendering of `provenance.interpreter()`."""
    if not interpreter:
        return "unknown interpreter"
    return "%s %s (%s)" % (
        interpreter.get("implementation") or "?",
        interpreter.get("version") or "?",
        interpreter.get("build") or "unknown build",
    )


def fingerprint_of_record(record: dict[str, Any]) -> Fingerprint:
    """The fingerprint a recorded baseline carries."""
    return Fingerprint(
        workload_checksum=record.get("workload_checksum"),
        calibration_checksum=record.get("calibration_checksum"),
        interpreter=record.get("interpreter"),
    )


def validate_tolerance(tolerance: float) -> float:
    """A tolerance that is a band, not an off switch.

    At 1.0 the floor is zero and every run clears it, so `--tolerance 1` is
    indistinguishable from having no guard while looking like a setting. At or
    below 0 the floor is at or above the baseline and every run fails. Both are
    refused rather than obeyed.
    """
    if not 0.0 < tolerance < 1.0:
        raise ValueError(
            "tolerance must be between 0 and 1 (exclusive), got %r: at 1.0 the "
            "floor is zero and the guard is off, and at 0 or below nothing can "
            "pass" % (tolerance,)
        )
    return tolerance


class Comparison(NamedTuple):
    """The verdict on one run against one baseline."""

    #: "ok" | "regression" | "no-baseline" | "incomparable".
    #: `no-baseline` when nothing is recorded for this key, or the recorded
    #: value is a placeholder: not a regression, an absence.
    #: `incomparable` when a baseline exists but describes a different
    #: experiment -- also not a regression, and not a pass either.
    status: str
    measured: float
    baseline: float | None
    normalised: float | None
    #: measured machine speed / baseline machine speed, by calibration.
    #: Below 1 means this machine is slower than the one that recorded it.
    speed_ratio: float | None
    floor: float | None
    tolerance: float
    confident: bool
    #: One entry per reason confidence is low, each carrying its number.
    #: Empty when confident. See the module docstring for why there is a list
    #: here and not a single ratio.
    confidence_notes: tuple[str, ...]
    #: `(max - min) / median` of the run's per-repeat work indices, when the
    #: run reported it. None means the run did not say.
    dispersion: float | None
    #: Why this baseline cannot judge this run. Non-empty iff `incomparable`.
    mismatches: tuple[str, ...]
    reason: str

    @property
    def failed(self) -> bool:
        return self.status == "regression"

    @property
    def incomparable(self) -> bool:
        """The guard could not be applied. Distinct from passing it."""
        return self.status == "incomparable"


def compare(
    *,
    measured: float,
    measured_work_index: float,
    measured_calibration_seconds: float,
    measured_fingerprint: Fingerprint,
    baseline: float | None,
    baseline_work_index: float | None,
    baseline_calibration_seconds: float | None,
    baseline_fingerprint: Fingerprint | None,
    tolerance: float = DEFAULT_TOLERANCE,
    confidence_band: float = CONFIDENCE_BAND,
    dispersion: float | None = None,
) -> Comparison:
    """Judge `measured` against `baseline`, normalised by calibration.

    `measured` and `baseline` are orders/sec, reported for humans. The
    judgment is made on the *work indices* -- orders processed per calibration
    pass -- because each of those is a ratio of two figures measured in the
    same moment, and it is that pairing rather than the arithmetic that makes
    contention cancel. `runner` explains what goes wrong when the two factors
    come from different moments.

    The fingerprints are required arguments and not optional ones. Recording
    evidence and never reading it is how the versioning rule became prose; a
    caller that has not decided what this run was *of* cannot get a verdict
    out of this function at all.

    `measured_calibration_seconds` is expected to be the run's *median*, the
    same statistic `measured_work_index` is, and it is used only for the
    machine-distance signal. `dispersion` is the run's own `(max - min) /
    median` work index spread, which is the signal that sees a machine that
    would not hold still; None means the run did not report one.
    """
    validate_tolerance(tolerance)
    if baseline is None or not baseline_work_index or not baseline_calibration_seconds:
        return _absent(measured, tolerance, "no baseline recorded for this key")
    if measured_calibration_seconds <= 0:
        raise ValueError("calibration seconds must be positive")
    if measured_work_index <= 0:
        raise ValueError("work index must be positive")

    mismatches = measured_fingerprint.mismatches(
        baseline_fingerprint
        if baseline_fingerprint is not None
        else Fingerprint(None, None, None)
    )
    if mismatches:
        return Comparison(
            status="incomparable",
            measured=measured,
            baseline=baseline,
            normalised=None,
            speed_ratio=None,
            floor=None,
            tolerance=tolerance,
            confident=False,
            confidence_notes=(),
            dispersion=dispersion,
            mismatches=tuple(mismatches),
            reason=(
                "this baseline cannot judge this run: %s" % ("; ".join(mismatches),)
            ),
        )

    speed_ratio = baseline_calibration_seconds / measured_calibration_seconds
    # The run's throughput expressed in the baseline machine's units.
    normalised = baseline * (measured_work_index / baseline_work_index)
    floor = baseline * (1.0 - tolerance)

    notes: list[str] = []
    # Dispersion first: it is the signal that tracks whether this run measured
    # one thing, and it is read against the band the verdict is drawn at.
    if dispersion is not None and dispersion > tolerance:
        notes.append(
            "the judged quantity moved %.0f%% across this run's repeats, more "
            "than the %.0f%% tolerance the verdict is drawn at: this run did "
            "not measure one thing, and the verdict could have gone either way"
            % (dispersion * 100, tolerance * 100)
        )
    if not (1.0 / confidence_band) <= speed_ratio <= confidence_band:
        notes.append(
            "this machine measures %.2fx the baseline's speed, outside the "
            "%.2fx band in which linear scaling can be trusted (a "
            "performance-to-efficiency core crossing looks like this)"
            % (speed_ratio, confidence_band)
        )

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
    if notes:
        reason += "; LOW CONFIDENCE: %s" % ("; ".join(notes),)
    return Comparison(
        status=status,
        measured=measured,
        baseline=baseline,
        normalised=normalised,
        speed_ratio=speed_ratio,
        floor=floor,
        tolerance=tolerance,
        confident=not notes,
        confidence_notes=tuple(notes),
        dispersion=dispersion,
        mismatches=(),
        reason=reason,
    )


def _absent(measured: float, tolerance: float, reason: str) -> Comparison:
    return Comparison(
        status="no-baseline",
        measured=measured,
        baseline=None,
        normalised=None,
        speed_ratio=None,
        floor=None,
        tolerance=tolerance,
        confident=True,
        confidence_notes=(),
        dispersion=None,
        mismatches=(),
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


def is_placeholder(record: Any) -> bool:
    """Whether `record` claims to be a placeholder rather than a measurement."""
    return isinstance(record, dict) and bool(record.get("placeholder"))


def entry(document: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The recorded entry for `key`, or None when it is absent or a placeholder.

    A placeholder reads as an absence rather than as a zero, which is the
    difference between "nothing to compare against" and "you have regressed to
    nothing".

    A record that claims to be a measurement is returned *whatever state it is
    in*, and `record_problems` says what is wrong with it. Returning None for a
    half-written record -- as this once did -- reported it as "no baseline
    recorded", which exits 0: a file that had lost half its contents would
    quietly disarm the guard and say nothing. An absence and a corruption are
    different events and only one of them is benign.
    """
    record = document.get("baselines", {}).get(key)
    if not isinstance(record, dict):
        return None
    if is_placeholder(record):
        return None
    return record


#: Fields a recorded (non-placeholder) baseline must carry, and what each must
#: be. `sink_orders_per_sec` is deliberately absent: `--no-sink` is a supported
#: way to record, and the sink figure is reported and never gating.
_REQUIRED: Final[tuple[tuple[str, type | tuple[type, ...]], ...]] = (
    ("sinkless_orders_per_sec", (int, float)),
    ("work_index", (int, float)),
    ("trades", int),
    ("calibration", str),
    ("calibration_seconds", (int, float)),
    ("calibration_checksum", int),
    ("workload_checksum", int),
    ("repeats", int),
    ("tolerance", (int, float)),
    ("recorded_utc", str),
    ("interpreter", dict),
    ("provenance", dict),
)

#: Of those, the ones that are meaningless at or below zero.
_POSITIVE: Final = (
    "sinkless_orders_per_sec",
    "work_index",
    "calibration_seconds",
    "repeats",
)


def record_problems(record: Any) -> list[str]:
    """Every reason `record` is not a baseline anything can be judged against.

    Empty means well-formed: every required field present, of the right type,
    not null, positive where positivity is the only meaningful reading, a
    plausible calibration figure, and figures that describe one run rather than
    several.

    This is the other half of "a placeholder must never masquerade as a real
    baseline": the first half is that a placeholder says so, and this is that
    anything *not* saying so is complete enough to guard with. A file that is
    half-written, hand-edited or truncated fails here, loudly, instead of
    reading as an absence and exiting 0.
    """
    if not isinstance(record, dict):
        return ["not a JSON object"]
    if is_placeholder(record):
        return ["marked as a placeholder, so it holds no measurement"]

    problems: list[str] = []
    for field, kinds in _REQUIRED:
        if field not in record:
            problems.append("%s is missing" % (field,))
            continue
        value = record[field]
        if value is None:
            problems.append("%s is null" % (field,))
        elif isinstance(value, bool) or not isinstance(value, kinds):
            problems.append(
                "%s is %r, which is not %s"
                % (
                    field,
                    value,
                    " or ".join(
                        kind.__name__
                        for kind in (kinds if isinstance(kinds, tuple) else (kinds,))
                    ),
                )
            )
    if problems:
        return problems

    for field in _POSITIVE:
        if record[field] <= 0:
            problems.append(
                "%s is %r, which is not above zero" % (field, record[field])
            )
    if record["trades"] < 0:
        problems.append("trades is %r, which is negative" % (record["trades"],))
    if record["calibration_seconds"] > MAX_PLAUSIBLE_CALIBRATION_SECONDS:
        problems.append(
            "calibration_seconds is %r, beyond the %.0fs any machine CPython "
            "runs on should take over a pass: this is a unit error, not a slow "
            "laptop"
            % (record["calibration_seconds"], MAX_PLAUSIBLE_CALIBRATION_SECONDS)
        )
    try:
        validate_tolerance(record["tolerance"])
    except ValueError as exc:
        problems.append(str(exc))
    if _identity(record["interpreter"]) is None:
        problems.append(
            "interpreter does not name an implementation and a version, so a "
            "later run cannot tell whether it is the same one"
        )
    if problems:
        return problems

    # Do the figures describe one run? `work_index` is orders per calibration
    # pass, so it should sit near `orders/sec x calibration seconds`. They are
    # different summaries of the same repeats and so are never exactly equal;
    # an order of magnitude apart means the file was edited, not measured.
    expected = record["sinkless_orders_per_sec"] * record["calibration_seconds"]
    ratio = record["work_index"] / expected if expected else float("inf")
    if not 1.0 / MAX_WORK_INDEX_INCONSISTENCY <= ratio <= MAX_WORK_INDEX_INCONSISTENCY:
        problems.append(
            "work_index %r is %.3gx orders/sec x calibration_seconds (%r): "
            "these fields do not describe one run"
            % (record["work_index"], ratio, expected)
        )
    return problems


def placeholder_problems(record: Any) -> list[str]:
    """Every way a record marked `placeholder` is pretending to hold a number.

    The complement of `record_problems`. A placeholder exists to document the
    schema and to guard nothing; a placeholder carrying a measured value would
    be the exact masquerade the marking is there to prevent.
    """
    if not isinstance(record, dict):
        return ["not a JSON object"]
    if not is_placeholder(record):
        return ["not marked as a placeholder"]
    return [
        "%s is %r, but a placeholder holds no measurements" % (field, record[field])
        for field in (
            "sinkless_orders_per_sec",
            "sink_orders_per_sec",
            "work_index",
            "calibration_seconds",
        )
        if record.get(field) is not None
    ]


def save(path: Path, document: dict[str, Any]) -> None:
    """Write a baselines document, formatted for review in a diff.

    Two spaces of indent, sorted keys and a trailing newline: a re-baseline is
    meant to be read in a pull request, and a one-line JSON blob is not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
