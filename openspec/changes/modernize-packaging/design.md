# Design: modernize-packaging

## Context

See proposal. Constraints: uv is the chosen manager (Phase-1 interview);
Python >= 3.11 floor; no PyPI; `./verify`'s stage list may not change without
maintainer approval — commands inside stages may.

## Goals / Non-Goals

**Goals:**
- A locked, reproducible dev environment; one `uv sync` from clean checkout
  to green `./verify`.
- Dead code gone before `inmemory-engine` starts (smaller surface to port).

**Non-Goals:**
- No engine behavior change; no test additions beyond keeping existing green.
- No CI setup (separate decision when wanted).

## Decisions

1. **Build backend: hatchling.** Boring default for a pure-Python src-layout
   package under uv. *Alternative rejected:* keep setuptools + setup.cfg —
   carries the PyScaffold residue this change exists to remove.
2. **SQL files ship as package data** (`src/PyLOB/*.sql` included via the
   build backend's include rules) — the engine's file-loading behavior is
   unchanged until ADR-0001's rewrite retires it.
3. **ruff config moves to `pyproject.toml`** (`[tool.ruff]`), replacing
   setup.cfg's flake8 section; same rule set `./verify` already enforces, so
   no new lint findings by construction.
4. **`./verify` calls `uv run ruff` / `uv run pytest`** with versions from
   `uv.lock`. Tool pins live in one place (the lock), not in the script.
5. **Dead-code removal is mechanical and separately committed** within the
   change: schema slimming (event tables, `active` column) regenerates
   `lob.db`, deletes `best_quotes.sql`, and adjusts nothing else — any test
   failure here means the review's "dead" verdict was wrong and the removal
   reverts, not gets patched around.

## Risks / Trade-offs

- [uv 0.6.6 on the maintainer's machine is a year old] → `uv self update`
  or Homebrew upgrade as task 0; lockfile format compatibility is checked
  before anything else.
- [Removing `active` changes `best_quotes` view text] → the view's
  `active=1` predicate drops with the column; behavior identical since the
  column is constant 1.
- [`order_detail`'s commission CASE references `active`] → the branch is
  dead (active always 1); removal keeps the live arm only.

## Open Questions

- None.
