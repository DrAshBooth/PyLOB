# Tasks: sink-re-entrancy-contract

The code and the docstrings this change ratifies already landed under
`lob-k3h`. What is left is the spec and the reconciliation, so nothing here
touches `src/`.

## 1. The spec

- [ ] 1.1 `openspec/specs/recording-sink/spec.md`: add the requirement from
      `specs/recording-sink/spec.md`, unedited. It states three things and all
      three are load-bearing — the synchronous call, the sink's prohibition,
      and the engine's non-enforcement. Dropping the third turns a decision
      into a hint.

## 2. Reconciliation

- [ ] 2.1 Confirm the three scenarios are covered. They are, in
      `tests/test_engine_boundaries.py`, section `lob-k3h`:
      `test_a_sink_reading_the_book_mid_walk_is_told_two_different_things`
      covers the first two (it asserts the disagreement from inside `consume`
      and the agreement after the return) and
      `test_a_sink_cancelling_mid_walk_strands_liquidity_and_crosses_the_book`
      covers the third. No new tests. If the maintainer wants them under
      `tests/acceptance/`, that is a new suite for a capability that has none,
      and it needs the engine-neutral adapter to grow a sink surface — decide
      before writing.
- [ ] 2.2 `tests/reference/matcher.py`: nothing to do. The reference matcher
      is the matching oracle and has no sink; confirm rather than assume, so
      the "the oracle follows the spec" step is answered rather than skipped.
- [ ] 2.3 Re-read the three docstrings the requirement now backs —
      `events.EventSink`, `engine.OrderBook`, `engine.OrderBook.emit` — and
      cite `recording-sink` where they currently cite only each other. One
      line each; this is the only edit to `src/` the change permits.

## 3. Done

- [ ] 3.1 `./verify` exits 0. Nothing in this change should be able to move
      it, which is itself worth confirming: if it does, something was
      implemented that this change said it would not implement.
