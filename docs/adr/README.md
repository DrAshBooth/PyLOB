# Architectural decision records

One line per ADR. Read this index before proposing a change; read an individual
ADR only if it is relevant.

- [ADR-0000](0000-template.md) — template
- [ADR-0001](0001-inmemory-matching-sqlite-sink.md) — Accepted: matching moves in-memory; SQLite becomes an optional off-hot-path sink (its transition clause is superseded by ADR-0003)
- [ADR-0002](0002-throughput-target-measured-sinkless.md) — **Superseded by ADR-0005**: the ≥100x throughput target is measured with no sink attached; the sink-attached figure is reported but does not gate
- [ADR-0003](0003-retire-the-legacy-sql-engine.md) — Accepted: the legacy SQL engine is retired; the differential oracle is replaced by a spec-derived reference matcher, not deleted (supersedes ADR-0001's transition clause)
- [ADR-0004](0004-trade-is-a-namedtuple.md) — Accepted: `Trade` is a NamedTuple, not a frozen dataclass (a measured 9% of the sinkless hot path); the type widens to a tuple
- [ADR-0005](0005-calibrated-throughput-baselines.md) — Accepted: throughput is judged against a calibration-normalised baseline, not a ratio to the deleted legacy engine (supersedes ADR-0002)
