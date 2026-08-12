# Design: spec-commissions-balances

## Context

Spec-first: contracts extracted from the current engine's behavior (probed
2026-08-10; scenario numbers are measured, not derived), minus the corruption
paths issue #8 introduces. ADR-0001 keeps these behaviors but moves their
implementation into the in-memory core.

## Goals / Non-Goals

**Goals:**
- Engine-neutral contracts for commission, balances, self-matching, with
  every scenario's numbers verified against the running engine.

**Non-Goals:**
- No margin/gating semantics (explicitly specified as absent).
- No multi-currency conversion; balances are per (trader, instrument-or-
  currency) buckets exactly as today.
- Where these compute (core vs sink) is `inmemory-engine`'s design decision;
  this spec only requires the observable results.

## Decisions

1. **Spec the formula as cumulative-recompute, not per-fill accrual.** That
   is what the trigger does (recompute from totals, debit the delta), and it
   is the economically correct reading of a min/floor/cap schedule. Verified:
   two fills of 3+2 @ 100 charge 2.5 total, not 5.0.
2. **Spec tracking-not-gating as a requirement, not an omission.** The most
   likely future bug is a well-meaning "add funds check" that breaks
   research workloads (short-selling strategies). Stating it makes removal a
   spec change.
3. **Self-matching skip leaves the resting order untouched and continues
   matching past it** — matches the current `matches.sql` predicate.

## Risks / Trade-offs

- [Extracted-from-implementation specs can canonize accidents] → each
  requirement was checked against the "spirit of IB" intent from PR #7's
  commit message, not just the trigger's arithmetic; the percentage-cap
  scenario uses parameters where cap and per-unit diverge to pin intent.
- [Commission recompute on qty-modify interacts with clamping (lifecycle
  spec)] → covered by the cumulative-recompute requirement: commission is a
  function of (Q, V), whatever path led there.

## Open Questions

- None.
