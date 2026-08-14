# Pre-retirement review of the in-memory engine — 2026-08-12

> **Status, 2026-08-13.** Its conditions were met and acted on. ADR-0003
> records all six P1 findings as fixed and the retirement cliff as closed —
> re-measured with the oracle excluded, three survivors, all deliberate no-op
> controls — and the maintainer then retired the legacy engine. So the verdict
> below, "do not retire the legacy engine yet", is a gate that has since
> opened, not standing advice; the recommended sequence under it is done.
>
> Two present-tense remarks have changed with it. The **419x** ratio in "What
> held up under attack" is against an engine that is no longer in tree and
> cannot be re-measured; the performance claim that survives is ADR-0002's
> sinkless figure, and this review's own method note already says to trust the
> ratios rather than the absolutes. And `lob-kbx`'s "`config.yaml` still
> declares the engine single-instrument" is closed: the `context:` block now
> states what the engine supports and tests. Nothing else here is edited —
> the findings are the record of what was found, and they stand as written.
>
> **Addendum, 2026-08-14.** ADR-0002 was itself superseded later on 2026-08-13
> by [ADR-0005](adr/0005-calibrated-throughput-baselines.md), so read the
> surviving performance claim there rather than in ADR-0002: throughput is
> still measured sinkless, but it is judged against calibration-normalised
> baselines in `benchmarks/baselines.json` instead of a ratio to the deleted
> engine, and 439 orders/sec is now recorded as a historical origin rather than
> a live denominator.

Requested by the maintainer before deciding whether to retire the legacy SQL
engine. Five independent adversarial reviews: matching-core correctness,
event/sink/replay soundness, API and spec alignment, test-suite adequacy
(mutation testing), and hot-path performance. Every P1 claim below was
re-verified independently by the orchestrator before being accepted. Findings
are filed as beads; this document is the consolidated record. Bead: `lob-8k1`.

## Verdict

**Do not retire the legacy engine yet.** Not because the core is unsound — the
strongest results of this review say it is sound — but because six P1 findings
need fixing first, and one of them is that the test suite itself leans on the
legacy oracle: 45 of 99 injected mutants would survive the post-retirement
suite, five of them in money-and-book code that only the differential harness
currently guards.

Recommended sequence: fix the five P1 defect clusters, close the
retirement-cliff test gaps (`lob-6oj`), re-run the mutation harness without
the differential suite to prove the cliff is gone, and only then write the
retirement ADR. The archive gate for `inmemory-engine` (`lob-5rt.15`) is
blocked on all six.

## What held up under attack

These are the results the retirement decision can lean on, and they were not
free passes — each is an attack that failed.

- **An independent reference matcher agrees with the engine over 600,000
  operations.** Written from the frozen specs alone — a flat list re-sorted by
  `(price, priority)` on every match, sharing no code with the engine —
  compared trade-for-trade and book-for-book after every operation, across 25
  seeds and four gating configurations. Zero divergence. This validates fill
  allocation and priority themselves, not merely internal consistency.
- **A ~1.4-million-operation invariant sweep is clean**, including hostile
  profiles (all-gated single trader, churn aimed at the touch, non-decimal
  ticks, three instruments). Checked after every operation: level volumes,
  both heaps against brute-force scans, snapshot completeness and ordering,
  fill bounds, trade conservation, balance netting, commission recomputation.
- **The book survives 13,043 aborted mid-walk exceptions** with structure
  intact — the `finally` push-back in `match_levels` is correct.
- **The buffered sink's output is bit-identical at buffer sizes 1, 7, and
  100,000**, and its statement coalescing cannot reorder effects (verified
  structurally). Finite floats, `-0.0`, denormals, huge ints, and non-ASCII
  text all round-trip the JSON log exactly.
- **Sink-attached and sinkless runs produce identical outcomes**, and a
  sinkless engine constructs zero events (25.6% of the sinkless run's cost is
  event construction when a sink is attached — the gating is doing real work).
- **Performance holds on every conventional shape**: 130k–307k orders/sec
  across seven workload shapes (one-tick, 10k-level sparse, cancel-heavy,
  modify-heavy, monotonic, stale-churn, mixed), no degradation over a
  2M-operation sustained run, no compaction cliff on `_best`, and
  fresh-engine-per-episode is both the cheaper and the faster reset pattern.
  Apples-to-apples on one machine: **419x** the legacy engine.

