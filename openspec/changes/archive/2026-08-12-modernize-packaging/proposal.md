# Proposal: modernize-packaging

## Why

The repo carries 2013-era packaging (PyScaffold `setup.cfg`/`setup.py` with
placeholder metadata and a `long_description` pointing at a file that does
not exist), no dependency management, ad-hoc tool pinning inside `./verify`
(`uvx ruff@0.9.10`), and dead code the architecture review catalogued. The
in-memory engine should be built in a modern project, not migrated into one
afterwards.

## What Changes

- `setup.cfg`/`setup.py` replaced by `pyproject.toml`; uv manages the
  project (Python >= 3.11 floor, `uv.lock`, dev-dependency group with ruff
  and pytest pinned there).
- Real metadata: description, authors, license, URLs pointing at this repo;
  README.md as the long description. Install-from-GitHub documented (no PyPI
  per standing constraints).
- `./verify` stages migrate from `uvx <tool>@<pin>` to `uv run` against the
  project's locked dev-deps (stage *commands* change; the stage *list* does
  not — amendment rule untouched). Stale pytest `--cov` addopts from
  setup.cfg do not carry over, retiring the `-o addopts=` workaround.
- Bundled dead-code removal (per maintainer, 2026-08-10): the unused
  `best_quotes.sql` query file, the unused `event`/`event_arg` tables, and
  the never-written `active` column with its dead `order_detail` CASE branch.
- Tests keep passing throughout; `tests/conftest.py`'s `sys.path` shim is
  replaced by an editable install via uv.

Not breaking for library users: import surface (`from PyLOB import
OrderBook`) is unchanged.

## Capabilities

<!-- skip_specs: true — tooling and packaging only; no observable engine
     behavior changes. Dead-schema removal deletes tables no code path
     reads or writes (review §3). -->

## Impact

- New: `pyproject.toml`, `uv.lock`. Removed: `setup.py`, `setup.cfg` (its
  flake8/pytest config re-homed or retired), `src/PyLOB/best_quotes.sql`.
- Modified: `create_lob.sql` (drop event tables, `active` column,
  `order_detail` branch), `./verify` (commands only), `tests/conftest.py`,
  `src/lob.db` regenerated to match the slimmed schema.
- `.gitignore`: uv artifacts.
- Depends on `fix-fulfilled-accounting` only in that both touch
  `create_lob.sql`/`lob.db`; land #1 first to keep diffs clean.
