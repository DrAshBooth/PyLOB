# Proposal: recordings-name-their-engine

> **Approved by the maintainer, 2026-08-14.** Accepted as proposed, and the
> task 1 gate is **decided**: `STREAM_VERSION` stays 1, `decode_event`
> refuses unknown fields, recorded in
> [ADR-0008](../../../docs/adr/0008-additive-event-fields-do-not-bump-the-stream-version.md).
> Task 1.3's timing confirmed — no tags exist yet, so this lands before
> v1.0.0 as the design argues. Converted into beads.

## Why

`src/PyLOB/__init__.py` says `__version__` is something "a recorded session
should note alongside its results" (`__init__.py:20-21`, and again at
`:64-67`: "it is here for researchers recording which version produced a
session"). Nothing records it. `__version__` appears nowhere in
`src/PyLOB/sinks/` or `src/PyLOB/bench/`, and no event carries it. The claim is
unmet by the code that makes it.

The gap is not cosmetic. A recording is the artefact this library exists to
produce, and the engine that produces it was **rewritten this year**: ADR-0001
moved matching in-memory, ADR-0003 deleted the SQL engine and its differential
oracle, and `openspec/changes/archive/` holds twelve changes landed since
2026-08-12. A `.db` from a sweep six months ago
cannot say which of those engines derived its fills, and the file's own
`check_log` will happily call it complete.

The sink already answers every other "what was this run" question inside the
file rather than in a filename — which order became what (`orders`), what a
trader paid (`trader_commission`), which run this is (`session_meta`), whether
the file can be trusted (`check_log`). The one thing it cannot say is *which
engine*.

Two facts shape where the answer goes, and both were probed rather than
assumed:

- **The sink already separates engine-provided session facts from
  caller-provided ones.** `session(seq, timestamp, tick_size, stream_version)`
  is a projection of `SessionStarted`; `session_meta(key, value)` is whatever
  the caller passed to `SQLiteSink(path, meta=...)`. A library version is
  engine-provided. Putting it in `session_meta` mixes the two and can collide
  with a caller's key.
- **`session` is a fold of the log, and that is tested.**
  `tests/test_sink_durability.py` lists `session` in `PROJECTIONS` and asserts
  `dump(refolded_path, PROJECTIONS) == recorded` (`:1163-1165`) — re-feeding a
  recorded log through a fresh sink must reproduce the projections exactly. So
  a column the *sink* stamps rather than reads out of an event would make a
  re-fold of a 1.0.0 log claim it was produced by whatever release performed
  the fold. That is not a hypothetical: `_warn_what_an_older_file_lacks`
  **recommends re-folding** as the remedy for an old file (`sqlite.py:1578-1581`).

So the version has to ride the event, and `SessionStarted` is the event.

## What Changes

- **`SessionStarted` gains `pylob_version: str | None = None`.** The engine
  fills it from `PyLOB.__version__` at construction, inside the existing
  `if self.recording:` gate (`engine.py:1152-1160`), so ADR-0002's "a sinkless
  engine constructs no event" is untouched.

  The default is `None` and never `PyLOB.__version__`: a payload recorded
  before this change must decode as *not stating a version*, not as having been
  produced by whatever release is reading it. Defaulting to the live constant
  would tell, in the decoder, exactly the lie the sink-stamped column was
  rejected for.

- **`session` gains a `pylob_version TEXT` column**, projected from that field
  like the three columns beside it. `SCHEMA_VERSION` 4 → 5.

- **`MIN_READABLE_SCHEMA_VERSION` stays 3**, so the reader window becomes
  `[3, 5]`. ADR-0007's test is applied in `design.md` decision 5 and answered:
  "this recording predates version stamping" is a true and complete answer from
  an old file, and no reader can misread an absent column as a version.
  `_warn_what_an_older_file_lacks` gains the version-4 case.

- **`STREAM_VERSION` stays 1.** This is the change's one contestable decision
  and `design.md` decision 4 argues it in full. In short: the constant's own
  rule is "bumped when a change to the events below would make an older
  persisted stream replay **wrongly** rather than merely incompletely"
  (`events.py:170-174`), and this field is inert to replay — `replay()` reads
  `tick_size` and `stream_version` off `SessionStarted` and nothing else
  (`replay.py:114-121`).

  **This is the item the maintainer is asked to rule on**, and it is task 1.
  Everything after it is written to be correct either way.

- **`decode_event` refuses an unknown field with `EventLogError`** instead of
  raising `TypeError` out of a dataclass constructor. This is the hazard the
  archived `researcher-ergonomics` change named when it rejected putting
  metadata on `SessionStarted` (its `design.md`, decision 3), and this change
  is the first ever additive event field, so it is the change that creates the
  hazard and owes the fix. Probed on today's code:

  ```
  old reader, old payload:  SessionStarted(seq=0, timestamp=0.0, tick_size=0.01,
                                           stream_version=1)
  old reader, NEW payload:  TypeError -> SessionStarted.__init__() got an
                            unexpected keyword argument 'pylob_version'
  ```

  Refused, not dropped: silently discarding a field a future release made
  load-bearing is the failure `STREAM_VERSION` exists to prevent.

- **`recording-sink` gains one requirement**, "A recording names the library
  version that produced it", with four scenarios — that a recording says it,
  that a re-recording keeps the *original's* answer rather than the re-recorder's,
  that an older recording says it does not know without erroring, and that none
  of this disturbs `session_meta`.

**No matching behaviour changes.** The engine derives the same fills, in the
same order, with the same accounting. What changes is that the stream carries
one more inert fact and the sink projects it.

## Capabilities

### New Capabilities

<!-- none -->

### Modified Capabilities

- `recording-sink`: gains the promise that a recording identifies the library
  release that produced it. Today the capability governs what the stream
  contains, that sinks cannot change outcomes, that history is queryable, that
  a sink does not act on the engine, and that caller-supplied metadata is
  persisted — and says nothing about the identity of the engine on the other
  end of the contract.

## Impact

- `openspec/specs/recording-sink/spec.md` — one ADDED requirement
- `src/PyLOB/events.py` — one defaulted field on `SessionStarted`; the
  `STREAM_VERSION` docstring gains the sentence that says why this addition did
  not bump it (only if task 1 lands as recommended)
- `src/PyLOB/engine.py` — one keyword at the existing emission site, and the
  function-local import the package's own import order forces (`design.md`
  decision 7)
- `src/PyLOB/sinks/sqlite.py` — `SCHEMA_VERSION = 5`, the `session` column, the
  `_SESSION_UPSERT` and `_project` pair, `_warn_what_an_older_file_lacks`, the
  question→table map at `:8-35`, and `decode_event`
- `tests/test_sink_durability.py` — an `as_version_4` sibling to `as_version_3`
  and a version-4 reading test; the `decode_event` refusal
- `tests/test_replay.py` — that the field does not reach replay, and that a
  version-less stream replays identically
- `tests/test_emission_coverage.py` — the engine states the version it was
  built from
- **No change to `src/PyLOB/__init__.py`.** The sentence that prompted this
  becomes true rather than needing an edit; sharpening its wording is the
  maintainer's call on the maintainer's file.
- **No ADR written here**, and one is warranted — see `design.md` decision 4
  and task 1.2. `docs/**` is outside this change's ownership.
- Constraints respected: no public API change (no ADR needed for the API); no
  runtime dependency; the package still reads nothing off disk; matching stays
  in-memory; no SQL on the matching path; the wheel still ships
  `src/PyLOB/**/*.py` and nothing else.
