# Design: rewrite-docs

## Context

See proposal. Constraint: docs must describe ratified specs and landed code,
not aspirations — this change is last in the dependency order for that
reason.

## Goals / Non-Goals

**Goals:**
- A reader can go from `git clone` to a running simulation in one README
  screen.
- No documented claim without a source: performance numbers from the
  benchmark baselines, semantics from the ratified specs.

**Non-Goals:**
- No tutorial series, no API reference generator (future decisions).
- No new badges/CI claims (no CI exists).

## Decisions

1. **README stays a single markdown page**; depth lives in the wiki.
   *Alternative rejected:* docs site — overhead unjustified for the
   audience.
2. **Wiki restructure: usage, analytics/sink guide, historical archive.**
   Old RBTree pages are marked historical, not deleted — they document the
   2013 design people cite.
3. **Issue hygiene rides along**: draft closes for #5 (resolved) and
   obsolete-notes for #1/#3, posted only on maintainer approval (external
   actions stay human-gated).

## Risks / Trade-offs

- [Wiki lives in a separate repo; drift risk returns] → the wiki's scope is
  narratives and guides; anything contract-like stays in specs, and the
  README links the spec directory as the source of truth.

## Open Questions

- None.
