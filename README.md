PyLOB
=====

A limit order book for simulation research, written in Python.

PyLOB simulates a limit-order-book exchange so that automated trading
strategies working from "Level 2" market data can be explored offline. It
operates standard price-time priority, supports market and limit orders with
add, cancel and modify, and is single-threaded. Its chief simplifying
assumption is zero latency: a quote is processed the instant it is submitted,
and every other trader can react to it before the next quote arrives.

Two behaviours are worth knowing before the first run: a market order is
immediate-or-cancel and never rests in the book, and a trade prices at the
*maker* — the resting order's limit, not the arriving one's.

Matching is in memory, in one layer. SQLite is optional and off the matching
path: attach a sink and the session's events, trades, balances and commissions
are recorded for querying afterwards; attach nothing and no event is
constructed at all. See [ADR-0001](docs/adr/0001-inmemory-matching-sqlite-sink.md)
for why, and [ADR-0003](docs/adr/0003-retire-the-legacy-sql-engine.md) for the
retirement of the SQL engine that sat behind the same API from 2023 until
2026.

Installation:
=============
PyLOB is not published on PyPI (the `pylob` name there belongs to an unrelated
project). Install it from GitHub; Python 3.11 or newer is required, and there
are no runtime dependencies — the optional recording sink uses the standard
library's `sqlite3`:

    pip install "PyLOB @ git+https://github.com/DrAshBooth/PyLOB.git@v1.0.0"

