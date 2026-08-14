# Tasks: recordings-name-their-engine

Task 1 is a gate: it is the maintainer's decision on `STREAM_VERSION` and the
ADR that records it. **Nothing in sections 2-6 may land before it**, because the
one thing that is expensive to revisit is the version decision, not the field.
Everything after task 1 is written to be correct whichever way it goes; where it
is not, the task says so.

## 1. MAINTAINER GATE: the `STREAM_VERSION` decision

- [ ] 1.1 Decide whether adding an inert, defaulted field to `SessionStarted`
      bumps `STREAM_VERSION` 1 → 2. `design.md` decision 4 recommends **no**,
      and states the cost either way: a bump makes every recording ever made
      unreadable *and* unreplayable, because `replay.py:114-120` and
      `sqlite.py:1627-1637` both demand exact equality and ADR-0007's window
      covers the schema only. Declining the bump instead requires task 5, which
      buys the one thing the bump would have bought.

      If the decision is to bump, tasks 2-6 stand as written plus: the constant
      goes to 2, `tests/test_replay.py::test_replay_refuses_a_stream_version_it_
      does_not_implement` keeps working unchanged, and a separate decision is
      owed on whether the stream gets a readable window of its own — which is a
      change of its own, not a subtask here.

- [ ] 1.2 Write the ADR the decision needs (`docs/adr/`), whichever way 1.1
      goes. `CLAUDE.md`'s test is met twice over: the decision constrains every
      future additive event field, and the rejected option leaves no other
      trace. It supersedes nothing — it is the stream-side sibling of ADR-0007's
      "honesty, not arithmetic" rule — and should say where the two differ: the
      schema readers have a window and the stream readers do not, and this
      decision avoids needing one rather than building one. Update
      `docs/adr/README.md`.

      Outside this change's ownership; the reasoning is in `design.md` decision
      4 and the before/after-1.0.0 section.

- [ ] 1.3 Confirm the timing. `design.md`'s "Before or after 1.0.0" concludes
      this belongs before the tag under **both** branches of 1.1, and probed
      that `git tag` is currently empty. If v1.0.0 has been tagged since, that
      section's arithmetic changes and 1.1 should be re-taken, not inherited.

## 2. The event

- [ ] 2.1 `src/PyLOB/events.py`: `SessionStarted` gains
      `pylob_version: str | None = None`, after `stream_version`.

      The default is `None` and **must not** be `PyLOB.__version__`
      (`design.md` decision 6): every payload recorded before this change
      decodes through this default, and the live constant would make each of
      them claim the reading release produced it. Say so in the field comment —
      it is the kind of default a later tidy-up "fixes".

- [ ] 2.2 Same file: the `SessionStarted` docstring gains the field, and the
      `STREAM_VERSION` docstring gains the sentence that says why this addition
      did not bump it (or, if 1.1 went the other way, why it did). The rule is
      already stated there — "replay wrongly rather than merely incompletely" —
      and this is the first change to test it, so the worked case belongs beside
      the rule.

- [ ] 2.3 `src/PyLOB/engine.py`: pass the version at the existing emission site
      (`:1152-1160`), inside the `if self.recording:` gate, with a
      function-local `from . import __version__`. Module scope does not work and
      the comment must say why — probed: `PyLOB/__init__.py` imports `.engine`
      at `:41` and defines the literal at `:68`, so a module-scope import is an
      `ImportError` on a partially initialized package.

      Keep it under the gate: ADR-0002's "a sinkless engine constructs no
      event" should go on costing a sinkless engine nothing, including this
      lookup.

## 3. The sink

- [ ] 3.1 `src/PyLOB/sinks/sqlite.py`: `session` gains `pylob_version TEXT`;
      `_SESSION_UPSERT` and `_project`'s `SessionStarted` case carry it. It is
      read out of the event and never from `PyLOB.__version__` — `design.md`
      decision 1, and `tests/test_sink_durability.py:1163-1165` is the test that
      catches a stamp.

