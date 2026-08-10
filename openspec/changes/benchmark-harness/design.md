# Design: benchmark-harness

## Context

See proposal. The trial script (scratch, 2026-08-10) is the workload's
starting shape; the differential harness in `inmemory-engine` needs the same
generator, so the generator is a library module, not a script.

## Goals / Non-Goals

**Goals:**
- One command answers "did I make it slower?" with a yes/no exit code.
- Cross-machine honesty: relative-to-baseline judgment, machine context
  recorded but never compared across machines.

**Non-Goals:**
- No micro-benchmarks, no profiling tooling, no CI wiring (future decision).
- Not part of `./verify` (standing constraint).

## Decisions

1. **Workload generator is `random.Random(seed)`-based and versioned by
   name** (`mixed-v1`); changing a workload's composition means a new name,
   never a silent edit — otherwise baselines lie.
2. **Baselines live in `benchmarks/baselines.json`** keyed by (engine,
   workload); values carry orders/sec, date, machine note. Default tolerance
   20% (interpreter jitter on laptops is real); overridable via CLI flag.
3. **Re-baseline is a CLI action (`--rebaseline`)** that rewrites the JSON —
   deliberate, diff-visible, maintainer-reviewed in the PR. *Alternative
   rejected:* auto-update on improvement — invisible ratchet, hides
   variance.
4. **Engine selection by name** (`--engine new|legacy`) mapping to the
   import surface `inmemory-engine` establishes.

## Risks / Trade-offs

- [Laptop thermal variance produces false regressions] → 20% tolerance +
  three-run-best-of reporting; persistent flags are real signals.
- [Workload realism] → mixed-v1 mirrors the trial's shape; adding replay-
  from-recorded-data workloads is a natural follow-up once the sink exists.

## Open Questions

- None.