or, with [uv](https://docs.astral.sh/uv/):

    uv add "PyLOB @ git+https://github.com/DrAshBooth/PyLOB.git@v1.0.0"

**Name the tag.** Both commands work without the `@v1.0.0`, and then they
install whatever is on the default branch at the moment you happen to run
them — which means an experiment run today and re-run in six months is not the
same experiment, and nothing in the results says so. Pin the tag for anything
whose numbers you intend to keep, and record `PyLOB.__version__` alongside
them. `CHANGELOG.md` lists the releases.

To work on PyLOB itself, clone the repo and run `uv sync`; `./verify` is the
definition of done.

Quickstart:
===========
Construct a book, submit an order that rests, submit one that crosses it:

```python
from PyLOB import OrderBook

book = OrderBook(tick_size=0.01)
book.configure_instrument("FAKE", "USD")

book.submit(1, "FAKE", "ask", "limit", 5, 101.0)          # crosses nothing; rests
order, trades = book.submit(2, "FAKE", "bid", "limit", 3, 101.0)

print(trades[0].price)   # 101.0 — the maker's limit, not the taker's
print(order.fulfilled)   # 3
```

`submit(tid, instrument, side, order_type, qty, price)` returns the order and
the executions it caused, in match order. The order goes on answering for
itself afterwards — `order.fulfilled`, `order.remaining`, `order.resting`,
`order.commission` — so nothing else needs to be tracked to see what became of
it. `cancel(idNum)` and `modify(idNum, qty=..., price=...)` change a resting
order afterwards: the identifier comes first and everything else is by
keyword, so nothing can land in the wrong slot, and the clock is `timestamp=`
as it is on `submit`. The 2013 spellings are unchanged and not deprecated:
`cancelOrder` and `modifyOrder` are these two under their original names, and
`processOrder` is `submit` in the dict-quote shape.

`book.print("FAKE")` renders the ladder and the last-trade price (it both
prints and returns the string). `book.depth("FAKE", "bid")` is the Level 2
read: the aggregated ladder as `(price, volume)` pairs, one per price at which
orders rest, best first, with `book.depth("FAKE", "bid", 5)` for the best five
levels alone. `getBestBid`, `getBestAsk`, `getWorstBid`, `getWorstAsk` and
`getVolumeAtPrice` answer one value at a time, and agree with the ladder by
construction rather than by arrangement — all of them read the same levels.

Configuration is optional but consequential. An instrument springs into being
on first mention, and an unconfigured trader pays no commission — but a book
that has never heard the instrument's currency can only move the instrument
leg of a trade, not the cash leg. `configure_trader` sets a trader's
commission schedule and whether it may match its own resting orders.

`src/example.py` is the full walkthrough — limit orders, crossing, partial
fills, market orders, the depth ladder, cancel and modify in both spellings,
the legacy dict-quote API, the same run again with a recording sink attached,
and that recording replayed back into a fresh engine. It is executed on every
`./verify`, so it cannot rot silently.

Sessions and episodes:
======================
One `OrderBook` is one session. It opens at construction and ends at
`close()`, which flushes the sink and does nothing else — it clears neither
the book, nor the order store, nor the ledgers, and on a book with no sink it
does nothing at all.

There is no `reset()`, and none is needed: **construct a fresh `OrderBook` for
each episode.** That is the intended pattern for RL gyms and parameter sweeps,
not a workaround, and the missing method is the answer rather than an omission:
[ADR-0006](docs/adr/0006-no-reset-episode-is-a-fresh-orderbook.md) records why
a `reset()` could not clear the store without giving up the identity rule two
paragraphs down, and why it would not pay for itself even if it could.

Construction is a handful of empty dicts: 0.4 µs for the bare `OrderBook`, and
8.5 µs for one with an instrument configured and twenty traders on it, which
no episode notices. Fresh-per-episode is also, measured, the *faster* pattern.
A hundred episodes of ten thousand `mixed-v1` orders ran at ~183k orders/sec
building a new engine per episode, against ~167k pushing all of them through
one long-lived book — about 10% ahead, with per-episode construction *and*
teardown inside the timed region. The pre-retirement review found the same
direction on its own machine (`docs/engine-review-2026-08.md`); as everywhere
else here, the ratio is the durable part and the absolutes are indicative.

Reusing one book is heavier as well as slower, because a book remembers every
order it has ever seen. The store maps `idNum` to `Order` and is never pruned:
a filled or cancelled order stays addressable for its `fulfilled` and
`commission`, and identifiers stay unique across every instrument the engine
holds for that engine's lifetime, which is what the `order-lifecycle` contract
requires. So a book driven
through a long sweep grows without bound, by design — and it grows linearly,
at about 350 bytes of process memory per order submitted (1M orders cost
356 MB, 2M cost 692 MB; the store itself is a steady 186 B/order and the rest
is the book and the ledgers). Nobody has run ten million orders through one
process, so take 10M × 350 B ≈ 3.5 GB as arithmetic off that slope rather than
as a measurement — but take it seriously before pointing a long sweep at a
single book. A fresh book per episode is what bounds it.

Dropping a book is not free either, and the cost scales with what it retained:
`del` plus a collection takes about 84 ms for a book holding a million orders,
against 1.1 ms for a 5,000-order episode engine — some 4% of the 27 ms that
episode took to run, and already paid inside the throughput figures above. So
the fresh-engine loop pays teardown in proportion, a little at a time, where a
book grown across a whole sweep pays it in one 84 ms-per-million piece
whenever it is finally released. Short episodes are where that matters.

For a sweep, then: run the episodes sinkless — the default, and free rather
than merely cheap, since an engine with no sink constructs no event at all —
and attach a sink to the few runs you mean to inspect afterwards. One database
per session: a sink pointed at a file that already holds a session cannot
write to it.

Label the ones you keep, inside the file rather than in its name — fifty
`.db`s told apart by their filenames stop being told apart the moment somebody
moves them. `SQLiteSink("run-07.db", meta={"seed": 42, "episode": 7})` writes
those keys into the recording, in the opening transaction rather than at
`close`, so an episode killed before its first flush still says which one it
was; `read_meta("run-07.db")` reads them back with their types intact, and
does not ask the log to be complete first — which is the point, since the run
worth naming is usually the one that died.

Recording and inspecting a session:
===================================
Attaching a sink turns the session into queryable history:

```python
from PyLOB import OrderBook
from PyLOB.sinks.sqlite import SQLiteSink

book = OrderBook(tick_size=0.01, sink=SQLiteSink("session.db"))
...
book.close()   # flushes the buffered tail; nothing is guaranteed on disk before
```

`SQLiteSink` is imported from `PyLOB.sinks.sqlite` rather than from `PyLOB`,
so that `import PyLOB` does not drag in `sqlite3` for the majority of callers
who never attach one.

The database has two layers. `event` is the append-only log — one row per
event, the whole event as JSON, the source of truth. `session`, `instrument`,
`trader`, `orders`, `trade` and `balance` are projections of that log: current
state, so that a question about it is a `SELECT` rather than a fold over
200,000 JSON rows. Three views sit on top — `resting_order` (what is still on
the book, with the quantity still available), `trader_commission` (commission
per trader per currency) and `trade_leg`, which unpivots each trade into the
balance movements it caused, one row per (trader, symbol) leg. `balance` keeps
only the running sum of those movements, so `trade_leg` is where per-trade
attribution comes from; summing it back by trader and symbol returns
`balance`, to within what a different order of float additions costs. Three
further tables record something other than the market: `session_meta`, which
holds whatever the run was labelled with, and `session_end` and `event_loss`,
which say what happened to the recording itself.

So, after a run:

```python
import sqlite3
import pandas as pd   # not a PyLOB dependency; sqlite3 on its own does fine

conn = sqlite3.connect("session.db")
trades = pd.read_sql(
    "select timestamp, price, qty, taker_side from trade order by seq", conn
)
resting = pd.read_sql("select * from resting_order", conn)
conn.close()
```

The schema documents itself: every table and column carries its comment inside
the `CREATE` statement, so `sqlite3 session.db .schema` is the reference. Read
the module docstring at the top of
[`src/PyLOB/sinks/sqlite.py`](src/PyLOB/sinks/sqlite.py) for the parts SQL
cannot state — why the log and the projections both exist, what buffering does
and does not change, and how a killed run is told apart from a finished one.
Do not read a recorded database without `check_log`/`read_events`, or at least
without knowing what that header says about `session_end`: a session that was
killed mid-run looks exactly like a shorter one.

A database recorded before `trade_leg` and `session_meta` existed still reads.
The readers take a window of schema versions rather than demanding the current
one ([ADR-0007](docs/adr/0007-sink-readers-accept-a-schema-version-window.md)),
so an older file opens, says on the way in what it does not carry, and answers
what it can. The *writer* stays strict and refuses anything but the current
version, so nothing appends to an old recording.

Replaying a session:
====================
The log, not the database file, is what a session persists. `replay` re-issues
the recorded *commands* — the configuration, the submissions, the
modifications, the cancellations someone asked for — into a fresh engine:

```python
from PyLOB import replay
from PyLOB.sinks.sqlite import read_events

book, trades = replay(read_events("session.db"))
```

The fills are not fed back in. The rebuilt engine matches again and derives
every one of them for itself, so an identical book is evidence of determinism
rather than of a restore — and `recording-sink` requires exactly that: the
reconstructed book snapshot and last-trade price equal the original session's
end state.

`replay` takes an *iterable of events*, not a path. `read_events` is what turns
a `.db` into one; a session kept in memory with `PyLOB.sinks.ListSink` replays
from `sink.events` with no file and no `sqlite3` in sight. That is also why
`import PyLOB` still does not import `sqlite3`, even though `replay` ships in
the package.

Usage and semantics:
====================
What the book is contractually required to do lives in `openspec/specs/`, one
capability per directory:

| Capability | Contract |
| --- | --- |
| `order-lifecycle` | what submissions are accepted, identity, cancel, modify, market orders, priority |
| `order-matching` | fill accounting: fills credited to the right order, never beyond its remainder |
| `book-queries` | best and worst prices, volume at a price, last trade, snapshot |
| `commissions` | the per-unit-with-floor-and-cap schedule, per order, in the instrument's currency |
| `trader-balances` | running per-(trader, instrument-or-currency) balances moved by trades and commissions |
| `recording-sink` | the event stream the core emits and the queryable history a sink turns it into |
| `benchmarking` | seeded deterministic workloads, throughput reported with its context, regression judged against a recorded baseline |

Four of those — `order-lifecycle`, `book-queries`, `commissions` and
`trader-balances` — have acceptance suites in `tests/acceptance/`, written one
test per ratified scenario against an engine-neutral adapter surface. The
other three are guarded by suites of their own: `order-matching` by
`tests/reference/matcher.py`, a matcher written from the frozen specs alone
that shares no code with the engine and is compared against it operation for
operation; `recording-sink` by `tests/test_emission_coverage.py` and the
`tests/test_sink_*.py` suites; and `benchmarking` by
`tests/test_bench_workloads.py` and the harness's own baseline guard.

Design decisions and their rejected alternatives are indexed in
[docs/adr/README.md](docs/adr/README.md).

The [wiki](https://github.com/DrAshBooth/PyLOB/wiki) carries the long-form
versions of what is above: a
[usage walkthrough](https://github.com/DrAshBooth/PyLOB/wiki/Usage-walkthrough)
that goes further than `example.py`, and a
[recording and analysis guide](https://github.com/DrAshBooth/PyLOB/wiki/Recording-and-analysis)
with the queries for reading a session back. Its `Implementation` page is kept
under a banner as history — it describes the red-black-tree implementation PR
#7 replaced in 2023, two engines ago.

Speed:
======
Sinkless matching is fast enough that the strategy under test, rather than the
book, is usually what a simulation waits on. [The pre-retirement
review](docs/engine-review-2026-08.md) measured 130k–307k orders/sec across
seven workload shapes (one-tick, sparse, cancel-heavy, modify-heavy,
monotonic, stale-churn, mixed), with no degradation over a 2M-operation
sustained run. Read that range as an indication of scale and not as a promise:
it was taken by hand on a contended machine, and throughput varies with the
shape of the workload.

Attaching a `SQLiteSink` costs roughly an order of magnitude in throughput:
[ADR-0002](docs/adr/0002-throughput-target-measured-sinkless.md) measured 8x
by hand, and the recorded baseline's own pair — 194,716 sinkless against
17,693 with a sink — is 11x. Take the scale, not the figure.
The cost is the sink's per-event encoding — `dataclasses.asdict` plus
`json.dumps`, plus a projection row-write per event — and not the matching
engine: raising the sink's buffer size from 512 to 16,384 moved the figure by
about 5%. Sinkless remains the default and is the configuration a performance
target governs.

How throughput is *judged* is a separate question, and the answer changed:
[ADR-0005](docs/adr/0005-calibrated-throughput-baselines.md) supersedes
ADR-0002. The floor is on a calibration-normalised figure — not on a raw
orders/sec, and no longer on a ratio to the retired SQL engine, whose 439
orders/sec is a historical origin rather than a live denominator and cannot be
re-measured now that the engine is gone. See below.

Benchmarking:
=============
The harness is `python -m PyLOB.bench`. It measures the engine on a
deterministic workload, compares the result against a recorded baseline, and
answers "did I make it slower?" through its exit code:

    uv run python -m PyLOB.bench            # measure and compare
    uv run python -m PyLOB.bench --list     # the workloads and calibrations
    uv run python -m PyLOB.bench --help     # everything else

    0  no regression, or nothing recorded to compare against
    1  a regression against the recorded baseline
    2  the command line was wrong
    3  the guard could not be applied at all

Three is deliberately not one. "You made it slower" and "I could not find out
whether you made it slower" are different facts, and a caller that cannot tell
them apart goes looking for a performance bug that does not exist.

The harness is **not** part of `./verify`, by standing project constraint: a
correctness gate that takes fifteen seconds must not grow a stage whose answer
depends on what else the laptop is doing.

**Reading the numbers.** A bare orders/sec figure is not comparable across
machines, or even across two runs on one machine — an M1 has performance and
efficiency cores, and a single-threaded run that lands on the wrong one reads
about 40% slow for no reason connected to the code; battery versus mains
changes it again. So every run also times a **calibration** workload:
a fixed, engine-independent reference computation (`calib-v1` — interpreter
dispatch, dict, heap, float and decimal work, in roughly the proportions the
engine's hot path pays them) that measures the machine rather than the code,
and imports nothing from `PyLOB.engine` so that a slower engine cannot slow
its own denominator.

The gating quantity is therefore the **work index**: orders processed per
calibration pass, the median across repeats. A comparison scales the baseline
by the ratio of the two calibration figures, so a machine that is uniformly
30% slower reads as *no regression* rather than as a 30% one. The orders/sec
in the report is the human-readable face of the work index, not the thing
being judged. Every run also records its provenance — machine, CPU, core
counts and which class of core it landed on, Python version, commit, load
average, power source — so a surprising number can be explained rather than
merely disbelieved.

Normalisation is a correction, not a cure. A heavily loaded or thermally
throttled machine still produces noisy numbers; the harness reports such a run
as low confidence rather than silently scaling it.

**Re-baselining.** `--rebaseline` records the current run as the floor,
rewriting `benchmarks/baselines.json` so the move is reviewable in the diff.
It is a deliberate act on a quiet machine, and the harness enforces that: it
refuses to record on a contended machine unless `--force`, and refuses to move
the floor *down* without `--rebaseline-down`, because a floor that can drop on
its own absorbs the very thing it exists to notice. A workload or calibration
whose composition changes gets a new name, which changes the baseline key;
where the name has not changed, the recorded checksums and interpreter
identity are compared on every run, so a baseline that measured something else
refuses to judge rather than judging wrongly.

**The recorded baseline.** `benchmarks/baselines.json` holds one, taken on
2026-08-14 on a quiet Apple M1 on mains power: 194,716 orders/sec sinkless and
17,693 with a `SQLiteSink` attached, against a calibration work index of
11,366. `python -m PyLOB.bench` measures your machine's calibration alongside
its own run and judges the normalised figure against that floor, so the
comparison survives being made on different hardware. It is a floor and not a
target: it is there to notice a regression, not to be beaten.

Where to read next:
===================
**Running simulations:** this README, then `src/example.py`, then
`help(PyLOB)`. After that, as the need arises — the module docstring of
`src/PyLOB/sinks/sqlite.py` for the recorded schema and killed-run recovery,
`src/PyLOB/events.py` for the event vocabulary and the balance rule,
`src/PyLOB/engine.py` for the internals and the cost table, and
`python -m PyLOB.bench --list` for performance.

**Porting code written against the pre-2026 SQL engine:**
[docs/migrating-from-the-legacy-engine.md](docs/migrating-from-the-legacy-engine.md)
lists every behavioural difference, including the ones that answer rather than
raise — a legacy call that used to return `0` or silently no-op and now does
something else is the kind that costs an afternoon.

**Contributing:** `CLAUDE.md`, then the `context:` block of
`openspec/config.yaml` for the standing constraints, then
[docs/adr/README.md](docs/adr/README.md), then `openspec/specs/`. The
executable contracts are `tests/reference/matcher.py` and
`tests/test_emission_coverage.py`.

**History:** the three dated reviews — `docs/architecture-review-2026-08.md`
on the SQL engine PR #7 built, `docs/engine-review-2026-08.md` on the engine
that replaced it, and `docs/clarity-review-2026-08.md` — each carrying a
banner saying what has happened since. Then `brain/architecture.md` — a
2026-08-10 draft written before the in-memory engine landed, so a record of
what was believed then rather than a description of the code now.

The code is open-sourced via the MIT Licence: see [LICENSE.md](LICENSE.md) for
the full text. (copied from http://opensource.org/licenses/mit-license.php)
