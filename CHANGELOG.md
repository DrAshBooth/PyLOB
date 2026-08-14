# Changelog

PyLOB has never been released. There are no earlier tags, so this file starts
at 1.0.0 rather than reconstructing a history it does not have, and the entry
below is not "what changed since last time". It is what the library is — and
first, because it is the part that costs someone an afternoon, what a person
arriving with code written against the engine that came before has to change.

Later entries will follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [semantic versioning](https://semver.org/spec/v2.0.0.html). This one does
not use those categories: with no predecessor, "Added" is every line, and the
distinction worth drawing instead is between the reader who is porting and the
reader who is arriving.

## 1.0.0 — 2026-08-14

A limit order book for simulation research: standard price-time priority,
market and limit orders with add, cancel and modify, single-threaded, and
zero-latency by assumption. Matching is in memory, in one layer. SQLite is
optional and off the matching path — attach a sink and the session is recorded
for querying afterwards, attach nothing and no event is constructed at all.

The library dates from 2013. From 2023 until this year a SQLite-backed engine
did the matching behind the same public API;
[ADR-0003](docs/adr/0003-retire-the-legacy-sql-engine.md) retired it, and the
in-memory engine that
[ADR-0001](docs/adr/0001-inmemory-matching-sqlite-sink.md) specified is now the
only one. There is no `LegacyOrderBook`, and `OrderBook(db=conn)` no longer
means anything.

### Porting from the retired SQL engine

Every name that engine exposed is still here — `processOrder`, `cancelOrder`,
`modifyOrder`, `getVolumeAtPrice`, `getBestBid`/`getBestAsk`,
`getWorstBid`/`getWorstAsk`, `print` — and none of them is deprecated. So a
port is not a rewrite of call sites. It is a question of what those calls now
return, write back and refuse.

[docs/migrating-from-the-legacy-engine.md](docs/migrating-from-the-legacy-engine.md)
enumerates every difference and is the document to work from. What follows is
not a summary of it. It is the handful that **answer rather than raise** — the
ones a port will not discover on its own, because nothing goes wrong loudly.

- **`getVolumeAtPrice` with a side that is not `"bid"` or `"ask"`** returned
  `0`, out of a `coalesce(sum(available), 0)` over rows that matched nothing —
  indistinguishable from a price with no volume resting at it. It now raises
  `InvalidOrder` naming the two valid sides.
- **`clipPrice` still exists and answers differently.** It is the same function
  object as `quantize`. The retired engine computed
  `round(price, int(log10(1/tick)))` — a digit count rather than a grid, which
  treated 0.25, 1 and 5 alike and put 100.03 on a 0.05 tick at **100.0**. The
  grid is real now, in `decimal`, and the same call gives **100.05**. Ordinary
  decimal prices on the default 0.0001 tick are unaffected; a simulation run on
  a non-decimal tick (0.05, 0.25, 5) will produce different resting prices.
- **A market order's unfilled remainder no longer rests.** It used to sit in
  the book at a NULL price, outranking every limit order indefinitely, invisible
  to `getBestBid`/`getBestAsk` — which reported `None` over a book that was not
  empty. Market orders are immediate-or-cancel now, and the remainder is
  cancelled with `cancel_reason="ioc_remainder"`. A simulation that leaned on
  resting market orders for liquidity will fill less than it used to.
- **A trade prices at the maker.** The retired engine priced a fill against a
  resting NULL-price market order at the *taker's* price instead.
- **`modifyOrder` no longer writes into the dict you pass it.** It used to set
  `idNum`, `timestamp`, `type`, `order_id`, `instrument` and `fulfilled` on your
  `orderUpdate` and quantize its `price` in place. It now reads it and leaves it
  untouched. Code that read results back out of the update dict —
  `orderUpdate["fulfilled"]` was the common one — should ask the order:
  `book.order(idNum).fulfilled`.
- **`modifyOrder` with `price=None` means "leave the price alone".** It
  emphatically does not mean "become a market order", which is how the retired
  engine read it while keeping the order's stored limit.
- **A reprice always re-crosses the book.** The retired engine re-matched only
  when its `betterPrice` test judged the new price more aggressive.
- **Zero is a value now.** `cancelOrder(side, idNum, time=0)` tested `if time:`,
  so `0` meant "nothing supplied, tick the clock". It now sets the clock to 0.
  Only `None` means "not supplied", for `cancelOrder`, `modifyOrder` and
  `submit`/`processOrder` alike. The clock is a `float` by default where it used
  to be an `int`.

The rest of the differences announce themselves, and are the cheap ones:
`Trade` is a 10-field `NamedTuple` whose two identifiers are `idNum`s rather
than SQLite rowids, so five-way unpacking raises `ValueError` instead of quietly
handing back the wrong columns; `quote["order_id"]` and `quote["lastprice"]` are
gone and raise `KeyError`; `OrderBook(connection)` complains about the tick
size, because the first parameter is `tick_size`. The migration document's
tables cover these and the refusals that used to be silent — cancelling an
unknown, mismatched, already-cancelled or fully filled order all did nothing at
all, and reported nothing.

Bad input raises rather than terminating the host process: `processOrder` used
to call `sys.exit()` on a non-positive quantity, an unknown type and an unknown
side. Nothing moves when a submission is refused — no order, no event, no clock
tick, no identifier consumed — so a caught `InvalidOrder` leaves a book you can
carry on using.

### Fixed, in code that may still be running

GitHub issue #8 documented two independent fill-accounting bugs in the retired
engine, both of which let a resting order be matched past its true size,
corrupting fills, trader balances and counterparty liquidity. The `trade_insert`
trigger credited fills to rows matched by `idNum` while the trade table stored
`order_id` values, so fills landed on the wrong orders whenever the two
identifier sequences diverged; and `processMatchesDB` took the quantity to
execute from the quote without subtracting `fulfilled`, so a repriced, partially
filled order re-traded its full size.

Both were fixed in the SQL engine before it was retired, and cannot recur here:
there is one identifier, one layer doing the accounting, and a ratified
`order-matching` contract requiring that fills be credited to the orders that
traded and that no order ever trade beyond its unfulfilled remainder. Nine
further defects specific to that engine closed as won't-fix with the code that
carried them (ADR-0003).

### The engine

- One `OrderBook` holds many instruments; an instrument springs into being on
  first mention. Order identifiers are unique across every instrument the
  engine holds, for that engine's lifetime, and `cancel`/`modify` reach an
  order by identifier without being told the instrument. Two engines both start
  at 1 — the scope is the engine, not the process.
- `submit(tid, instrument, side, order_type, qty, price)` returns
  `(order, trades)` in match order. The `Order` goes on answering for itself —
  `fulfilled`, `remaining`, `resting`, `commission` — so nothing else needs to
  be tracked to see what became of it. `processOrder` is the same operation in
  the 2013 dict-quote shape, returning `(trades, quote)`.
- `cancel(idNum, *, side=None, timestamp=None)` and
  `modify(idNum, *, qty=None, price=None, side=None, timestamp=None)`: the
  identifier comes first and everything else is keyword-only, so it cannot land
  in the wrong slot, and the clock is spelled `timestamp=` throughout.
  `cancelOrder` and `modifyOrder` are these two under their original names and
  original positional signatures.
- Every refusal is an exception. `PyLOBError` is the base of everything the
  library raises; `InvalidOrder` is also a `ValueError`, `UnknownOrder` also a
  `LookupError`, and `DuplicateOrderID` is an `InvalidOrder`.
- Modify is validated and priority-aware: the side is immutable, a quantity
  reduction keeps time priority, a quantity increase or a price change loses it,
  and a fully filled order is finished — modify refuses it, as cancel already
  did.
- Commissions (per unit, with a floor and a percentage cap), per-trader
  balances and a per-trader self-matching gate, all computed in core so PnL is
  available online rather than after a query. Balances **track and do not
  gate**: there is no margin or sufficient-funds enforcement, and a seller's
  instrument balance goes negative freely. That is a stated requirement, so
  nobody quietly "fixes" it.
- Prices are quantized to a real tick grid. The tick is fixed for a book's
  life, so a different grid means a new `OrderBook` rather than a setter.

### The read side

`depth(instrument, side, levels=None)` is the Level 2 read: the aggregated
ladder as `(price, volume)` pairs, one per price at which orders rest, best
first. `getBestBid`, `getBestAsk`, `getWorstBid`, `getWorstAsk`,
`getVolumeAtPrice`, `getLastPrice`, `snapshot` and `print` answer beside it, and
agree with the ladder by construction rather than by arrangement — all of them
read the same levels. Every one of them is scoped to a single instrument, which
the `book-queries` contract now says in as many words rather than leaving to be
derived from a neighbouring requirement.

### Recording, replay and inspection

- Sinks are optional and off the hot path. With none attached, no event is
  constructed at all.
- `PyLOB.sinks.sqlite.SQLiteSink` turns a session into queryable history: an
  append-only `event` log as the source of truth, projections of it
  (`session`, `instrument`, `trader`, `orders`, `trade`, `balance`) so that a
  question is a `SELECT` rather than a fold over the log, three views
  (`resting_order`, `trader_commission`, `trade_leg`) and three tables about the
  recording itself (`session_meta`, `session_end`, `event_loss`). Every table
  and column carries its comment inside the `CREATE`, so `.schema` is the
  reference.
- `SQLiteSink(path, meta={...})` labels a run *inside* the file — written when
  the recording opens rather than at close, so a run killed before its first
  flush still says which one it was. `read_meta` reads it back with types
  intact and does not require a complete log, which is the point: the run worth
  naming is usually the one that died. `check_log` and `read_events` are how a
  killed session is told apart from a shorter one.
- `trade_leg` unpivots each trade into the balance movements it caused, one row
  per (trader, symbol), booked in the currency in force at the fill.
- `replay(events)` re-issues the recorded *commands* into a fresh engine and
  lets that engine derive every fill again, so an identical book is evidence of
  determinism rather than of a restore. It takes an iterable of events, not a
  path, so a session held in memory by `PyLOB.sinks.ListSink` replays with no
  file and no `sqlite3` involved.
- `import PyLOB` does not import `sqlite3`. That is a promise, and a subprocess
  test holds it.
- The sink schema is at version 4. *Readers* accept a window down to version 3,
  so a recording made before `trade_leg` and `session_meta` existed still opens,
  says on the way in what it does not carry, and answers what it can; the
  *writer* stays exact, so nothing appends to an old recording
  ([ADR-0007](docs/adr/0007-sink-readers-accept-a-schema-version-window.md)).
- A sink observes the stream and does not act on the engine: `consume` runs
  inside the operation being recorded, a sink must not call back into the
  engine from it, and the engine does not detect or refuse one that does. The
  non-enforcement is ratified deliberately, so adding a guard later is a
  contract change rather than a tidy-up.

### Benchmarking

`python -m PyLOB.bench` measures the engine on a seeded, deterministic
workload, compares the result against a recorded baseline, and answers "did I
make it slower?" through its exit code: `0` no regression (or nothing recorded
to compare against), `1` a regression, `2` a bad command line, `3` the guard
could not be applied at all. Three is deliberately not one — "you made it
slower" and "I could not find out whether you made it slower" are different
facts.

