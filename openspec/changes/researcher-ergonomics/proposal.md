# Proposal: researcher-ergonomics

## Why

The clarity review (`docs/clarity-review-2026-08.md`, bead `lob-gv6`) filed four
additions under one heading: the library is correct, and the places a
researcher touches it are where it costs them. Each of the four is a verified
finding, re-checked against the tree on 2026-08-14 rather than taken from the
review's snapshot.

1. **`cancelOrder(side, idNum, time=None)` is positional, and side comes
   first** (`src/PyLOB/engine.py:1441`). A reviewer writing a replay loop from
   the documentation bound the identifier to `side` and shipped a latent bug —
   the review's own miniature of the finding. Everything else a caller reaches
   for is keyword-friendly: `submit` takes eight named parameters
   (`engine.py:1365`). The same surface also carries a name split — `submit`
   spells the clock `timestamp=`, `cancelOrder` and `modifyOrder` spell it
   `time=` — so no one can learn it once.
2. **The README sells "Level 2" market data** (`README.md:5-7`) and the read
   side answers "one value at a time" (`README.md:62-64`). The only path to a
   (price, volume) ladder is finding `BookSide.levels()` in the source
   (`engine.py:742`), which is not exported and returns internal `PriceLevel`
   objects. The test suite proves the gap by hand-rolling the aggregation twice
   over: `tests/test_sink_projections.py:544-579` builds the ladder in SQL and
   then rebuilds it in Python out of `snapshot()` to check it.
3. **Recorded sessions are anonymous.** The `session` table holds `seq`,
   `timestamp`, `tick_size`, `stream_version` and nothing else
   (`src/PyLOB/sinks/sqlite.py:326-331`). A parameter sweep therefore produces
   fifty `.db` files with no seed, no episode number and no label anywhere
   inside them; the only identifier is a filename, which is exactly what gets
   lost when the files are moved.
4. **Every researcher rewrites the same unpivot.** The sink computes four
   balance movements per trade (`sqlite.py:1043-1065`) and keeps only their
   running sum in `balance`; the per-trade legs, which are what PnL attribution
   needs, are documented as derivable and left underived
   (`sqlite.py:29-33`). The review verified the unpivot reproduces `balance` to
   1e-9 — and re-checking it against the tree found the caveat the review did
   not state (see `design.md`, decision 4): the currency the sink books a leg
   in is the currency **in force at the fill**, not the one stamped on the
   order, so the obvious hand-written unpivot is silently wrong for any session
   that re-denominates an instrument mid-run (`engine.py:1140-1142`).

All four are additions. `openspec/config.yaml` keeps the public API
(`processOrder`, `cancelOrder`, `modifyOrder`, `getVolumeAtPrice`,
`getBest*`/`getWorst*`, `print`) unless it proves a limiter, and changing it
needs an ADR. **Nothing here changes it.** The existing methods keep their
signatures, their parameter names and their return shapes; the new names sit
alongside. One item does change behaviour a caller can observe, and it is
called out rather than smuggled: the sink's `SCHEMA_VERSION` must go 3 → 4,
and the `trade` table gains a column. See Impact.

## What Changes

- **`OrderBook.depth(instrument, side, levels=None)`** — the aggregated price
  ladder, best price first, one (price, volume) pair per level. Read-side only;
  agrees by construction with `getBestBid`/`getBestAsk`, `getVolumeAtPrice` and
  `snapshot`.
- **`OrderBook.cancel(idNum, *, side=None, timestamp=None)`** and
  **`OrderBook.modify(idNum, *, qty=None, price=None, side=None,
  timestamp=None)`** — keyword companions to `submit`. The identifier comes
  first and everything else is keyword-only, so the misbinding that produced
  the review's bug is not expressible. `timestamp=` throughout, healing the
  `time`/`timestamp` split without touching the old spellings. They delegate to
  `cancelOrder`/`modifyOrder`: same validation, same state change, same events,
  so a stream recorded through either is identical and `replay` is indifferent
  to which was used.
- **`SQLiteSink(path, *, meta=...)` and a `session_meta` table** — caller-
  supplied key/value provenance (seed, episode, label, anything), written when
  the recording opens rather than at close, so a session killed before its
  first flush still names itself. Read back with a new `read_meta`, which does
  not require the log to be complete.
- **`trade_leg`, a view over `trade`** — one row per balance movement per
  trade: `(trade_id, tid, symbol, amount)` and the trade's context. Its
  aggregate reproduces `balance` within floating-point tolerance, including
  across a mid-session re-denomination, which requires `trade` to gain a
  `currency` column recording what the sink actually settled in.
