"""The benchmark harness: deterministic workloads, calibrated baselines.

Run it::

    uv run python -m PyLOB.bench                # measure and compare
    uv run python -m PyLOB.bench --list         # what workloads exist
    uv run python -m PyLOB.bench --rebaseline   # record, deliberately

It is **not** part of `./verify`, by standing constraint: a correctness gate
that takes five seconds must not grow a stage whose answer depends on what
else the laptop is doing. The harness is run on demand, and its baseline
updates are deliberate acts that show up in a diff.

The pieces, and which of them is load-bearing

- `workloads` -- seeded generators, versioned by name. Imported by the
  benchmark runner *and* by the differential harness, which is why it is a
  library module and not a script.
- `calibration` -- an engine-independent reference computation, run alongside
  every benchmark, that measures the machine rather than the code.
- `baselines` -- the recorded numbers and the calibrated comparison. ADR-0005
  lives here: a run is judged against a baseline after scaling by the ratio of
  their calibration figures.
- `provenance` -- machine, CPU, core counts, power source, load, commit, and
  on Apple silicon whether the run landed on a performance or an efficiency
  core.
- `runner` -- the timed loop.

What gates and what does not: the floor is on the **sinkless** figure, which
is the configuration the primary workload uses (ADR-0002's substance, kept by
ADR-0005). The sink-attached figure is measured and reported alongside it and
never gates.
"""

from .baselines import CONFIDENCE_BAND, DEFAULT_TOLERANCE, Comparison, compare
from .calibration import CALIBRATIONS, CalibrationResult
from .runner import BenchResult, RunResult, measure
from .workloads import WORKLOADS, Op, WorkloadSpec, generate

__all__ = [
    "CALIBRATIONS",
    "CONFIDENCE_BAND",
    "DEFAULT_TOLERANCE",
    "BenchResult",
    "CalibrationResult",
    "Comparison",
    "Op",
    "RunResult",
    "WORKLOADS",
    "WorkloadSpec",
    "compare",
    "generate",
    "measure",
]
