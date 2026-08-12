# Architectural decision records

One line per ADR. Read this index before proposing a change; read an individual
ADR only if it is relevant.

- [ADR-0000](0000-template.md) — template
- [ADR-0001](0001-inmemory-matching-sqlite-sink.md) — Accepted: matching moves in-memory; SQLite becomes an optional off-hot-path sink
- [ADR-0002](0002-throughput-target-measured-sinkless.md) — Accepted: the ≥100x throughput target is measured with no sink attached; the sink-attached figure is reported but does not gate
