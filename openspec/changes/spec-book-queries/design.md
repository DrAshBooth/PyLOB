# Design: spec-book-queries

## Context

Spec-first change: no code ships here. The rulings resolve the undefined
behaviors catalogued in `docs/architecture-review-2026-08.md` §1.4, §1.6–§1.9
and §2 (tie-break gap), so that `inmemory-engine` implements decided contracts
instead of accidents. ADR-0001 fixes the architectural frame (in-memory
matching; persistence via event log/sink).

## Goals / Non-Goals

**Goals:**
- Every review pathology in scope maps to a normative requirement + scenario.
- Contracts are engine-neutral and testable without reference to SQL or to
  the in-memory data structures.

**Non-Goals:**
- No implementation, no backports to the legacy SQL engine beyond what
  `fix-fulfilled-accounting` already does.
- Commission and balance behavior (own change: `spec-commissions-balances`).
- Multi-instrument interactions beyond per-instrument scoping already present.

## Decisions

1. **IOC for market-order remainders** — maintainer ruling (2026-08-10),
   recorded in the proposal. Alternatives (rest-at-top = current, convert-to-
   limit) rejected: rest-at-top makes best-price undefined and lets a market
   order outrank all limits forever; convert-to-limit adds a pricing rule with
   no research payoff.
2. **Priority on modify: price change or qty increase loses time priority;
   qty decrease keeps it.** Maintainer-ratified 2026-08-10: standard venue
   behavior (matches how the current engine treats qty increases; the current
   engine's keep-priority-on-price-change is retired as an accident, not a
   feature). If research ever needs configurable priority rules, that lands
   as a later change; the spec states the default.
3. **Duplicate external idNum: reject** rather than last-writer-wins. The
   replay path is for reproducing recorded sessions; silent aliasing is how
   review finding §1.4 (mass-cancel) happened.
4. **Tick quantization: nearest multiple of an arbitrary positive tick.**
   Rejected alternative: restrict to powers of ten (current implementation
   assumption) — silently wrong for 0.05-tick instruments, which are common
   research subjects.
5. **Exceptions over `sys.exit`** — a library must not kill its host. The
   exception taxonomy (single class vs hierarchy) is an implementation detail
   left to `inmemory-engine`'s design.

## Risks / Trade-offs

- [IOC breaks anyone modeling resting market orders] → the behavior was
  undefined-by-accident and pathological (review §1.6); ADR-0001's transition
  keeps the legacy engine in tree for anyone needing the old accident.
- [Priority ruling (decision 2) changes modeled microstructure vs the legacy
  engine] → ratified deliberately; the divergence is whitelisted by name in
  `inmemory-engine`'s differential harness.
- [Spec-first means specs can drift from what implementation later finds
  practical] → reconcile at `inmemory-engine`'s archive per CLAUDE.md; any
  requirement change is a delta, not an edit.

## Open Questions

- None. All rulings ratified 2026-08-10.
