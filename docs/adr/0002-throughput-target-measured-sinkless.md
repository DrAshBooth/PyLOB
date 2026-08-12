# ADR-0002: The throughput target is measured with no sink attached

Status: Accepted
Date: 2026-08-12

## Context

ADR-0001 set matching throughput as the criterion that resolved SQLite's trial,
against a measured legacy baseline of **439 orders/sec**, and stated an intent
of roughly 100x. It made the SQLite sink optional and off the hot path, but it
did not say whether the target is measured with a sink attached or without one.
That was fine while no engine existed. It stopped being fine the moment one did.

First measurement of the completed in-memory engine (20k-order mixed workload,
70% passive / 20% crossing / 10% market, 20 traders, commissions on):

| Configuration | orders/sec | vs. the 439/s baseline |
| --- | --- | --- |
| No sink | ~155,000 | ~353x |
| `SQLiteSink` attached | ~19,000 | ~42x |

So the same engine either clears the target by 3.5x or misses it by more than
half, depending entirely on a question nobody had answered.

The sink's cost is per-event encoding — `dataclasses.asdict` plus
`json.dumps`, plus a projection row-write per event — not transaction count.
Raising `buffer_size` from 512 to 16,384 moved the figure only from 18.6k to
19.6k orders/sec. The engine is not the bottleneck in the sink-attached number.

## Decision

**The ≥100x throughput target is measured with no sink attached.** The
sinkless configuration is the one the target governs, the one benchmark
baselines are recorded against, and the one a performance regression is
measured on.

The sink-attached figure is reported but **not** subject to the target. It is
a distinct number for a distinct purpose.

This follows the way the library is actually used. A research workload — an RL
gym, a parameter sweep, a replay of many episodes — wants maximum speed and no
persistence, and runs sinkless. Attaching the sink is a deliberate act taken
for a smaller number of runs the user intends to inspect afterwards: trade
history, balances, commissions. Paying a 8x throughput cost on every run to
serve the minority that will be inspected is the wrong default, and constraining
the sink to a target set for the matching core would force exactly that
trade-off.

Consequently the engine constructs no event at all when no sink is attached —
`if book.recording: book.emit(...)` — so sinkless is genuinely free rather
than merely cheap, and `sink=None` stays the default.

## Alternatives considered

- **Apply the target to the sink-attached configuration.** Rejected. It would
  make the recording format's encoding cost a correctness-adjacent constraint
  on the matching epic, and the lever it forces — a binary payload, or dropping
  the append-only log in favour of projections only — trades away replay
  fidelity and auditability to hit a number that the primary use case never
  pays. The log layer exists because replay must read real events rather than
  rebuild them from summaries.
- **Set a second, lower target for the sink-attached path.** Rejected for now,
  as a number invented without a use case to justify it. Nobody has yet said
  how fast recorded runs need to be. If a user reports the sink as too slow for
  a real workload, that is the evidence to set one, and this ADR should be
  superseded rather than quietly reinterpreted.
- **Make the sink's cost the benchmark's headline number**, on the grounds that
  it is the more conservative figure. Rejected: it would understate the engine
  by 8x for every user who never attaches a sink, which is expected to be most
  of them.

## Consequences

- `benchmark-harness` records its baselines and its regression threshold
  against the sinkless configuration. The sink-attached number is reported
  alongside, clearly labelled, and does not gate.
- Documentation quotes both numbers and says which is which. Quoting only the
  sinkless figure without qualification would be misleading to anyone who
  attaches a sink.
- `sink=None` remains the default when the new engine becomes the public
  `OrderBook`. A default that silently records would make every user pay for a
  feature most do not want.
- Sink performance work is legitimate but unconstrained by this target. If it
  is ever undertaken, the lever is the per-event encoding, not the buffer size.