- **Documentation to match**: the sink's "which table answers which question"
  map gains the two new objects, the README's read-side paragraph names
  `depth`, and `example.py` uses `depth`/`cancel`/`modify` (it is executed by
  `./verify`, so the new surface cannot rot).

### Not proposed, and why

- **`ListSink`** — already shipped (`src/PyLOB/sinks/__init__.py:52-66`,
  exported at line 41). Verified present; removed from this change's scope.
- **`replay()`** — needs no proposal, and has landed while this one was being
  written. The `recording-sink` spec already ratifies it
  (`openspec/specs/recording-sink/spec.md:57-61`, scenario "State reconstructs
  from the log"): it was an unreachable ratified contract, not a new
  capability. Verified in the tree as `src/PyLOB/replay.py`,
  `replay(events, *, sink=None) -> tuple[OrderBook, list[Trade]]`, exported
  from `PyLOB`. (The review's "two drifted copies" had already healed to one
  before that: by 2026-08-14 the only test implementation was
  `tests/test_replay.py:229`, imported by `test_emission_coverage.py:98`.)
  Nothing below adds to or alters replay's contract. The interim replay recipe
  named in bead `lob-6zw` belonged to that work and is likewise out of scope
  here.
- **A `reset()` API.** On the review's must-not-break list: engine
  construction costs 0.8 µs and a fresh `OrderBook` per episode is the intended
  reset (`engine.py:1044-1045`). Adding `reset()` would create a second, slower
  way to do it and a state-clearing invariant to maintain.
- **A `trade_leg` *table*** rather than a view. Four extra row-writes per trade
  on the sink's write path buys nothing a view over data already recorded does
  not give (`design.md`, decision 4).
- **Deprecating or renaming `cancelOrder`/`modifyOrder`.** `config.yaml`
  protects them; no deprecation warning, no alias flip, no removal.
- **Carrying session metadata in the event stream** rather than in the sink.
  Rejected on a forward-compatibility hazard; see `design.md`, decision 3.
- **`cancel`/`modify` accepting an `Order` object** as well as an identifier.
  Rejected as written in the bead; see `design.md`, decision 2.

## Capabilities

### New Capabilities

<!-- none: all four additions land in capabilities that already exist. -->

### Modified Capabilities

- `book-queries` — adds the aggregated depth ladder and its agreement with the
  existing read-side queries.
- `order-lifecycle` — adds the keyword-addressed cancel/modify companions and
  the one-name-for-the-clock rule.
- `recording-sink` — adds session metadata and per-trade balance legs.

No existing requirement in any of the three is modified or removed. Every
delta is `## ADDED Requirements`.

## Impact

- **Code**: `src/PyLOB/engine.py` (two new operations, one new query),
  `src/PyLOB/sinks/sqlite.py` (schema, write path, one new reader),
  `src/example.py`, `README.md`. No new modules. No change to
  `src/PyLOB/events.py` — the event vocabulary and `STREAM_VERSION` are
  untouched, so **every existing recorded stream still replays**.
- **Behaviour change, flagged**: `SCHEMA_VERSION` 3 → 4. The sink refuses any
  database whose `PRAGMA user_version` is not exactly `SCHEMA_VERSION`
  (`sqlite.py:1085-1094`), and so do `check_log` and `read_events`
  (`sqlite.py:1276-1286`). After this change, a `.db` recorded by today's
  library cannot be opened, checked or read by the new one. Nothing in this
  repo needs migrating — no `.db` is committed, and `benchmarks/` holds only
  JSON and logs — so the cost falls entirely on a user's own accumulated
  sessions. The bump is **not avoidable** (a new table, a new column and a new
  view all have to be stamped), but the cost to old files may be: see the
  maintainer gate in `tasks.md` and the open question in `design.md`.
- **Behaviour change, flagged**: the `trade` table gains a `currency` column,
  so `SELECT * FROM trade` returns one more field. Named columns are
  unaffected.
- **No change to**: the public API `config.yaml` protects, the event stream,
  `./verify`'s stage list, the wheel's contents, or the dependency set (the
  library still imports the standard library only).
- **Sequencing**: independent of `benchmark-harness`, and of `replay()`, which
  has landed. `replay(events, *, sink=None)` attaches a sink of the caller's
  choosing, so a replayed session labels itself through that sink's own
  metadata — which is the right answer (a replay's provenance is "a replay of
  X", not X's) and needed no coordination between the two changes.
