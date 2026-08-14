# benchmarking Specification

## Purpose
Deterministic performance measurement for the matching engines: reproducible
workloads, comparable numbers, and an explicit baseline discipline so
performance claims stay honest.
## Requirements
### Requirement: Workloads are seeded and deterministic

A benchmark workload SHALL be fully determined by (workload name, seed):
the same pair SHALL produce the identical order stream on every run and
machine, so that two engines can be measured on byte-identical input.

#### Scenario: Same seed, same stream

- **WHEN** the mixed workload is generated twice with seed 42
- **THEN** both runs produce the identical sequence of orders

### Requirement: Measurement reports throughput with context

A benchmark run SHALL report orders processed, trades executed, wall-clock
time, and orders/sec, tagged with engine identity, workload name, and seed.

#### Scenario: Report contents

- **WHEN** a benchmark run completes
- **THEN** its report contains engine, workload, seed, order count, trade
  count, elapsed seconds, and orders/sec

### Requirement: Regression is judged against a recorded baseline

The harness SHALL compare a run to the recorded in-repo baseline for the
same (engine, workload) and flag a regression when throughput falls below
the baseline by more than the configured tolerance. Baselines SHALL only
change by an explicit re-baseline action, never automatically.

#### Scenario: Regression flagged

- **WHEN** a run's orders/sec is below baseline by more than the tolerance
- **THEN** the harness exits non-zero and names the offending workload

#### Scenario: Faster run does not silently re-baseline

- **WHEN** a run beats the baseline
- **THEN** the recorded baseline file is unchanged

