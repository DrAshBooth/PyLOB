# Tasks: spec-commissions-balances

Spec-first: deliverable is ratified contracts plus acceptance tests runnable
against the (fixed) legacy engine now and the in-memory engine later.

## 1. Ratification

- [ ] 1.1 Maintainer reviews both spec files (esp. tracking-not-gating and
      the percentage-cap scenario) and confirms or amends
- [ ] 1.2 Freeze; later edits are new deltas

## 2. Acceptance tests

- [ ] 2.1 Write `tests/acceptance/test_commissions.py` covering every
      scenario in `specs/commissions/spec.md`, engine-fixture parameterized;
      runs against the legacy engine once `fix-fulfilled-accounting` lands
- [ ] 2.2 Write `tests/acceptance/test_trader_balances.py` covering every
      scenario in `specs/trader-balances/spec.md`, same pattern
- [ ] 2.3 Run against the fixed legacy engine; all green (this doubles as
      differential validation of the extracted contracts)

## 3. Handoff

- [ ] 3.1 Note for `inmemory-engine`: these tests must pass unchanged against
      the new engine; where commissions/balances compute is its design call
