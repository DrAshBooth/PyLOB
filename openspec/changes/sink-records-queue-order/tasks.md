# Tasks: sink-records-queue-order

The behaviour this change ratifies already ships, and most of it is already
checked. What is left is the spec, one test, and the reconciliation — nothing
here changes `src/` beyond a citation.

## 1. The spec

- [ ] 1.1 `openspec/specs/recording-sink/spec.md`: add the requirement from
      `specs/recording-sink/spec.md`, unedited. Both halves are load-bearing —
      the ordering promise and the four things the value does not promise.
      Dropping the second half ratifies the number by implication, which is
      what the change exists to prevent (`design.md`, decision 1).

## 2. Reconciliation

- [ ] 2.1 Scenario "The resting orders read back as the book" is covered.
      `tests/test_sink_projections.py::assert_resting_orders_match` sorts
      `resting_order` by best price then the recorded ordering value and
      compares it position by position against `OrderBook.snapshot()`, for both
      sides of both instruments, on both the scripted and the randomised
      workload. Confirm rather than assume, then bind it: the function's
      docstring cites `book-queries` for why the snapshot's tuple position is
      the answer, and should now also cite the `recording-sink` requirement
      that makes the SQL side of the comparison a contract.
- [ ] 2.2 Scenario "The order survives a reload" is covered by
      `tests/test_replay.py::test_replay_re_emits_an_identical_stream`: a
      replay recorded through its own sink emits the identical stream, and the
      projections are a fold of the stream, so both recordings' resting orders
      read back identically — order included. Confirm the chain holds (the
      test compares streams, not projections) and, if it does not close the
      scenario on its own, add the projection comparison there rather than
      writing a third reload test.
- [ ] 2.3 Scenario "An order that lost priority is recorded behind the one that
      overtook it" needs a test. The engine-side half exists
      (`tests/acceptance/test_order_lifecycle.py::test_price_change_loses_time_priority`,
      for `order-lifecycle`'s "Price change loses time priority"); the recorded
      half does not, because `test_sink_projections.py`'s scripted session
      reprices an order into a cross rather than back onto an occupied level.
      Add it to `tests/test_sink_projections.py`, and assert what the engine
      cannot: that reading the two orders by acceptance order puts them the
      wrong way round, so the test fails if the ordering value ever silently
      becomes an arrival order.
- [ ] 2.4 Do **not** loosen `assert_resting_orders_match`'s row-by-row equality
      of the recorded value with `Order.priority` on the strength of the new
      non-promises. It is a projection-fidelity check inside one session, not a
      promise to a reader holding a file, and it is what fails first if the
      fold starts inventing values (`design.md`, Risks).
- [ ] 2.5 `tests/reference/matcher.py`: nothing to do. The reference matcher has
      no sink and records nothing; confirm rather than assume, so the "does the
      oracle follow the spec" step is answered rather than skipped.
- [ ] 2.6 `src/PyLOB/sinks/sqlite.py`: the `orders.priority` column comment is
      currently one word (`priority INTEGER NOT NULL`) and the
      `resting_order` view says only "What is still on the book, with the
      quantity still available to trade". Give each one line citing the new
      requirement — what the ordering promises and what the number does not.
      The schema documents itself and is the reference a reader reaches for
      (`sqlite3 session.db .schema`), so this is where the non-promise has to
      be legible. This is the only edit to `src/` the change permits.

## 3. Done

- [ ] 3.1 `./verify` exits 0, including its `specs` stage. Only task 2.3 should
      be able to move the `test` stage; if anything else does, something was
      implemented that this change said it would not implement.
