# Tasks: modify-refuses-filled-orders

## 1. The guard

- [ ] 1.1 `src/PyLOB/engine.py`: in `modifyOrder`, after the cancelled and
      market-order checks and before anything mutates, raise `InvalidOrder`
      when `order.filled`. The message opens like `cancelOrder`'s ("fully
      filled, nothing left to modify"), says what accepting it would do, and
      names the remedy: submit a new order.
- [ ] 1.2 Fold the rule into `modifyOrder`'s docstring beside the other three
      refusals, and make the clamp bullet say that the clamp finishes the
      order for good.

## 2. The tests

- [ ] 2.1 `tests/test_engine_boundaries.py`: an order filled by trading
      refuses a quantity increase, with the book, the order's quantity and
      the emitted events all unchanged.
- [ ] 2.2 Same module: an order clamped to its fulfilled amount by an earlier
      modify refuses identically — the second route into the state, and the
      one the old spec text left ambiguous.
- [ ] 2.3 `tests/acceptance/test_order_lifecycle.py`: one test per new
      scenario, docstringed `Requirement / Scenario` like its neighbours,
      driven through the engine-neutral adapter.

## 3. The oracle and the sweep

- [ ] 3.1 `tests/reference/matcher.py`: the same refusal in `modify`, cited to
      the requirement, so the spec-derived model does not enforce a
      superseded contract.
- [ ] 3.2 Search the suite for anything that modified a filled order, and run
      the differential, replay, sink-equality and projection suites. Report
      rather than quietly adjust any generator found producing the input —
      the generators are owned elsewhere.
- [ ] 3.3 `./verify` exits 0; `ruff format --check` and `ruff check` clean
      over `src` and `tests`.
