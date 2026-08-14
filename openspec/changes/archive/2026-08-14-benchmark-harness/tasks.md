# Tasks: benchmark-harness

## 1. Generator and runner

- [ ] 1.1 `PyLOB/bench/workloads.py`: seeded generator, `mixed-v1` (70/20/10
      shape from the trial script), importable by the differential harness
- [ ] 1.2 `PyLOB/bench/__main__.py`: run (engine, workload, seed), best-of-3,
      report per the benchmarking spec
- [ ] 1.3 Determinism test: same (workload, seed) twice -> identical streams

## 2. Baselines

- [ ] 2.1 Baseline comparison + tolerance + non-zero exit on regression;
      `--rebaseline` flow
- [ ] 2.2 Record initial baselines: legacy engine (expect ~439/s territory)
      and new engine, on the maintainer's machine; commit
      `benchmarks/baselines.json`
- [ ] 2.3 Confirm the new-engine number meets ADR-0001's >= 100x intent; if
      not, report to maintainer before recording (that is an engine problem,
      not a baseline to accept)

## 3. Docs hook

- [ ] 3.1 Short README section: how to run, how to read, how to re-baseline
      (full docs pass is `rewrite-docs`)
