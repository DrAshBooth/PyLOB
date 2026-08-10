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
- [ ] 1.2 Freeze both spec files; any later edit is a new delta, not an
      in-place change

## 2. Acceptance tests (target-behavior, marked for the new engine)

- [ ] 2.1 Write `tests/acceptance/test_order_lifecycle.py` covering every
      scenario in `specs/order-lifecycle/spec.md`, parameterized over an
      engine fixture (legacy engine cases marked xfail where behavior
      intentionally diverges, e.g. IOC)
- [ ] 2.2 Write `tests/acceptance/test_book_queries.py` covering every
      scenario in `specs/book-queries/spec.md`, same fixture pattern
- [ ] 2.3 Verify the suite runs green with the new-engine cases skipped
      (engine not yet present) and legacy cases passing/xfailing as marked

## 3. Handoff

- [ ] 3.1 Record in the change README (or handoff note) which scenarios are
      implementation-blocking for `inmemory-engine` (all of them) and which
      legacy divergences exist (IOC, priority-on-price-change)
