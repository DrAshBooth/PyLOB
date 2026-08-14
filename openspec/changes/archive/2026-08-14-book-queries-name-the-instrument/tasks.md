# Tasks: book-queries-name-the-instrument

No behaviour change. Both tasks pin a rule the engine and the reference
matcher already follow. A task that turns out to need an engine change has
found a bug: file it and stop, rather than folding a fix in here.

## 1. The scenarios

- [ ] 1.1 `tests/acceptance/test_book_queries.py`: a test for
      *Another instrument's orders do not move these prices*. Two instruments
      in one engine via `engine_factory(instruments=...)`, the second quoted
      **inside and across** the first's spread and the wrong way round, so a
      pooled engine answers visibly wrong rather than coincidentally right.
      Assert both sides of both instruments, and the empty-instrument case.
      The module already has `OTHER = "AAA"` and its rationale comment; use
      them rather than adding a third symbol.
- [ ] 1.2 Same module: a test for *Another instrument's volume at the same
      price is excluded*. The same price on both instruments is the point —
      different prices would pass under a pooled implementation that filtered
      by price alone.
- [ ] 1.3 Both docstringed `Requirement` / `Scenario` like their neighbours,
      quoting the ratified text, and driven through the engine-neutral adapter
      rather than the engine's own API.

## 2. Non-vacuity

- [ ] 2.1 Confirm each new test fails against an engine whose book lookup
      ignores its instrument argument. It should — but the whole reason this
      change exists is that six existing tests already fail under that mutation
      while none of them is bound to these two requirements, so "something
      catches it" is not the bar. **These** tests must catch it.
      Do this by monkeypatching in a scratch copy or a pytest plugin, not by
      editing `src/`.

## 3. Done

- [ ] 3.1 `./verify` exits 0. It should not move otherwise: nothing here
      changes behaviour, and its `specs` stage will validate the ratified text
      once this change is archived.

---

These boxes are frozen planning input. Beads are the execution source of truth
once the maintainer converts them; reconcile at archive time.
