# Proposal: spec-commissions-balances

## Why

PR #7 added commissions, per-trader balances, and a self-matching gate — none
of it specified anywhere. These behaviors survive ADR-0001 (they are part of
what makes the simulator useful for strategy research: online PnL), so the
in-memory engine must reimplement them; without a contract it would have to
reverse-engineer triggers. This change writes the contract, with scenario
numbers verified against the current engine (2026-08-10 probes).

## What Changes

- Commission behavior becomes a spec: `min(max_pct% of fill value,
  max(min_commission, per_unit x filled qty))`, recomputed on cumulative
  fills, charged in the instrument's currency.
- Balance movements become a spec: buyer receives quantity in the instrument
  and pays quantity x price in the instrument's currency; seller the
  converse; commissions debit the currency balance.
- Balances are explicitly *tracking, not gating*: no margin or
  sufficient-funds enforcement (verified: a seller's instrument balance goes
  negative freely). Stated as a requirement so nobody "fixes" it silently.
- The self-matching gate becomes a spec: a trader's order does not match
  their own resting orders unless that trader's `allow_self_matching` is set.

No code changes; implementation is `inmemory-engine`'s obligation (where
these compute in-core so PnL is available online, with the sink persisting —
see that change's design).

## Capabilities

### New Capabilities

- `commissions`: the commission formula, its recomputation on fills and
  cancels, and its settlement currency.
- `trader-balances`: balance movements per trade, commission debits,
  tracking-not-gating semantics.

### Modified Capabilities

<!-- none -->

## Impact

- No code here. `inmemory-engine` implements; its acceptance tests reuse
  these scenarios.
- The legacy engine already passes these contracts *except* where issue #8's
  misattributed fills corrupt them — `fix-fulfilled-accounting` restores it
  as a valid oracle for this spec too.
