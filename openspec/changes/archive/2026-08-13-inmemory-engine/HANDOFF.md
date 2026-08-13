# Handoff: inmemory-engine

The in-memory engine is the public `PyLOB.OrderBook`. The SQL engine remains
as `PyLOB.LegacyOrderBook`, the cross-check oracle ADR-0001 keeps in tree.

## Measured throughput

20,000-order mixed workload (70% passive, 20% crossing, 10% market; 20 traders
with commissions; drifting mid), best of three, on the maintainer's laptop
while several agents ran in parallel:

| Configuration | orders/sec | vs. the 439/s legacy baseline |
| --- | --- | --- |
| Sinkless (the default) | ~143,000 | **~326x** |
| `SQLiteSink` attached | ~17,000 | ~38x |

Per **ADR-0002** the ≥100x target is measured sinkless, and it is met with
room to spare. The sink-attached figure is reported and does not gate.

**These numbers are informal.** They were taken on a contended machine and are
not baselines. `benchmark-harness` is the formal guard, and its
`lob-lby.5` — recording baselines on a quiet maintainer machine — is
deliberately deferred to a dedicated run after the rewrite settles.

The sink's cost is per-event encoding (`asdict` plus `json.dumps`, plus a
projection row-write per event), not transaction count: raising `buffer_size`
from 512 to 16,384 moved it barely at all. The engine is not the bottleneck
in that number.

## Reconciliations needed at archive time

CLAUDE.md says to reconcile at archive, not at proposal. Four items.

**1. `openspec/config.yaml`'s `context:` block has two stale statements.**
Both are load-bearing, because every change proposal is written against them.

- Line 13: *"Packaging: setup.cfg/setup.py migrate to pyproject.toml (decided,
  not yet done)."* — done, in `2026-08-12-modernize-packaging`. Should now
  read as fact, not intent.
- Lines 20–24: *"Matching **is moving** to an in-memory engine … The legacy
  SQL engine stays in tree as a cross-check oracle **until** the in-memory
  engine passes the same test suite."* — it has moved, and the engine passes
  the same suite. The sentence should describe the present state and say what
  now governs the legacy engine's retirement.

**2. ADR-0001's transition condition is satisfied, and that opens a question
it deliberately left open.** The ADR retains the legacy engine "as a
cross-check oracle during the transition"; the transition is over. Retiring it
would delete `orderbook.py`, `create_lob.sql`, the legacy adapter, thirteen
`engine_xfail` markers and the differential harness's whole reason to exist.
Keeping it costs the maintenance of a dead engine and nine open defects nobody
intends to fix.

This is a maintainer decision and wants a new ADR either way, not a quiet
deletion. **Do not fold it into this archive.** Note that the differential
harness is the strongest argument for keeping the oracle a while longer: it is
the only thing that would catch a regression in the new engine against an
independent implementation.

**3. The delta spec `specs/recording-sink/spec.md` syncs into
`openspec/specs/recording-sink/`.** Standard archive sync; it is the only
delta this change carries.

**4. ADR-0002 was written mid-change** (`docs/adr/0002-...`) and is already
accurate. It needs no reconciliation, but the README will quote both its
numbers, so the two must not drift.

## Nine legacy defects stay open, deliberately

Writing the specs suites surfaced nine defects in the SQL engine. **None is
fixed there**, by decision: ADR-0001 retires that engine, so the fixes would
be thrown away. Each is pinned as a strict `engine_xfail`, which makes it
implementation-blocking for the new engine and turns the run red if it is ever
quietly fixed without removing the marker.

All nine are closed by construction on the new engine.

| Bead | Defect (legacy only) |
| --- | --- |
| `lob-0bl` | Market remainder rests, sorts best at a null price, trades at the taker's price |
| `lob-crf` | `modifyOrder(price=None)` matches as a market order |
| `lob-ihv` | Invalid submissions call `sys.exit` |
| `lob-0rb` | Unknown-id and wrong-side cancel/modify silently no-op |
| `lob-a17` | Duplicate external `idNum` accepted |
| `lob-7e7` | Identifier counter not seeded across a reload |
| `lob-pn3` | Price change on modify keeps time priority |
| `lob-bis` | `lastPrice` not rebuilt on reload |
| `lob-z45` | Book queries leak across instruments (P4: out of scope under the single-instrument constraint) |

If item 2 above retires the legacy engine, these close as won't-fix together
with it. Until then they are accurate open bugs against shipped code.

## What the next epics should know

**`benchmark-harness`.** ADR-0002 governs: baselines and the regression
threshold are recorded against the **sinkless** configuration; the
sink-attached number is reported alongside and must not gate. `lob-lby.8` has
the differential harness importing the bench workloads — note
`tests/test_differential.py` already carries nine generator constraints, each
naming a bead, and the bench workloads will need the same care about which
inputs are legitimate to generate against two engines.

**`rewrite-docs`.** The README must quote **both** throughput numbers and say
which is which; quoting only the sinkless figure would mislead anyone who
attaches a sink. Frame the sink as ADR-0002 does — run sinkless for speed
(RL gym, parameter sweeps, many episodes), attach a sink for the smaller
number of runs you intend to inspect afterwards. `src/lob.html` is a stale
generated schema dump (`lob-k1f`) and should be regenerated or deleted.

## Test surface as it stands

`./verify`: format, lint, test, smoke — **151 passed, 13 xfailed**, exit 0 in
about 5.6s against a 60s budget. All 13 xfails are `[legacy]`.

- `tests/acceptance/` — the four frozen-contract suites, both engines
- `tests/test_lifecycle.py` — legacy invariants
- `tests/test_issue8_regressions.py` — both engines
- `tests/test_replay.py` — persisted stream to fresh engine, end-state equality
- `tests/test_sink_equality.py` — sink-attached vs sinkless, the guarantee
  ADR-0002's workflow rests on
- `tests/test_differential.py` — both engines in lockstep, whitelist of two