Every run also times an engine-independent calibration workload and gates on
the **work index**, orders processed per calibration pass, so a machine that is
uniformly slower reads as no regression rather than as a regression the size of
the difference ([ADR-0005](docs/adr/0005-calibrated-throughput-baselines.md),
superseding ADR-0002's ratio to an engine that no longer exists to be
re-measured). Each run records its provenance — machine, CPU, core counts and
which class of core it landed on, Python version, commit, load average, power
source. `--rebaseline` moves the floor as a deliberate, reviewable act; it
refuses a contended machine without `--force` and refuses to move the floor
down without `--rebaseline-down`.

The harness is **not** part of `./verify`, by standing project constraint: a
fifteen-second correctness gate must not grow a stage whose answer depends on
what else the laptop is doing.

The first baseline is recorded in `benchmarks/baselines.json` — `mixed-v1`,
20,000 orders, seed 42, five repeats on an Apple M1 on mains power. Treat the
figures there and in [`docs/engine-review-2026-08.md`](docs/engine-review-2026-08.md)
(130k–307k orders/sec across seven workload shapes, no degradation over a
2M-operation sustained run) as measurements recorded on particular machines
rather than as promises about yours; a bare orders/sec figure is not comparable
across machines, or even across two runs on one, which is the entire reason the
work index exists. Attaching a `SQLiteSink` costs roughly an order of magnitude
of throughput — ADR-0002 measured about 8x, and the recorded baseline's own
sinkless and sink figures are about 11x apart. The cost is the sink's per-event
encoding, not the matching engine.

