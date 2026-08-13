# ADR-0005: Throughput is judged against calibrated baselines

Status: Accepted
Date: 2026-08-13

Supersedes [ADR-0002](0002-throughput-target-measured-sinkless.md).

## Context

ADR-0002 set the throughput target at ≥100x the legacy SQL engine's measured
439 orders/sec, and settled that it is measured with no sink attached. ADR-0003
then deleted that engine. The denominator is gone: nobody can re-measure 439/s
on any machine, so "100x" is now an appeal to a number in a document rather
than a property anyone can check.

Two further facts make a plain absolute floor insufficient on its own.

**The 439/s figure never reproduced.** The pre-retirement performance review
re-measured the legacy engine at 296 orders/sec on the maintainer's machine —
a third lower than the architecture review's figure, on the same workload
shape. Neither number was wrong; they were taken on differently loaded
machines. That is the whole problem in miniature.

**This machine is not quiet, and no maintainer's laptop is.** Measurements
during this work ranged from 49k to 114k orders/sec for the *same* sinkless
workload inside a single interleaved loop, with load average between 1.9 and
325. An M1 has four performance and four efficiency cores, so a single-threaded
run can land on an efficiency core and read ~40% slow for no reason connected
to the code. Battery versus mains changes it again.

A baseline recorded under those conditions and compared literally will produce
false regressions for everyone who is not the maintainer on the day, which is
exactly why recording them was deferred rather than done in passing.

## Decision

**Throughput is judged against a calibrated baseline, not an absolute number
and not a ratio to a deleted engine.**

Three parts:

1. **A calibration workload runs alongside every benchmark run.** It is a
   fixed, engine-independent reference computation with a known cost, versioned
   by name exactly as the engine workloads are. Its result is recorded with
   every measurement.

2. **Comparisons are normalised by calibration.** A run is compared to a
   baseline after scaling by the ratio of their calibration figures, so a
   machine that is uniformly 30% slower reads as *no regression* rather than as
   a 30% one. A regression is a change in the engine's cost *relative to the
   machine it ran on*.

3. **The target is a floor on the normalised figure, recorded in
   `benchmarks/baselines.json` and re-baselined deliberately.** ADR-0002's
   substance survives: the floor is measured **sinkless**, because that is the
   configuration the primary workload uses, and the sink-attached figure is
   reported alongside but does not gate. The tolerance band remains the
   change's own decision (20% by default), applied to the normalised value.

**439 orders/sec is recorded as a historical origin, not a live denominator.**
The engine's speedup over the 2013 SQL implementation is a fact about the
project's history, quotable as such, and no longer something a test can assert.

## Consequences

- Baselines become meaningful off the machine that recorded them, which is what
  makes recording them on the maintainer's laptop defensible at all.
- Every run records provenance — machine, CPU brand, core counts, Python
  version, commit, load average, power source — so a surprising number can be
  explained rather than merely disbelieved.
- Calibration can itself drift (a Python release changing the cost of the
  reference computation), so the calibration workload is versioned and a
  changed name means a re-baseline, the same rule the engine workloads follow.
- `benchmark-harness`'s design decision 4 (`--engine new|legacy`) is void with
  the legacy engine, and its task 2.2 (record legacy baselines) is
  unperformable. Both are reconciled when that change is archived.
- Normalisation is a correction, not a cure. A thermally throttled or
  heavily-loaded machine still produces noisy numbers; calibration makes the
  noise visible and comparable rather than eliminating it. A run whose
  calibration is far from the baseline's should be reported as low-confidence,
  not silently scaled.

## Alternatives considered

- **A plain absolute floor in orders/sec.** Rejected as the thing that breaks
  the moment someone runs it on different hardware — and CI, a laptop on
  battery, and the maintainer's desk are already three different machines.
- **Keep a ratio, re-baselining the denominator against a stored legacy
  figure.** Rejected: it preserves the form of ADR-0002 while quietly making
  the denominator unfalsifiable, which is worse than dropping it honestly.
- **Never compare across machines** — the original plan's position, recording
  machine context but only ever comparing a machine to itself. Rejected as too
  weak now that the numbers appear in documentation: the README quotes a
  throughput figure, so somebody will compare, and it is better to make that
  comparison sound than to disclaim it.
- **Pin the benchmark to a performance core and require mains power.**
  Rejected as the whole answer — worth doing, and the harness should detect and
  report both, but it makes the measurement stricter rather than portable, and
  it cannot be enforced in every environment that will run this.
