# Tasks: modernize-packaging

## 1. Project scaffolding

- [x] 1.1 Update uv if needed; `uv init`-equivalent `pyproject.toml` with
      hatchling backend, real metadata, Python >= 3.11, README.md as long
      description, install-from-GitHub instructions in README (short note;
      full docs rewrite is `rewrite-docs`)
- [x] 1.2 Dev-dependency group: ruff (same version `./verify` pins today),
      pytest; `uv lock`; commit `uv.lock`; gitignore uv cache artifacts
- [x] 1.3 Package data rules so `src/PyLOB/*.sql` ship; editable install via
      `uv sync`; delete `tests/conftest.py` sys.path shim
- [x] 1.4 Port ruff config into `[tool.ruff]`; port/retire setup.cfg pytest
      section (no `--cov`); delete `setup.py` and `setup.cfg`

## 2. Verify migration

- [x] 2.1 `./verify` stages switch to `uv run ruff format --check src`,
      `uv run ruff check src`, `uv run pytest -q tests` (commands only; the
      stage list is unchanged — no amendment-rule approval needed, but say so
      in the handoff)
- [x] 2.2 Full `./verify` green from a clean checkout after `uv sync`; record
      wall-clock vs the 60s budget

## 3. Dead code removal (separate commit)

- [x] 3.1 Delete `src/PyLOB/best_quotes.sql` (unused query file, name-collides
      with the view)
- [~] 3.2 Drop `event`/`event_arg` tables and the `active` column (plus its
      view/`order_detail` references) from `create_lob.sql`; regenerate
      `src/lob.db` schema-only
- [x] 3.3 `./verify` green; any failure reverts the removal and reports (the
      "dead" verdict was wrong), no patching around

---

Reconciled at archive time, 2026-08-12. Beads were the execution source of
truth (`lob-476.1` .. `lob-476.10`); these boxes are checked from their
closure, not the other way round.

**3.2 is `[~]`, not done, and this change's verdict on `active` was wrong.**

`event` and `event_arg` were dropped and `src/lob.db` regenerated. The
`active` column was not. This change's `proposal.md` and `design.md` — and
`docs/architecture-review-2026-08.md:118` — describe it as dead. Half of that
holds: nothing writes it, so it is always the default 1. But four places read
it, two of them test queries selecting it straight out of `trade_order`,
where no view edit can shield them.

The removal was attempted exactly as `design.md` prescribes and produced 45
failures on `no such column: active`. The `smoke` stage still passed, so the
engine genuinely does not need the column; the breakage was entirely the test
suite's own introspection SQL. Task 3.3 forbids editing tests to force a
dead-code removal through, so it was reverted and split into its own bead for
a scope decision, with a recommendation to complete the removal in a change of
its own.

`proposal.md` and `design.md` are deliberately left as written. They are the
historical record of what was proposed; this note is the record of what was
true.

**2.1 note (required by the task):** the `./verify` stage list was unchanged —
`format, lint, test, smoke` before and after, same names and order. Only three
stage commands moved to `uv run`, so the amendment rule did not apply and no
maintainer approval was sought.
