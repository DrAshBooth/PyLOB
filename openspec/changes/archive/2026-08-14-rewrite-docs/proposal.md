# Proposal: rewrite-docs

## Why

The README describes an implementation deleted in 2023 (pure-Python RBTrees,
"no requirements other than a standard python3 install") and the wiki
documents the same dead code — issue #5 has asked for real documentation
since 2018. Once `inmemory-engine` lands, the true story (in-memory core,
optional SQLite sink, measured performance) exists and is worth telling.

## What Changes

- README rewritten: what PyLOB is (simulation-for-research LOB), the
  ADR-0001 architecture in two paragraphs, install-from-GitHub via uv/pip,
  quickstart mirroring the new `example.py`, IOC market-order semantics
  called out, measured orders/sec with a pointer to the benchmark harness,
  contribution notes (OpenSpec planning, `./verify`, ADR index).
- Wiki updated (maintainer ruling: in scope): usage walkthrough against the
  new engine, sink/analytics guide (SQL queries over recorded sessions),
  archive page marking the old RBTree docs as historical.
- `example.py`'s stale comments (compatibility-mode bug notes, "my next
  version will use a db") cleaned in the same pass — code comments are docs.
- Closes issue #5; comments on issues #1/#3 (obsolete against the rewritten
  code) drafted for maintainer approval — external actions are the
  maintainer's to send.

## Capabilities

<!-- skip_specs: true — documentation only; no system behavior changes. -->

## Impact

- README.md, wiki (separate git repo on GitHub), example.py comments.
- Depends on `inmemory-engine` (documents its architecture) and
  `benchmark-harness` (quotes its measured numbers).
- Wiki edits happen in the wiki's own repo; tasks include cloning it —
  nothing in this repo's tree carries wiki content.