### What is contractually promised

Seven capabilities are ratified in `openspec/specs/`, one directory each:
`order-lifecycle` (what is accepted, identity, cancel, modify, market orders,
priority), `order-matching` (fill accounting), `book-queries` (best and worst
prices, volume at a price, last trade, snapshot, the depth ladder),
`commissions`, `trader-balances`, `recording-sink` and `benchmarking`.

Four are covered by acceptance suites in `tests/acceptance/`, one test per
ratified scenario against an engine-neutral adapter. `order-matching` is
guarded by `tests/reference/matcher.py` — a second matcher written from the
frozen specs alone, sharing no code with the engine, compared against it
operation for operation. `recording-sink` is guarded by the emission-coverage
and sink suites, and `benchmarking` by its workload tests and the harness's own
baseline guard.

Not every behaviour the engine has is ratified, and the difference is worth
knowing before you depend on one. Of cancel's terminal refusals, only the
unknown-identifier case is required by a spec; refusing an already-cancelled or
fully filled order is current engine behaviour that no requirement states.
The same is true of `processOrder(..., fromData=True)` refusing a quote with no
`idNum` or `timestamp`. `tests/reference/matcher.py` marks these where it
implements them.

### Decisions

`docs/adr/README.md` is the index; the records themselves carry the rejected
alternatives, which is the part that leaves no other trace.

