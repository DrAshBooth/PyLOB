# Tasks: identifiers-unique-per-engine

No behaviour changes. Every task below either states the ratified rule where a
reader will meet it, or pins it with a test. A task that turns out to need an
engine change has found a bug: file it against this change's bead and stop,
rather than folding a fix in here.

## 1. The acceptance scenarios

- [ ] 1.1 `tests/acceptance/test_order_lifecycle.py`: identifiers do not
      restart per instrument. Submit alternately on two instruments of one
      engine (`engine_factory(instruments=...)`, second symbol sorting *before*
      the default so an engine answering from the wrong book answers visibly
      wrong) and assert the identifiers are all distinct. Assert distinctness,
      not the sequence — the requirement promises uniqueness, not a counter.
- [ ] 1.2 Same module: a supplied identifier already issued on a *different*
      instrument is rejected, and neither instrument's book changes.
- [ ] 1.3 Same module: the identifier of a cancelled order, and of a fully
      filled one, is refused a later replay submission.
- [ ] 1.4 Same module: cancel reaches an order on the engine's second
      instrument by identifier alone, naming no instrument, and leaves the
      first instrument's book untouched.
- [ ] 1.5 Each test docstringed `Requirement / Scenario` like its neighbours
      and driven through the engine-neutral adapter, never an engine's own API.

## 2. The citations

- [ ] 2.1 `src/PyLOB/engine.py`: the "Identity" section of the module docstring
      justifies never pruning `_orders` by quoting "within the book's
      lifetime". Requote the ratified clause, and say the scope out loud — one
      identifier space for every instrument the engine holds. Comment only.
- [ ] 2.2 `tests/reference/matcher.py`: the `orders` field comment cites the
      same clause for the same reason. Same edit. Confirm while there that the
      reference matcher's allocation is engine-wide (one `orders` dict, one
      `_next_idNum`) and matches the ratified rule — it should, and a
      difference is a bug to file.
- [ ] 2.3 `grep -rn "within the book's lifetime"` across `src`, `tests` and
      `docs` for any third copy.

## 3. The sweep

- [ ] 3.1 Confirm no differential, replay, sink-equality or projection
      generator supplies an identifier per instrument or reuses a finished
      order's. They draw from a book snapshot, so they should not; report
      rather than quietly adjust anything found, since the generators are owned
      elsewhere.
- [ ] 3.2 `./verify` exits 0; `ruff format --check` and `ruff check` clean over
      `src` and `tests`.