- [ ] 3.2 `SCHEMA_VERSION = 5`, with the constant's history note extended.
      `MIN_READABLE_SCHEMA_VERSION` stays 3; its docstring already says the
      window is a decision rather than arithmetic, so record the decision for 4
      there: absence of the column is unambiguous, and cannot be misread as the
      good answer the way an absent `event_loss` table can.

- [ ] 3.3 `_warn_what_an_older_file_lacks` gains the version-4 case. Note that
      `_has_object` cannot answer it — `session` is present in a version-4 file
      and only the column is missing — so this needs a column check, and the
      helper should be the one place that knows it.

      Say whether the warning is worth emitting at all for a version-4 file.
      The version-3 warning exists because `trade_leg` cannot be rebuilt from
      what that file holds; a missing provenance column costs the reader one
      fact and misleads them about nothing. A quieter answer here is defensible
      and should be a deliberate choice, not an omission.

- [ ] 3.4 The question→table map at `sqlite.py:8-35` gains a line: *which engine
      produced this* → `session`. It is the map's own promise to be the reader's
      first stop, and this change exists because that question had no row.

## 4. Reading it back

- [ ] 4.1 Confirm no library reader needs changing. Probed: `check_log` reads
      `event`, `event_loss` and `session_end`; `read_events` reads `event`;
      `read_meta` reads `session_meta`. None reads `session`. Confirm rather
      than inherit — this is the claim that keeps task 3's cost small.

## 5. `decode_event` refuses what it does not understand

- [ ] 5.1 `decode_event` raises `EventLogError` on a field the event class does
      not have, naming the field and the likely cause (a file written by a newer
      PyLOB), instead of letting `TypeError` out of a dataclass constructor.

      Refused, not dropped (`design.md` decision 8): dropping would let a future
      load-bearing field vanish silently and the stream replay wrongly, which is
      what `STREAM_VERSION` exists to prevent.

      This task is what makes 1.1's "no bump" answer safe, and it is worth
      strictly more before v1.0.0 than after — a reader that shipped without it
      never gets it.

## 6. Tests

- [ ] 6.1 `tests/test_emission_coverage.py`: the engine states its own version
      in the opening event — scenario "A recording says which engine produced
      it", engine side.

- [ ] 6.2 `tests/test_sink_durability.py`:
      - the sink projects it, and a re-fold reproduces it (the existing
        `PROJECTIONS` comparison covers this once the column exists; confirm it
        does and cite it rather than adding a second assertion);
      - scenario "A derived recording keeps the original's answer": feed a log
        whose `SessionStarted` names a *different* version through a fresh sink
        and assert the projection names that one. This is the assertion that
        would fail if anyone later reaches for `PyLOB.__version__` here;
      - scenario "An older recording says it does not know": an `as_version_4`
        sibling to `as_version_3` (`:1653-1684`) and a reading test modelled on
        `test_a_version_3_recording_still_reads`, asserting the file genuinely
        lacks the column rather than merely being stamped 4;
      - scenario "The version does not reach the caller's metadata":
        `read_meta` on a recording opened with no `meta` is still `{}`;
      - the `decode_event` refusal from task 5.1;
      - confirm the version-refusal parametrisation at `:1064` still names the
        versions it means to (it is written as `(1, SCHEMA_VERSION + 1)` and
        `:1073-1075` explains why they are named outright — check that reasoning
        still holds at 5, since 4 is now inside the window).

- [ ] 6.3 `tests/test_replay.py`: a stream with no `pylob_version` replays
      identically to one with it — the field is inert to replay, which is the
      evidence decision 4 rests on and should not be left as an assertion in a
      design document.

- [ ] 6.4 `./verify`. Six stages, unchanged; nothing here adds or removes one.
      Confirm `benchmarks/baselines.json` is unaffected (`SessionStarted` is
      constructed once per recording engine and is on no measured inner loop) —
      confirm, do not assume.
