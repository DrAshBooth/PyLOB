# Tasks: researcher-ergonomics

Each numbered task is a landing: it ends with `./verify` green and nothing
half-built behind it. Dependencies are stated per task and are the only ones —
2 and 3 depend on nothing and on each other not at all, so they can run in
parallel with 1 and with 4.

## 1. Decide before the schema moves

- [ ] 1.1 **MAINTAINER GATE** — schema-version compatibility window. Decide
      whether the sink's *readers* (`check_log`, `read_events`, and the
      `read_meta` of task 4.2) accept a window of schema versions or keep
      demanding equality. `design.md`'s open question states the case: the
      reasoning that forced the 1→2 and 2→3 bumps does not apply to a table
      whose absence is an honest answer, and a window of [3, 4] would let every
      existing recording go on being read. Recommendation: take it, writer
      unchanged, and write the ADR — it rejects an option and sets a
      compatibility policy the module does not currently have, which is
      exactly the rationale that leaves no other trace. Deciding *before* task
      4 lands is the whole point: a hard cut shipped and later softened costs
      users a migration they did not need.
      *Blocks 4.1. Not an agent's to claim.*

## 2. The price ladder

- [ ] 2.1 `OrderBook.depth(instrument, side, levels=None)` in
      `src/PyLOB/engine.py`: aggregated (price, volume) pairs, best price
      first, bounded by `levels`, empty ladder for an empty side, library
      exception for a non-positive bound. Reads `PriceLevel.volume` — no walk
      over orders, no new state. Acceptance coverage in
      `tests/acceptance/test_book_queries.py` for both requirements of the
      `book-queries` delta, including the cumulative-depth agreement with
      `getVolumeAtPrice` and the aggregation agreement with `snapshot`
      (`tests/test_sink_projections.py:544-579` already asserts both relations
      by hand and is the model).
      *Depends on nothing.*

## 3. Cancel and modify by keyword

- [ ] 3.1 `OrderBook.cancel(idNum, *, side=None, timestamp=None)` and
      `OrderBook.modify(idNum, *, qty=None, price=None, side=None,
      timestamp=None)` in `src/PyLOB/engine.py`, both delegating to
      `cancelOrder`/`modifyOrder` — no second copy of the clamp rule, the
      priority rule or the validation order. `modify` returns
      `(order, trades)`, mirroring `submit`; it raises when neither `qty` nor
      `price` is named. Cross-reference the docstrings both ways. Acceptance
      coverage in `tests/acceptance/test_order_lifecycle.py` for the three
      requirements of the `order-lifecycle` delta, with the
      either-spelling-records-the-same-stream scenario asserted against a real
      recorded stream rather than by inspection.
      *Depends on nothing. `cancelOrder`/`modifyOrder` are untouched: no
      renamed parameters, no deprecation, no warnings.*

## 4. The sink

- [ ] 4.1 One schema revision to `SCHEMA_VERSION = 4` in
      `src/PyLOB/sinks/sqlite.py`, landing all three objects and the code that
      populates them together (see `design.md` decision 5 — a partial landing
      produces version-4 files that are missing a column no `CREATE TABLE IF
      NOT EXISTS` can add later):
      - `session_meta(key, value)`, written in the opening transaction, fed by
        a new `SQLiteSink(path, *, meta=...)` keyword;
      - `trade.currency`, the currency the sink actually settled the legs in,
        `NULL` when the instrument had none;
      - the `trade_leg` view, unpivoting each trade row into its balance
        movements.
      Every column and the view carry their comments *inside* their `CREATE`
      statements — `.schema` stays self-describing, which is on the review's
      must-not-break list. Bump the `SCHEMA_VERSION` docstring's history note
      with the reason for 4. Coverage in `tests/test_sink_projections.py` (the
      aggregate equals `balance` within tolerance, across two instruments and
      two currencies, across a mid-session re-denomination, and for an
      instrument with no declared currency) and `tests/test_sink_durability.py`
      (metadata survives a session killed before its first flush; a metadata
      -carrying session records the same stream as one without).
      *Depends on 1.1.*
- [ ] 4.2 `read_meta(source)` in `src/PyLOB/sinks/sqlite.py`, exported from
      `PyLOB.sinks`: returns the recorded metadata as a plain dict, does not
      call `check_log` (an incomplete log's metadata was committed before any
      event and is exactly as trustworthy as a complete one's), and implements
      whatever version window task 1.1 settled on — as does `check_log` and
      `read_events` if the answer was a window. Coverage for reading metadata
      out of an incomplete log and, if the window was taken, for a version-3
      file reading and a version-3 file still being refused for writing.
      *Depends on 4.1.*

## 5. The front door

- [ ] 5.1 Documentation, in one pass so the reading map is written once:
      `sinks/sqlite.py`'s "which table answers which question" map gains
      `session_meta` and `trade_leg` (and the sentence at lines 29-33 saying
      the movements are merely derivable is retired); the README's read-side
      paragraph names `depth` where it currently says the queries answer "one
      value at a time", and its sessions-and-episodes paragraph says how to
      label a sweep; `src/example.py` uses `depth`, `cancel` and `modify` — it
      runs on every `./verify`, so the new surface cannot rot silently.
      *Depends on 2.1, 3.1, 4.1, 4.2. `replay()` has landed, so the README's
      recording section can be written once, covering both.*

---

Not in this change, and deliberately: `ListSink` (shipped),
`replay()` and its interim recipe (already ratified by the `recording-sink`
spec and in flight separately), a `reset()` API, a `trade_leg` table, an
`Order`-accepting `cancel`/`modify`, and any deprecation of the legacy
spellings. `proposal.md` carries the reasoning for each.

These boxes are frozen planning input. Beads are the execution source of
truth once the maintainer converts them; reconcile at archive time.
