# Tasks: trade-identifiers-survive-replay

No behaviour changes. Every task below either pins the ratified rule with a
test, or states it where a reader will meet it. A task that turns out to need an
engine change has found a bug: file it against this change's bead and stop,
rather than folding a fix in here.

Two of these files carry the property already. The work there is to make the
assertion say what it is for, so that a later tidy-up cannot drop it while
believing nothing was promised.

## 1. The scenarios

- [ ] 1.1 `tests/test_engine_bookkeeping.py`: executions on two instruments of
      one engine, **sinkless**, carry distinct identifiers. Beside
      `test_priority_is_a_strictly_increasing_arrival_stamp`, which is the same
      kind of test about the counter next door. Assert distinctness and that
      every reported `Trade` carries an identifier — **not** the sequence
      1, 2, 3: density is deliberately not ratified (`design.md` decision 2),
      and a test asserting it would re-impose what the requirement declined to
      promise.
- [ ] 1.2 `tests/test_replay.py`: the replay scenario, asserted in its own
      right. It is true today only inside `trade_log()`, whose field list is not
      an argument. Give it a named assertion (or its own test) that cites
      `order-matching`, "Trade identifiers are unique and reproducible", and
      says what breaks without it: a researcher joining a replayed run against
      the `trade` table it was recorded into.
- [ ] 1.3 Leave `trade_log()` itself carrying `trade_id`. 1.2 is what stops the
      field being read as incidental; removing it from the tuple would lose the
      whole-trade comparison the replay and sink-equality suites share.
- [ ] 1.4 Both tests docstringed `Requirement / Scenario`, the convention the
      acceptance suites follow, even though neither module is one — these
      scenarios need `Trade.trade_id`, which the engine-neutral adapter surface
      does not carry and is not being given (`design.md` decision 4).

## 2. The citations

- [ ] 2.1 `src/PyLOB/events.py`, the "Replay" section: it promises "the next
      trade identifier `max(trade_id) + 1` ... so an order submitted after a
      reload cannot collide with one from before it", and cites
      `order-lifecycle`'s identifier clause, which is about **order**
      identifiers. Two corrections: cite the new requirement, and describe the
      mechanism as it is — nothing computes `max(trade_id)`; the counter arrives
      at `N + 1` because the replay re-derives all N executions, and `Filled` is
      filtered out of the replay entirely. The `max(idNum) + 1` half of the same
      sentence is accurate and stays.
- [ ] 2.2 `src/PyLOB/engine.py`, `next_trade_id`: this is where a reader meets
      the counter, and its "Not replay-coherent" note is the other half of the
      ratified property — one identifier per execution and none taken outside
      one is exactly what makes a replay re-derive the same numbers. Say so, and
      cite the requirement. Comment only.
- [ ] 2.3 While in `engine.py`: `_execute` takes its identifier outside the
      `if self.recording:` gate, which is what makes the sinkless clause true.
      Confirm it and leave a one-line note there if it is not already obvious;
      moving that call inside the gate is the way this requirement breaks.
- [ ] 2.4 `grep -rn "trade_id" src tests docs` for any third place that
      describes what a trade identifier promises.

## 3. The reliance this change does not ratify

- [ ] 3.1 `tests/test_sink_projections.py`, `assert_trades_match`: reads the
      trade table `ORDER BY trade_id` and compares it to the engine's trade list
      in order, which relies on identifiers increasing along the stream —
      declined in `design.md` decision 2. Change it to `ORDER BY seq`, the key
      `recording-sink` guarantees. Same rows, ratified ordering.
- [ ] 3.2 Sweep for any other reader that recovers execution order from
      `trade_id` rather than `seq` (`grep -rn "ORDER BY trade_id\|order by
      trade_id" src tests docs benchmarks`). Report anything found outside
      `tests/`; `src/example.py` selects trades without ordering and needs
      nothing.

## 4. What is deliberately not done

- [ ] 4.1 Do **not** add `trade_id` to `tests/harness/surface.py`'s `Trade`, and
      do not give `tests/reference/matcher.py` a trade-identifier counter. That
      would make `tests/test_differential.py` compare identifiers between the
      two implementations — a stricter oracle, and a decision for its own change
      (`design.md` decision 4).
- [ ] 4.2 No delta and no edit for `recording-sink` or `order-lifecycle`. The
      sink's `trade.trade_id INTEGER PRIMARY KEY` depends on this requirement
      and correctly does not restate it.

## 5. Done

- [ ] 5.1 `./verify` exits 0, including its `specs` stage; `ruff format --check`
      and `ruff check` clean over `src` and `tests`.
