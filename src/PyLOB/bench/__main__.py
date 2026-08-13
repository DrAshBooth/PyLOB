"""`python -m PyLOB.bench`: measure, compare, and say whether it got slower.

Exit codes are the interface, because the point of the harness is that one
command answers "did I make it slower?" without anyone reading prose:

    0   no regression, or nothing recorded to compare against
    1   a regression against the recorded baseline (or, with
        `--fail-on-low-confidence`, a measurement not worth trusting)
    2   the command line was wrong (argparse's own code)
    3   the guard could not be applied at all

Three is separate from one on purpose. "You made it slower" and "I could not
find out whether you made it slower" are different facts, and a caller that
cannot tell them apart -- CI, a bisect script, a pre-push hook -- will read the
second as the first and go looking for a performance bug that does not exist.
It covers a baselines file that is missing, unreadable or half-written, and a
baseline that measured a different workload, a different reference computation
or a different interpreter. Every one of those means *this baseline cannot
judge this run*, which is not a verdict.

`--rebaseline` does not change the verdict. A regression still exits 1 even
when the floor has just been rewritten, because the run did regress and the
rewriting was a separate deliberate act.

There is no `--engine` flag. The change this implements specified
`--engine new|legacy`; ADR-0003 deleted the legacy engine, so the flag would
have one legal value and would exist only to be typed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Final, Sequence

from . import baselines as baselines_module
from . import calibration as calibration_module
from . import runner as runner_module
from . import workloads as workloads_module

#: Relative to the repository root, which is four directories above this file
#: (`src/PyLOB/bench/__main__.py`).
_DEFAULT_BASELINES = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "benchmarks"
    / "baselines.json"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m PyLOB.bench",
        description=(
            "Measure the matching engine against a calibrated baseline "
            "(ADR-0005). Not part of ./verify."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes:\n"
            "  0  no regression, or no baseline recorded yet\n"
            "  1  regression against the recorded baseline\n"
            "  2  bad usage\n"
            "  3  the guard could not be applied: the baselines file is\n"
            "     missing, unreadable or half-written, or the baseline\n"
            "     measured a different workload, calibration or interpreter\n"
        ),
    )
    parser.add_argument(
        "--workload",
        default="mixed-v1",
        help="workload name (default: %(default)s). A name is a promise about "
        "a composition: changing one means a new name.",
    )
    parser.add_argument("--seed", type=int, default=workloads_module.CANONICAL_SEED)
    parser.add_argument(
        "--orders",
        type=int,
        default=None,
        help="override the workload's canonical order count (changes the "
        "baseline key, and skips the stream checksum)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="timed passes; the best of each is kept (default: %(default)s)",
    )
    parser.add_argument(
        "--calibration",
        default="calib-v1",
        help="reference computation used to normalise (default: %(default)s)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=None,
        help="fraction below baseline that counts as a regression. Must be "
        "between 0 and 1. Defaults to the tolerance recorded with the "
        "baseline, or %s when there is none." % (baselines_module.DEFAULT_TOLERANCE,),
    )
    parser.add_argument(
        "--baselines",
        type=Path,
        default=_DEFAULT_BASELINES,
        help="baselines file (default: benchmarks/baselines.json). A path that "
        "does not exist is an error, not an empty guard.",
    )
    parser.add_argument(
        "--rebaseline",
        action="store_true",
        help="record this run as the baseline, rewriting the file. Refuses on "
        "a machine that is not quiet unless --force, and refuses to move the "
        "floor down without --rebaseline-down.",
    )
    parser.add_argument(
        "--rebaseline-down",
        action="store_true",
        help="with --rebaseline, allow the new baseline to be *lower* than the "
        "recorded one. Moving a floor down absorbs whatever made it move.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --rebaseline, record even though the machine is contended. "
        "Does not permit a downward move: that is --rebaseline-down.",
    )
    parser.add_argument(
        "--fail-on-low-confidence",
        action="store_true",
        help="exit non-zero on a measurement not worth trusting, even without "
        "a regression: the judged quantity moved more across this run's "
        "repeats than the tolerance, or the machine is too far from the "
        "baseline's for scaling to be trusted",
    )
    parser.add_argument(
        "--no-sink",
        action="store_true",
        help="skip the sink-attached figure (it is reported, never gating)",
    )
    parser.add_argument(
        "--no-qos",
        action="store_true",
        help="do not ask the scheduler for a performance core",
    )
    parser.add_argument(
        "--machine-label",
        default=None,
        help="what to record instead of this machine's hostname",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument(
        "--list", action="store_true", help="list workloads and calibrations, then exit"
    )
    return parser


def _list() -> int:
    print("workloads:")
    for name, spec in sorted(workloads_module.WORKLOADS.items()):
        print("  %-12s %s" % (name, spec.description))
        print(
            "  %-12s %d orders, tick %s, %d traders"
            % ("", spec.orders, spec.tick_size, spec.traders)
        )
    print("calibrations:")
    for name, spec in sorted(calibration_module.CALIBRATIONS.items()):
        print("  %-12s %s" % (name, spec.description))
        print("  %-12s phases: %s" % ("", ", ".join(spec.phases)))
    return 0


def _record(result: runner_module.BenchResult, tolerance: float) -> dict[str, Any]:
    return {
        "placeholder": False,
        "sinkless_orders_per_sec": round(result.sinkless.orders_per_sec, 1),
        "sink_orders_per_sec": (
            None if result.sink is None else round(result.sink.orders_per_sec, 1)
        ),
        "trades": result.sinkless.trades,
        # Orders per calibration pass, median over repeats. This is what a
        # later run is actually judged against; the orders/sec above is the
        # human-readable face of it.
        "work_index": round(result.work_index, 2),
        "work_index_spread": round(result.work_index_spread, 4),
        "calibration": result.calibration.name,
        # The *median*, matching the median work index above, because these two
        # are the pair a later comparison forms its ratios from and a ratio
        # built from two different statistics describes no run that happened.
        "calibration_seconds": round(result.calibration_median_seconds, 6),
        # Best of N, for the report only. Never compared against.
        "calibration_best_seconds": round(result.calibration.seconds, 6),
        "calibration_checksum": result.calibration.checksum,
        "calibration_spread": round(result.calibration_spread, 4),
        "workload_checksum": result.workload_checksum,
        # Lifted out of the provenance below, where it is also recorded,
        # because it is load-bearing rather than explanatory: `compare` refuses
        # across a mismatch, so it is part of the contract of a record and not
        # part of the story around one.
        "interpreter": result.provenance.get("interpreter"),
        "repeats": result.repeats,
        "tolerance": tolerance,
        "recorded_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "provenance": result.provenance,
    }


def _delta(previous: dict[str, Any] | None, result: runner_module.BenchResult) -> str:
    """How far `--rebaseline` is about to move the floor, in words.

    Always printed, in either direction. A guard whose floor moves without
    saying how far is a guard nobody can review in the diff, and the diff is
    the only place a re-baseline is ever looked at.
    """
    if previous is None or not previous.get("work_index"):
        return "first baseline for this key: work index %.0f (nothing replaced)" % (
            result.work_index,
        )
    old = previous["work_index"]
    new = result.work_index
    return "work index %.0f -> %.0f (%+.1f%%); reported throughput %.0f/s -> %.0f/s" % (
        old,
        new,
        (new - old) / old * 100.0,
        previous.get("sinkless_orders_per_sec") or 0.0,
        result.sinkless.orders_per_sec,
    )


def _report(
    result: runner_module.BenchResult,
    comparison: baselines_module.Comparison,
    key: str,
) -> None:
    context = result.provenance
    print(
        "workload    %s seed=%d orders=%d (best of %d)"
        % (
            result.workload,
            result.seed,
            result.orders,
            result.repeats,
        )
    )
    print(
        "machine     %s, %s, %s"
        % (
            context.get("machine"),
            context.get("cpu"),
            context.get("python"),
        )
    )
    cores = context.get("cores") or {}
    core_class = context.get("core_class") or "unknown"
    print(
        "cores       %s logical (%s performance / %s efficiency); this run: %s%s"
        % (
            cores.get("logical"),
            cores.get("performance"),
            cores.get("efficiency"),
            core_class,
            ""
            if context.get("effective_ghz") is None
            else " at %.2f GHz" % context["effective_ghz"],
        )
    )
    commit = context.get("commit") or {}
    print(
        "power       %s; load %s; qos %s"
        % (
            context.get("power_source"),
            context.get("loadavg"),
            context.get("qos"),
        )
    )
    print(
        "commit      %s%s on %s"
        % (
            commit.get("sha"),
            " (dirty)" if commit.get("dirty") else "",
            commit.get("branch"),
        )
    )
    print(
        "interpreter %s"
        % (baselines_module.interpreter_label(context.get("interpreter")),)
    )
    print(
        "calibration %s %.4fs per pass (median %.4fs), spread %.1f%% across repeats"
        % (
            result.calibration.name,
            result.calibration.seconds,
            result.calibration_median_seconds,
            result.calibration_spread * 100,
        )
    )
    print()
    print(
        "sinkless    %9.0f orders/sec   %d trades in %.4fs   [best of %d]"
        % (
            result.sinkless.orders_per_sec,
            result.sinkless.trades,
            result.sinkless.seconds,
            result.repeats,
        )
    )
    if result.sink is not None:
        print(
            "sqlite sink %9.0f orders/sec   %d trades in %.4fs   [reported]"
            % (
                result.sink.orders_per_sec,
                result.sink.trades,
                result.sink.seconds,
            )
        )
    print(
        "work index  %9.0f orders per calibration pass   "
        "[median of %d, spread %.1f%%, GATING]"
        % (result.work_index, result.repeats, result.work_index_spread * 100)
    )
    print()
    print("baseline    %s" % key)
    if comparison.status == "no-baseline":
        print("            NO BASELINE recorded. Nothing to guard against yet;")
        print("            `--rebaseline` records one, on a quiet machine.")
        return
    if comparison.incomparable:
        print("            INCOMPARABLE: a baseline is recorded, and it cannot")
        print("            judge this run.")
        for mismatch in comparison.mismatches:
            print("            - %s" % (mismatch,))
        return
    print(
        "            recorded %.0f/s; this machine measures %.2fx its speed"
        % (
            comparison.baseline,
            comparison.speed_ratio,
        )
    )
    print(
        "            normalised %.0f/s against a floor of %.0f/s (%.0f%% tolerance)"
        % (
            comparison.normalised,
            comparison.floor,
            comparison.tolerance * 100,
        )
    )
    verdict = "REGRESSION" if comparison.failed else "ok"
    if not comparison.confident:
        verdict += " (LOW CONFIDENCE)"
    print("            %s" % verdict)
    for note in comparison.confidence_notes:
        print("            - %s" % (note,))


#: The guard could not be applied. See the module docstring for why this is
#: not 1.
_UNUSABLE: Final = 3


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        return _list()

    if args.tolerance is not None:
        try:
            baselines_module.validate_tolerance(args.tolerance)
        except ValueError as exc:
            print("error: --tolerance %s" % exc, file=sys.stderr)
            return 2
    if args.rebaseline_down and not args.rebaseline:
        print(
            "error: --rebaseline-down means nothing without --rebaseline",
            file=sys.stderr,
        )
        return 2

    # A path that does not exist reads as an empty document, which reads as
    # "no baseline", which exits 0. That is a typo in `--baselines` silently
    # disarming the guard, so the only place it is allowed is the one that is
    # about to create the file.
    if not args.baselines.exists() and not args.rebaseline:
        print(
            "error: no baselines file at %s. A missing file cannot guard "
            "anything; pass --rebaseline to create one, or correct the path."
            % (args.baselines,),
            file=sys.stderr,
        )
        return _UNUSABLE
    try:
        document = baselines_module.load(args.baselines)
    except (OSError, ValueError) as exc:
        # OSError as well as ValueError: a path that exists but is a directory,
        # or is unreadable, is the same event as a corrupt one -- the guard has
        # no baseline to apply and must not report that as a pass.
        print("error: %s" % exc, file=sys.stderr)
        return _UNUSABLE

    try:
        result = runner_module.measure(
            workload=args.workload,
            seed=args.seed,
            orders=args.orders,
            repeats=args.repeats,
            calibration=args.calibration,
            with_sink=not args.no_sink,
            request_performance_core=not args.no_qos,
            machine_label=args.machine_label,
        )
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    key = baselines_module.baseline_key(
        result.workload, result.seed, result.orders, result.calibration.name
    )
    recorded = baselines_module.entry(document, key)
    problems = [] if recorded is None else baselines_module.record_problems(recorded)
    if problems:
        if not args.rebaseline:
            print(
                "error: the baseline recorded at %s is not usable:" % (key,),
                file=sys.stderr,
            )
            for problem in problems:
                print("  - %s" % problem, file=sys.stderr)
            print(
                "A half-written baseline is not an absent one: it would read "
                "as 'nothing recorded' and exit 0, leaving the guard off and "
                "saying nothing. Fix the file, or --rebaseline over it.",
                file=sys.stderr,
            )
            return _UNUSABLE
        # About to be overwritten, so it need not be usable -- but say so.
        print(
            "warning: replacing an unusable baseline (%s)" % ("; ".join(problems),),
            file=sys.stderr,
        )
        recorded = None

    # The tolerance recorded with a baseline is the band it was recorded to be
    # judged at, so it is what a later run uses unless that run says otherwise.
    # Recording it and then ignoring it -- as this did -- made the field a
    # comment, and a comment inside a guard is a place for a wrong belief to
    # live undisturbed.
    if args.tolerance is not None:
        tolerance = args.tolerance
    elif recorded is not None and isinstance(recorded.get("tolerance"), (int, float)):
        tolerance = float(recorded["tolerance"])
    else:
        tolerance = baselines_module.DEFAULT_TOLERANCE

    comparison = baselines_module.compare(
        measured=result.sinkless.orders_per_sec,
        measured_work_index=result.work_index,
        # The median, matching the median work index: see `runner`.
        measured_calibration_seconds=result.calibration_median_seconds,
        measured_fingerprint=baselines_module.Fingerprint(
            workload_checksum=result.workload_checksum,
            calibration_checksum=result.calibration.checksum,
            interpreter=result.provenance.get("interpreter"),
        ),
        baseline=None if recorded is None else recorded["sinkless_orders_per_sec"],
        baseline_work_index=None if recorded is None else recorded.get("work_index"),
        baseline_calibration_seconds=(
            None if recorded is None else recorded["calibration_seconds"]
        ),
        baseline_fingerprint=(
            None
            if recorded is None
            else baselines_module.fingerprint_of_record(recorded)
        ),
        tolerance=tolerance,
        dispersion=result.work_index_spread,
    )

    if args.rebaseline:
        complaints = baselines_module.quiet_machine_complaints(
            result.provenance, result.calibration_spread
        )
        if complaints and not args.force:
            print("refusing to record a baseline on this machine:", file=sys.stderr)
            for complaint in complaints:
                print("  - %s" % complaint, file=sys.stderr)
            print(
                "A bad baseline is a bad denominator for everyone, forever "
                "(ADR-0005). Run it quiet, or --force if you mean it.",
                file=sys.stderr,
            )
            return _UNUSABLE
        # A floor that can move down on its own is not a floor. Moving it up is
        # the ordinary case and needs no ceremony; moving it down absorbs
        # whatever made it move, which is the one thing the guard exists to
        # notice, so it takes a flag of its own. `--force` is about the machine
        # and deliberately does not carry this.
        movement = _delta(recorded, result)
        if (
            recorded is not None
            and recorded.get("work_index")
            and result.work_index < recorded["work_index"]
            and not args.rebaseline_down
        ):
            print(
                "refusing to move the baseline down: %s" % (movement,),
                file=sys.stderr,
            )
            print(
                "The verdict on this run was %s. Re-baselining down writes "
                "whatever caused that into the floor, so it takes "
                "--rebaseline-down and not --force." % (comparison.status,),
                file=sys.stderr,
            )
            return _UNUSABLE
        document.setdefault("baselines", {})[key] = _record(result, tolerance)
        if complaints:
            document["baselines"][key]["forced"] = complaints
        document["note"] = (
            "Recorded baselines. A number here is only comparable alongside "
            "the calibration figure taken with it (ADR-0005); the harness "
            "scales by their ratio. Change a workload or a calibration and "
            "its name changes, which changes the key, which reads as 'no "
            "baseline' until it is recorded again -- and if the name does not "
            "change, the recorded workload and calibration checksums and the "
            "recorded interpreter identity are compared on every run, so a "
            "baseline that measured something else refuses to judge instead of "
            "judging wrongly."
        )
        baselines_module.save(args.baselines, document)

    if args.json:
        print(
            json.dumps(
                {
                    "key": key,
                    "workload": result.workload,
                    "seed": result.seed,
                    "orders": result.orders,
                    "repeats": result.repeats,
                    "sinkless_orders_per_sec": result.sinkless.orders_per_sec,
                    "sink_orders_per_sec": (
                        None if result.sink is None else result.sink.orders_per_sec
                    ),
                    "trades": result.sinkless.trades,
                    "work_index": result.work_index,
                    "work_index_spread": result.work_index_spread,
                    "calibration": result.calibration.name,
                    "calibration_seconds": result.calibration.seconds,
                    "calibration_median_seconds": result.calibration_median_seconds,
                    "calibration_spread": result.calibration_spread,
                    "workload_checksum": result.workload_checksum,
                    "calibration_checksum": result.calibration.checksum,
                    "samples": [
                        {
                            "calibration_seconds": sample.calibration_seconds,
                            "sinkless_orders_per_sec": sample.sinkless.orders_per_sec,
                            "work_index": sample.work_index,
                        }
                        for sample in result.samples
                    ],
                    "comparison": comparison._asdict(),
                    "provenance": result.provenance,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
    else:
        _report(result, comparison, key)
        if args.rebaseline:
            print()
            print("rebaseline  %s" % (movement,))
            print("recorded to %s" % args.baselines)

    if comparison.incomparable and not args.rebaseline:
        print(
            "INCOMPARABLE on %s: %s" % (result.workload, comparison.reason),
            file=sys.stderr,
        )
        return _UNUSABLE
    if comparison.failed:
        print(
            "REGRESSION on %s: %s" % (result.workload, comparison.reason),
            file=sys.stderr,
        )
        return 1
    if args.fail_on_low_confidence and not comparison.confident:
        print(
            "LOW CONFIDENCE on %s: %s" % (result.workload, comparison.reason),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
