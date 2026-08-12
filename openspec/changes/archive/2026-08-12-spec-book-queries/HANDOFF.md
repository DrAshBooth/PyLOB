# Handoff: spec-book-queries

Both spec files are frozen. Every scenario in them is now an executable test in
`tests/acceptance/`, parameterized over the engine registry in
`tests/acceptance/conftest.py`.

## For whoever builds the in-memory engine

**Every scenario is implementation-blocking.** There is no partial-credit
subset. `tests/acceptance/test_order_lifecycle.py` (14 scenarios) and
`tests/acceptance/test_book_queries.py` (10) are the contract the new engine is
built against, and they are already written — you are not writing tests, you
are turning skips into passes.

Wiring the engine in is one function body. `ENGINES` in
`tests/acceptance/conftest.py` already registers it:

```python
EngineSpec(id="inmemory", requires="PyLOB.engine", build=build_inmemory)
```

While `PyLOB.engine` is absent every `inmemory` parameterization is **skipped**,
not failed. The moment that module imports, all 46 skips become live tests. Fill
in `build_inmemory` with an adapter carrying `LegacyAdapter`'s surface; no test
file changes.

**The xfail markers are legacy-only and strict.** `@pytest.mark.engine_xfail`
names the engines it applies to, so a divergence marked `"legacy"` does not
excuse the new engine — those tests must pass outright on `inmemory`. Strictness
cuts both ways: if you fix a legacy defect without removing its marker, the run
goes red with XPASS.

## Legacy divergences pinned in these suites

Thirteen xfails, all citing a bead:

| Bead | Divergence | Tests |
| --- | --- | --- |
| `lob-0bl` | A market order's remainder rests instead of being cancelled. Through the query surface it is worse: the null-priced remainder sorts ahead of every priced order, so `best()` answers `None` for a side that holds a priced limit order, while `volume_at()` still counts the remainder | 4 |
| `lob-ihv` | Invalid submissions call `sys.exit` instead of raising | 2 |
| `lob-0rb` | Unknown identifier and wrong-side cancel/modify silently no-op | 3 |
| `lob-a17` | A duplicate externally supplied `idNum` is accepted | 1 |
| `lob-7e7` | The `idNum` counter is not seeded from persisted state, so identifiers collide after a reload | 1 |
| `lob-pn3` | A price change on modify does not surrender time priority | 1 |
| `lob-5rt.2` | Tick quantization is decimal-only; tick 0.05 clips 100.03 to 100.0 | 1 |

The two the change proposal predicted — IOC and priority-on-price-change — are
both there. The other five were found while writing the suites.

## One scenario that passes on legacy but should not be trusted

`test_same_timestamp_arrivals_keep_arrival_order` is **not** marked xfail,
because legacy passes it — incidentally. There is no arrival sequence number:
`order_priority` tie-breaks on `event_dt` alone and falls through to rowid,
and SQLite's sorter happened to preserve insertion order at 60 tied rows when
probed directly. The contract requires a total order; legacy satisfies the
observable behavior by relying on storage-order behavior nothing guarantees.

Bead `lob-xqz`. The new engine should carry an explicit monotonic arrival
sequence so this passes by construction rather than by luck.

## Known gap in the acceptance fixture

`engine_factory` seeds exactly one instrument, so the "for an instrument" half
of the book-snapshot requirement has no test. Filed with the fixture bead. It
matters little today — `openspec/config.yaml` puts multi-instrument explicitly
out of scope — but the legacy engine does leak orders across instruments in
`best`/`worst`/`snapshot` (P4 bead), so the new engine should scope book
queries by instrument by construction rather than inherit the question.
