# Tasks: spec-book-queries

Spec-first change: the deliverable is ratified contracts plus executable
acceptance tests that `inmemory-engine` will be built against. Tests written
here are expected to fail (or be skipped) against the legacy engine — they
encode target behavior, and get wired into `./verify` only when
`inmemory-engine` lands.

## 1. Ratification

- [x] 1.1 Maintainer confirms or amends design decision 2 (modify priority:
      price change / qty increase lose time priority; qty decrease keeps it)
      — confirmed as written, 2026-08-10
- [x] 1.2 Freeze both spec files; any later edit is a new delta, not an
      in-place change

## 2. Acceptance tests (target-behavior, marked for the new engine)

- [x] 2.1 Write `tests/acceptance/test_order_lifecycle.py` covering every
      scenario in `specs/order-lifecycle/spec.md`, parameterized over an
      engine fixture (legacy engine cases marked xfail where behavior
      intentionally diverges, e.g. IOC)
- [x] 2.2 Write `tests/acceptance/test_book_queries.py` covering every
      scenario in `specs/book-queries/spec.md`, same fixture pattern
- [x] 2.3 Verify the suite runs green with the new-engine cases skipped
      (engine not yet present) and legacy cases passing/xfailing as marked

## 3. Handoff

- [x] 3.1 Record in the change README (or handoff note) which scenarios are
      implementation-blocking for `inmemory-engine` (all of them) and which
      legacy divergences exist (IOC, priority-on-price-change)

---

Reconciled at archive time, 2026-08-12. Beads were the execution source of
truth (`lob-kzr.1` .. `lob-kzr.7`, plus `lob-kzr.6` added in bead-structure
review for the shared acceptance fixture); these boxes are checked from their
closure, not the other way round.

2.1 and 2.2 landed as `tests/acceptance/test_order_lifecycle.py` (14 scenarios)
and `tests/acceptance/test_book_queries.py` (10), both on the shared engine
fixture. 2.3 verified: 33 passed / 46 skipped / 13 xfailed, every skip an
`inmemory` parameterization and every xfail strict and bead-referenced.

3.1 is `HANDOFF.md`. It records more legacy divergences than the change
predicted: IOC and priority-on-price-change were expected, and writing the
suites also surfaced `lob-a17`, `lob-7e7`, `lob-ihv`, `lob-0rb`, `lob-crf`
and `lob-xqz`.