## P1 findings (all orchestrator-verified)

| Bead | Finding |
| --- | --- |
| `lob-d6i` | **A NaN limit price is accepted and silently corrupts the book.** One NaN order buries better prices in the heap (every comparison against NaN is false, and the stale entry can never be evicted), after which a marketable ask at 105 walks past a resting bid at 106 and the book ends **crossed between two different traders** — permanently, silently, and reproduced faithfully by replay. Same boundary: `inf`/string/huge prices leak `decimal.InvalidOperation`; a huge quantity raises `OverflowError` *after* fills commit but *before* settlement (fills recorded, no money moved — and the silent variant drives the ledger to ±inf); negative prices produce negative commission deltas (the ledger credits commission); a market order silently ignores a supplied price. |
| `lob-rp4` | **The self-match skip walk is O(k²)** and collapses exactly on ADR-0002's named use case: a single-agent RL gym quoting then crossing its own depth runs at 2,578 orders/sec at k=200 (below the 100x line), 149 at k=800, and **10 orders/sec at k=3200 — 30x slower than the legacy engine it replaces**. Verified at 4.1x per doubling. Fix is local: hold one iterator instead of re-indexing. |
| `lob-n3n` | **The `_worst` heap is never compacted** — the gate reads `_best`, which matching keeps short, so it never opens. Verified: 20k churn cycles leave 20,001 stale entries over 1 live level; at 1M ops one shape reaches 499,000 entries, 19.2 MB, and a 234ms first `getWorst*` call. Plus: duplicate heap pushes make `match_levels` hand the same gated level out twice within one walk (correctness holds; compounds the O(k²) walk to 1.57s for one order). |
| `lob-c2k` | **The sink can lose acknowledged events silently.** A failed manual `flush()` forgets its error — verified: `close()` then returns cleanly over an empty file. A poison event drops its whole 512-event batch with *nothing on disk marking the loss* (the surviving seq range is contiguous), and leaves the projections self-inconsistent. `close()` only re-raises once; `consume` after `close` discards everything. |
| `lob-9fu` | **The emission set does not cover the public mutation set.** `configure_instrument(sym, None)` withdraws a currency with zero events — engine and sink ledgers then diverge 2x, silently. `setLastPrice` mutates a spec-named reporting value unrecorded. `match()` executes real trades for an order the engine never registered (verified: ghost order trades, `order(999)` is `None`), and a replayed `create_order` becomes a full `submit`, inventing trades. |
| `lob-6oj` | **The retirement cliff.** 99 mutants: 40 survive today, **45 post-retirement**. Five money/book mutants are killed only by the differential harness; 56% of the suite adds zero engine line coverage; the sink's entire projection layer — the tables a deep-dive queries — has no test at all (11 untouched mutants; verified to have no *current* bug); and `order.priority` is written and recorded but **never read** — matching FIFO rests on dict insertion order, so the spec's named mechanism is satisfied by accident. |

## P2/P3 (filed, not blocking)

`lob-8r6` — modifyOrder resurrects fully-filled orders while cancelOrder
refuses them; needs a spec decision, not a unilateral fix. `lob-49r` —
`processOrder` compat shim: bare `KeyError`, zero coverage, and the
return-shape change (`Trade` dataclasses vs legacy rowid-tuples) is
undocumented. `lob-k3h` — re-entrancy contract: a sink observing the book
mid-walk sees the disagreement `book-queries` forbids; mutable `Order` lets a
caller desynchronize the level index; two engine docstrings make claims the
review falsified. `lob-fcq` — the RL-gym reset story (fresh engine per
episode) is the right pattern, measured faster than not resetting, and
documented nowhere. `lob-kbx` — hygiene sweep: the acceptance conftest is
loaded twice under two module names, read queries mutate the instrument set,
instruments and currencies share a balance namespace, and `config.yaml` still
declares the engine single-instrument.

## Method notes

Mutation harness, workloads, reference matcher, and all repro scripts are in
the session scratchpad (`mutations*.py`, `results*.json`, `workloads.py`,
`bench_*.py`, `check_projections.py`). The mutation harness was calibrated
with three no-op controls (all correctly survived). Performance numbers were
taken on a contended machine (load 2.9–10.9) and rest on interleaved A/B
comparison; treat ratios as reliable and absolutes as indicative. The
439 orders/sec legacy baseline from the August architecture review did not
reproduce (296/s measured solo); the *ratio* is what survives.