- **ADR-0001** — matching moves in memory; SQLite becomes an optional
  off-hot-path sink.
- **ADR-0002** — the throughput target is measured with no sink attached.
  *Superseded by ADR-0005.*
- **ADR-0003** — the legacy SQL engine is retired, and its differential oracle
  is replaced by a spec-derived reference matcher rather than deleted. What was
  genuinely lost is recorded there too: an independent *implementation*, written
  by someone else at another time.
- **ADR-0004** — `Trade` is a `NamedTuple` and not a frozen dataclass, worth a
  measured 9% of the sinkless hot path. The type widens to a tuple, so field
  order became public surface.
- **ADR-0005** — throughput is judged against a calibration-normalised
  baseline. *Supersedes ADR-0002.*
- **ADR-0006** — there is no `reset()`, and `close()` clears nothing; an episode
  is a fresh `OrderBook`.
- **ADR-0007** — sink readers accept a schema-version window; the writer stays
  exact.

### Requirements and installation

Python 3.11 or newer. No runtime dependencies — the optional recording sink
uses the standard library's `sqlite3`. MIT licensed.

Not published on PyPI: the `pylob` name there belongs to an unrelated project.
Install from GitHub:

    pip install "PyLOB @ git+https://github.com/DrAshBooth/PyLOB.git"

`./verify` is the definition of done for contributors, in six stages: format,
lint, test, specs, smoke and packaging. `src/example.py` is executed by the
smoke stage on every run, so the walkthrough cannot rot silently.

### Deliberately not included

- **No `reset()`.** Construct a fresh `OrderBook` for each episode — the
  intended pattern for RL gyms and parameter sweeps, measured faster than
  reusing one book, and the only arrangement that bounds memory. A book
  remembers every order it has ever seen, by design: a filled or cancelled
  order stays addressable for its `fulfilled` and `commission`, which is what
  the identity rule requires. `close()` flushes the sink and does nothing else.
- **No margin, and no sufficient-funds check.** Balances track.
- **No trade log inside the engine.** `getLastPrice` is what it keeps; attach a
  sink for history.
- **No enforcement of the sink re-entrancy prohibition**, as above.
- **No deprecation of the 2013 spellings.** `processOrder`, `cancelOrder` and
  `modifyOrder` are supported surface, not a compatibility shim.

Zero latency and a single thread are simplifying assumptions rather than
omissions: a quote is processed the instant it is submitted, and every other
trader can react to it before the next quote arrives.
