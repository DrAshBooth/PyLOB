# Tasks: spec-commissions-balances

Spec-first: deliverable is ratified contracts plus acceptance tests runnable
against the (fixed) legacy engine now and the in-memory engine later.

## 1. Ratification

- [x] 1.1 Maintainer reviews both spec files (esp. tracking-not-gating and
      the percentage-cap scenario) and confirms or amends
- [x] 1.2 Freeze; later edits are new deltas

## 2. Acceptance tests

- [x] 2.1 Write `tests/acceptance/test_commissions.py` covering every
      scenario in `specs/commissions/spec.md`, engine-fixture parameterized;
      runs against the legacy engine once `fix-fulfilled-accounting` lands
- [x] 2.2 Write `tests/acceptance/test_trader_balances.py` covering every
      scenario in `specs/trader-balances/spec.md`, same pattern
- [x] 2.3 Run against the fixed legacy engine; all green (this doubles as
      differential validation of the extracted contracts)

## 3. Handoff

- [x] 3.1 Note for `inmemory-engine`: these tests must pass unchanged against
      the new engine; where commissions/balances compute is its design call

---

Reconciled at archive time, 2026-08-12. Beads were the execution source of
truth (`lob-02l.1` .. `lob-02l.7`); these boxes are checked from their
closure, not the other way round.

1.1, the maintainer gate, was discharged with two amendments to the
commissions spec (commit `c2045cf`, before the 1.2 freeze): the percentage cap
binds ahead of the floor, and commission carries no currency quantization so
acceptance tests compare within tolerance. Both were then re-measured against
the running engine by the 2.1 suite and hold.

2.1 and 2.2 landed as `tests/acceptance/test_commissions.py` (11 tests) and
`tests/acceptance/test_trader_balances.py` (9). 2.3: 20/20 green against the
fixed legacy engine with zero xfails needed — the extracted contracts match
the engine exactly.

3.1 is `HANDOFF.md`.
