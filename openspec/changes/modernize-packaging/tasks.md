# Tasks: modernize-packaging

## 1. Project scaffolding

- [ ] 1.1 Update uv if needed; `uv init`-equivalent `pyproject.toml` with
      hatchling backend, real metadata, Python >= 3.11, README.md as long
      description, install-from-GitHub instructions in README (short note;
      full docs rewrite is `rewrite-docs`)
- [ ] 1.2 Dev-dependency group: ruff (same version `./verify` pins today),
      pytest; `uv lock`; commit `uv.lock`; gitignore uv cache artifacts
- [ ] 1.3 Package data rules so `src/PyLOB/*.sql` ship; editable install via
      `uv sync`; delete `tests/conftest.py` sys.path shim
- [ ] 1.4 Port ruff config into `[tool.ruff]`; port/retire setup.cfg pytest
      section (no `--cov`); delete `setup.py` and `setup.cfg`

## 2. Verify migration

- [ ] 2.1 `./verify` stages switch to `uv run ruff format --check src`,
      `uv run ruff check src`, `uv run pytest -q tests` (commands only; the
      stage list is unchanged — no amendment-rule approval needed, but say so
      in the handoff)
- [ ] 2.2 Full `./verify` green from a clean checkout after `uv sync`; record
      wall-clock vs the 60s budget

## 3. Dead code removal (separate commit)

- [ ] 3.1 Delete `src/PyLOB/best_quotes.sql` (unused query file, name-collides
      with the view)
- [ ] 3.2 Drop `event`/`event_arg` tables and the `active` column (plus its
      view/`order_detail` references) from `create_lob.sql`; regenerate
      `src/lob.db` schema-only
- [ ] 3.3 `./verify` green; any failure reverts the removal and reports (the
      "dead" verdict was wrong), no patching around
