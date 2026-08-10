# Proposal: benchmark-harness

## Why

ADR-0001's performance claim needs a permanent, deterministic guard. The
trial measurement (439 orders/sec, 20k mixed workload, 2026-08-10) was a
scratch script; without a maintained harness, performance regressions in the
new engine are invisible until a user's simulation crawls. "Fast" in the
README becomes a measured, reproducible number.

## What Changes

- A benchmark entry point (`python -m PyLOB.bench`) running seeded,
  deterministic mixed workloads (the trial script's shape: 70% passive
  limits, 20% crossing, 10% market, drifting mid) against a selectable
  engine, reporting orders/sec and trade counts.
- Recorded baselines in-repo (JSON): legacy engine baseline and the new
  engine's accepted number, per workload, with machine context noted.
- A regression criterion: relative to the recorded baseline for the same
  workload, not absolute (machines differ). Default tolerance and the
  update procedure (re-baseline requires maintainer say-so) documented.
- Benchmarks stay **out of `./verify`** (standing constraint: too noisy for
  a 60-second correctness gate); the harness is run on demand and its
  baseline updates are deliberate acts.

## Capabilities

### New Capabilities

- `benchmarking`: deterministic workload generation, measurement, baseline
  comparison semantics.

### Modified Capabilities

<!-- none -->

## Impact

- New: `src/PyLOB/bench.py` (or `bench/` module), `benchmarks/baselines.json`.
- Depends on `inmemory-engine` (its performance is the thing measured;
  engine selection needs both engines importable).
- The differential harness in `inmemory-engine` reuses the workload
  generator; build it importable, not script-only.
