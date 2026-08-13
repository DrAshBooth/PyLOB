"""`python -m PyLOB.bench`: measure, compare, and say whether it got slower.

Exit codes are the interface, because the point of the harness is that one
command answers "did I make it slower?" without anyone reading prose:

    0   no regression, or nothing recorded to compare against
    1   a regression against the recorded baseline (or, with
        `--fail-on-low-confidence`, a measurement not worth trusting)
    2   the command line was wrong (argparse's own code)

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
from typing import Any, Sequence

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
        default=baselines_module.DEFAULT_TOLERANCE,
        help="fraction below baseline that counts as a regression "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--baselines",
        type=Path,
        default=_DEFAULT_BASELINES,
        help="baselines file (default: benchmarks/baselines.json)",
    )
    parser.add_argument(
        "--rebaseline",
        action="store_true",
        help="record this run as the baseline, rewriting the file. Refuses on "
        "a machine that is not quiet unless --force.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --rebaseline, record even though the machine is contended",
    )
    parser.add_argument(
        "--fail-on-low-confidence",
        action="store_true",
        help="exit non-zero when the calibration is too far from the "
        "baseline's for scaling to be trusted, even without a regression",
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
        "calibration": result.calibration.name,
        "calibration_seconds": round(result.calibration.seconds, 6),
        "calibration_checksum": result.calibration.checksum,
        "calibration_spread": round(result.calibration_spread, 4),
        "workload_checksum": result.workload_checksum,
        "repeats": result.repeats,
        "tolerance": tolerance,
        "recorded_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "provenance": result.provenance,
    }


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
        "calibration %s %.4fs per pass, spread %.1f%% across repeats"
        % (
            result.calibration.name,
            result.calibration.seconds,
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
        "work index  %9.0f orders per calibration pass   [median of %d, GATING]"
        % (result.work_index, result.repeats)
    )
    print()
    print("baseline    %s" % key)
    if comparison.status == "no-baseline":
        print("            NO BASELINE recorded. Nothing to guard against yet;")
        print("            `--rebaseline` records one, on a quiet machine.")
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
    if not comparison.confident:
        print(
            "            calibration is %.2fx the baseline's, outside the %.2fx band"
            % (comparison.speed_ratio, baselines_module.CONFIDENCE_BAND)
        )
        print(
            "            in which linear scaling can be trusted; treat this "
            "number as indicative"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        return _list()

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
    document = baselines_module.load(args.baselines)
    recorded = baselines_module.entry(document, key)
    comparison = baselines_module.compare(
        measured=result.sinkless.orders_per_sec,
        measured_work_index=result.work_index,
        measured_calibration_seconds=result.calibration.seconds,
        baseline=None if recorded is None else recorded["sinkless_orders_per_sec"],
        baseline_work_index=None if recorded is None else recorded.get("work_index"),
        baseline_calibration_seconds=(
            None if recorded is None else recorded["calibration_seconds"]
        ),
        tolerance=args.tolerance,
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
            return 1
        document.setdefault("baselines", {})[key] = _record(result, args.tolerance)
        if complaints:
            document["baselines"][key]["forced"] = complaints
        document["note"] = (
            "Recorded baselines. A number here is only comparable alongside "
            "the calibration figure taken with it (ADR-0005); the harness "
            "scales by their ratio. Change a workload or a calibration and "
            "its name changes, which changes the key, which reads as 'no "
            "baseline' until it is recorded again."
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
                    "calibration": result.calibration.name,
                    "calibration_seconds": result.calibration.seconds,
                    "calibration_spread": result.calibration_spread,
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
            print("recorded to %s" % args.baselines)

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
