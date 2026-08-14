# Design: recordings-name-their-engine

## Context

`__version__` is a literal in `src/PyLOB/__init__.py` (`:68`) whose stated
purpose is provenance for recordings, and nothing consumes it. The question is
not "can we write it somewhere" — it is one line either way — but **which of
the three places that could hold it is the one that stays true**, and what the
version constants cost on the way.

Everything below was probed on today's tree at `0aaac65`, not reasoned from
memory. `./verify` is green at that commit.

## Goals / Non-Goals

**Goals.** A recording answers "which release of PyLOB produced these results"
from inside the file, without the caller having remembered anything, and goes
on answering it correctly after the file has been re-recorded. Recordings made
before this change stay readable and answer honestly.

**Non-Goals.** Not a build identity — no git sha, no dirty-tree flag, no
Python version. `PyLOB.bench.provenance` already captures all of that for
benchmark runs (`provenance.py:300-325`), and it can, because a benchmark runs
from a checkout. A sink runs from an installed wheel where no `.git` exists,
which is exactly why the answer here has to be the version literal and not a
commit. Not a migration tool: no reader rewrites an old file.

## Decisions

### 1. The version rides `SessionStarted`, and the sink projects it

`session(seq, timestamp, tick_size, stream_version)` is the sink's row of
engine-provided session facts and is a projection of `SessionStarted`.
`pylob_version` is an engine-provided session fact. It goes there, and it gets
there the way the other three do.

*Rejected — the sink stamps the column itself, no event change (option 2).*
This is the cheap one: no event vocabulary change, no `STREAM_VERSION`
question, one `SCHEMA_VERSION` bump. It is wrong, and not on a technicality.

`session` is in `PROJECTIONS`, and `tests/test_sink_durability.py:1163-1165`
asserts that re-feeding a recorded log through a fresh sink reproduces the
projections exactly. A sink-stamped column breaks that test — correctly, because
what the test is defending is that **the projections hold nothing the log does
not**. A version the sink supplies is precisely such a thing, and the failure it
produces is not a red test but a false file: re-folding a 1.0.0 log through
1.1.0 would produce a database stating that 1.1.0 produced those events.

That is not a corner case. `_warn_what_an_older_file_lacks` tells the reader of
an old file to do exactly this — "re-recording it through a current SQLiteSink
rebuilds the projections at version %d" (`sqlite.py:1588-1597`), and
`tests/test_sink_durability.py:1815` asserts the remedy works. The module would
be recommending the operation that erases the answer.

The same argument disposes of every variant that stamps at write time rather
than reading out of the log: a `recorder` table, a row in `session_meta`, a
`PRAGMA`. They answer "which release wrote this file", which coincides with
"which engine produced these results" for an original recording and diverges
for every re-recording and every replay-into-a-sink. The docstring's claim is
about results.

### 2. Not `session_meta`, reserved key or otherwise

*Rejected — a reserved key such as `pylob.version`, written by the sink in the
opening transaction (option 3).* It is the only option needing **zero** version
bumps: `session_meta` already exists at schema 4, and adding a row is not a
schema change. Three things kill it.

It writes into the caller's table. `recording-sink`'s ratified requirement calls
the contents "caller-supplied metadata" and says "Its absence SHALL mean the
caller supplied none". A reserved namespace makes collisions impossible but does
not make the mixing honest.

It breaks a scenario ratified today. "No metadata is not an error — WHEN a
session is recorded without metadata and its metadata is read THEN the answer is
empty" (`recording-sink`). With a reserved key always written, `read_meta()` on a
fresh sink returns `{'pylob.version': '1.0.0'}` and never `{}`. Either the
scenario changes — editing a requirement ratified this same week, in order to
add provenance — or `read_meta` filters the reserved namespace out, which makes
it a function that reports something other than what the file contains. Both are
worse than a column.

It is a write-time stamp, so decision 1 applies to it in full.

### 3. Not "make the docstring honest and let the caller do it"

*Rejected — delete the claim, document `meta={"pylob": PyLOB.__version__}`
(option 4).* This deserves the serious hearing the brief asked for, because it
costs nothing, is reversible, and "the library should do it for you" is an
assertion rather than a finding. Three things decide against it.

**Provenance that depends on remembering is not provenance.** The sink has made
this exact call before. `close` could have relied on the caller saying the
session finished; instead `session_end` makes the file say it, precisely
because "the process was killed" and "the caller forgot" are indistinguishable
after the fact (`sqlite.py:130-149`). A forgotten `meta` key is the same
failure with the same signature: an unattributed file that reads as complete.

**It is the wrong party.** A seed is knowledge only the caller has, which is why
`session_meta` is right for it. The library version is knowledge only the
*library* has — the caller can state it only by importing it back out of the
library and handing it over. That is a strange division of labour, and it is
the one place in the sink where the engine is asked to be told about itself.

**It does not survive a sweep.** Fifty files whose provenance depends on a
hand-typed key are fifty files where the key may be spelled `pylob`,
`pylob_version` or `version`, and a colleague's sweep uses none of them. A
column has one name.

What option 4 genuinely buys is time: it ships today with no version
arithmetic. That trade is real, and "Before or after 1.0.0" below is where it is
priced, because deferring is not free either.

### 4. `STREAM_VERSION` stays 1

**This is the maintainer's call (task 1). It is argued here, not assumed.**

*What a bump would cost.* `STREAM_VERSION` has been 1 since the beginning and
this would be its first ever bump. Two places key off it and **both are exact
equality with no window at all**:

- `replay.py:114-120` — a stream whose `stream_version` is not this release's
  raises `ReplayError`.
- `sqlite.py:1627-1637` — `check_log` reads the opening `SessionStarted`
  payload and raises `EventLogError`; `read_events` runs `check_log` first, so
  the log cannot even be read back out.

So `STREAM_VERSION` 1 → 2 makes every recording ever made **unreadable and
unreplayable**. ADR-0007's softening does not reach it: that ADR put the *schema*
readers on a window, and nothing analogous exists for the stream. Making the
bump non-destructive would mean inventing `MIN_REPLAYABLE_STREAM_VERSION`,
applying ADR-0007's honesty test to the stream, and threading it through
`replay` and `check_log` — a larger and more consequential decision than the
feature that provoked it, taken in the same change.

*What the rule actually says.* "Bumped when a change to the events below would
make an older persisted stream replay **wrongly** rather than merely
incompletely" (`events.py:170-174`). Probed: `replay()` touches exactly two
fields of `SessionStarted` — `tick_size`, to build the engine, and
`stream_version`, to refuse (`replay.py:114-121`). A stream without
`pylob_version` therefore replays not merely completely but *identically*. By
the constant's own rule, no bump is owed.

The archived `researcher-ergonomics` reached the same reading of the rule for
the same event ("adding a defaulted field makes an older stream replay
incompletely, not wrongly", its `design.md` decision 3) and still declined to
touch `events.py` — because it was carrying **caller** metadata, which had a
perfectly good sink-side home. This change is carrying an engine fact, which
does not.

*What a bump would buy.* Exactly one thing: a clean failure when an old PyLOB
reads a new file. Probed (see `proposal.md`): today that failure is a `TypeError`
out of a dataclass constructor rather than the `EventLogError` this module
raises for every other unreadable file. A bump converts it into the clean
refusal, because `check_log` compares the version before decoding anything.

*Why the bump is the wrong way to buy it.* Decision 8 buys the same thing in
`decode_event`, permanently, for every additive field this library ever adds,
without stranding a single recording. And the bump has a cost beyond the
recordings: it would establish that an inert additive field bumps the stream
version, which destroys the constant's signal. `STREAM_VERSION` is meant to mean
"your old recording will replay wrongly". If it also means "we added a field you
do not care about", nobody seeing a bump can tell whether to worry, and the
next genuinely dangerous bump arrives looking like the last harmless one.

*What not bumping costs.* A `stream_version` of 1 no longer determines whether
the payload carries `pylob_version` — a reader must look at the field rather
than infer from the number. That is the same shape ADR-0007 already accepted
for the schema, where `_has_object` asks rather than infers
(`sqlite.py:1510-1521`), and the ADR's own summary of its policy is "a rule
about honesty rather than about version arithmetic". Extending that reading to
the stream is the substance of the ADR task 1.2 asks for.

### 5. `SCHEMA_VERSION` 4 → 5, and 4 stays in the reader window

The bump is not optional. ADR-0007 rejected shipping additions unstamped
outright: "A file stamped 3 that contains a `session_meta` table is a file whose
stamp is a lie, and the whole apparatus depends on the stamp being true." A file
stamped 4 with a `pylob_version` column is the same lie.

Whether 4 stays readable is the decision ADR-0007 requires whoever bumps to take
and state. Its test: **can a reader answer the new question honestly from an old
file?**

It can, and more cleanly than for either previous case. A version-4 file has no
`pylob_version` column, and the honest answer from it is "this recording
predates version stamping" — true, complete, and *unambiguous*, because there is
no reading of an absent column under which the file appears to name a version.
Contrast versions 1 and 2, which the window excludes: an absent `event_loss`
table reads as "nothing was lost", which is the good answer to a question the
file cannot answer. Absent provenance cannot be misread as good provenance. So
`MIN_READABLE_SCHEMA_VERSION` stays 3 and the window becomes `[3, 5]`.

*What it costs the readers*, which ADR-0007 says is where the cost belongs. Less
than the last bump did: no library reader touches `session` at all — `check_log`
reads `event`, `event_loss` and `session_end`; `read_events` reads `event`;
`read_meta` reads `session_meta`. The cost is one clause in
`_warn_what_an_older_file_lacks` and a human's `SELECT pylob_version FROM
session` erroring on a pre-5 file. Note that `_has_object` tests for a table or
view and cannot answer this: `session` is present in a version-4 file, and only
the column is missing.

**The asymmetry between the two bumps is the whole cost story of this change.**
The schema bump is cheap because ADR-0007 built the mechanism that makes an
additive bump cheap. The stream bump is expensive because nobody has built that
mechanism for the stream. Using the one that exists is not a cost — it is the
use ADR-0007 was written for.

### 6. The decoding default is `None`, and this is load-bearing

`stream_version: int = STREAM_VERSION` defaults to the live constant, and it is
tempting to copy the pattern. It must not be copied. No payload has ever lacked
`stream_version`, so that default is only ever reached by hand-built events;
payloads lacking `pylob_version` are the entire existing population, and
`decode_event` does `EVENT_BY_KIND[kind](**payload)`. A default of
`PyLOB.__version__` would make every recording ever made decode as though the
reading release had produced it — decision 1's lie, relocated into the decoder
and harder to see.

`None` means "this stream does not state a version". The engine always supplies
the field explicitly, so `None` in a stream from a current engine is impossible
and no reader has to distinguish "old file" from "new file that declined".

### 7. Where the literal comes from: a function-local import

Probed, because the obvious spelling does not work:

```
from PyLOB import __version__            # at module scope in engine.py
ImportError: cannot import name '__version__' from partially initialized
module 'PyLOB' (most likely due to a circular import)
```

`PyLOB/__init__.py` imports `.engine` at `:41` and defines `__version__` at
`:68`, so nothing `__init__` imports can see the literal at module scope. Two
ways out:

- **A function-local `from . import __version__` inside `OrderBook.__init__`,
  under the existing `if self.recording:` gate.** Probed working, both for
  `import PyLOB` and for `import PyLOB.engine` first. One `sys.modules` lookup
  per engine construction, on a path that already builds a dataclass and calls a
  sink, and *not executed at all* by a sinkless engine — so ADR-0002's "a
  sinkless engine constructs no event" keeps costing nothing.
- **Move the literal to an import-free `_version.py`.** Rejected. `pyproject.toml`
  already considered and declined this for its own reasons ("would satisfy
  `code` at the cost of a file and an indirection", `:90-92`), the literal is
  load-bearing for the build (`[tool.hatch.version]` reads it out of
  `__init__.py`'s text), and `src/PyLOB/__init__.py` is the maintainer's file
  this week.

### 8. `decode_event` refuses an unknown field

`EVENT_BY_KIND[kind](**payload)` (`sqlite.py:1735-1740`) turns an unexpected
field into a `TypeError`, which is the one unreadable-file failure in this
module that is not an `EventLogError`. It becomes one, naming the field and the
likely cause (a file written by a newer PyLOB).

*Refused, not dropped.* Dropping unknown fields would let a future field that
*is* load-bearing vanish silently and the stream replay wrongly, which is the
exact failure `STREAM_VERSION` exists to prevent. Refusing is the direction this
module already takes everywhere: it "refuses what it does not understand".

The module has already anticipated this situation and stopped one step short of
it. `check_log` reads the stream version "out of the raw JSON rather than a
decoded event, since **decoding a version whose fields have changed is the thing
being guarded against**" (`sqlite.py:1447-1453`), and `read_events` runs
`check_log` before decoding anything. So a *bumped* stream already fails
cleanly. What is unhandled is the unbumped stream carrying a field an older
reader has never heard of — the case decision 4 creates — and this task is
exactly that gap, closed in the place the module's own comment points at.

This is not scope creep. This change adds the first additive event field in the
project's history, so it is the change that creates the hazard, and
`researcher-ergonomics` already documented the hazard as a reason not to touch
`events.py`. Removing that reason is part of the price of touching it.

### 9. No ADR is written here, and one is warranted

`CLAUDE.md`'s test: write an ADR when a decision constrains a change proposal
not yet written, rejects an option that leaves no other trace, or supersedes an
ADR.

Decision 4 meets the first two. It constrains every future additive event field
— the answer "an inert field does not bump the stream version" is a standing
rule, not a fact about this change — and it rejects the bump, which otherwise
leaves no trace anywhere. It does not supersede ADR-0007; it is the stream-side
sibling of ADR-0007's honesty rule, and the ADR should say so and say where the
two differ (the schema has a window, the stream does not, and this decision
avoids needing one rather than building one).

This change does not own `docs/**`. Task 1.2 is the gate, written the way
`researcher-ergonomics` wrote its ADR-0007 gate: the recommendation is stated,
the reasoning is here, and the maintainer takes the decision and writes the
record.

## Before or after 1.0.0

The brief's instinct is that changing the event vocabulary right after tagging
1.0.0 is worse than doing it before. **Tested, and it holds — but the usual
reason for it is not the operative one here.**

The usual reason is optics, and optics do not survive contact with the facts.
`git tag` is empty: **v1.0.0 is not tagged yet**, and commit `0aaac65` states
that `__version__` "was introduced during this modernization and never
shipped". The README's install commands were updated in that same commit to
name `@v1.0.0` — a tag that does not yet exist. The window is open right now and
closes when it is pushed.

The operative reason is the population of readers and recordings, and it is
worth separating the two branches, because the timing argument turns out to hold
under **both** — which is what makes it robust rather than a rationalisation of
the recommendation.

*If the maintainer takes the `STREAM_VERSION` bump.* Before the tag it strands
recordings made by unpinned git installs — a population the maintainer's own
commit message has just declared unsupported ("an experiment run today and
re-run in six months was not the same experiment and nothing in the results said
so"). After the tag it strands every v1.0.0 user's recordings, unreadably and
unreplayably, with no window mechanism to soften it. For a library whose purpose
is that recordings survive to be studied later, that is close to the worst
breakage available, and semver would make it 2.0.0 — or force someone to build
the stream-version window first. The cost difference is not incremental; it is
"free" against "a major version or a new compatibility mechanism".

*If the maintainer declines the bump, as recommended.* Before the tag, the
forward hazard has a population of zero: there is no released PyLOB to read a
new file. After the tag, every v1.0.0 install is a reader that meets a
v1.1.0 file. Decision 8 handles it either way, but decision 8 only helps
readers that *have* it — and a v1.0.0 that shipped without it never will. The
`decode_event` fix is worth strictly more before the tag than after, because
after the tag it can only protect files, never the readers already in the field.

So both branches say the same thing, for the same underlying reason: **v1.0.0 is
the moment a population comes into existence, and every compatibility cost in
this change is proportional to that population, which is currently zero.**

One honest qualification. This is an argument for doing it before the tag, not
for doing it *hastily* before the tag. If the schedule does not allow the
maintainer to weigh decision 4 properly, decision 3's option 4 — document the
one-liner, ship 1.0.0, revisit — is a legitimate fallback, and the fallback in
"Open Questions" below is cheaper still. What is not legitimate is landing the
event field without the `STREAM_VERSION` decision having been taken
deliberately, because that decision is the one that gets expensive to revisit.

## Risks / Trade-offs

- **[The maintainer disagrees with decision 4 and bumps `STREAM_VERSION`]** →
  the change still lands; task 1 is the gate and tasks 2-6 are written to be
  correct either way. The extra cost is a `check_log`/`replay` refusal for every
  existing recording, and a decision about whether to build a stream-version
  window. Nothing in the repo needs migrating; the affected files are personal
  ones outside it.
- **[A `stream_version` of 1 no longer implies a fixed field set]** → the price
  of decision 4, and the same price ADR-0007 already accepted for the schema.
  Mitigated by the field being defaulted, by `decode_event` refusing rather than
  guessing (decision 8), and by the `STREAM_VERSION` docstring being made to say
  this outright rather than leaving it to be inferred.
- **[`session` gains a column, so `SELECT *` widens]** → named columns are
  unaffected. Any query against a version-5 file was written after this change
  by definition, and version-4 files keep the shape they have.
- **[One more field constructed per engine]** → `SessionStarted` is emitted once
  per `OrderBook`, only when recording. Not on the matching path, not on any
  benchmarked workload's inner loop. `benchmarks/baselines.json` should be
  unaffected; task 6.2 confirms rather than assumes it.
- **[A caller could already have a `session_meta` key holding their own version
  string]** → untouched. The column and the table are separate answers to
  separate questions, and scenario 4 pins that they do not interfere.

## Open Questions

- **Should the `session` column be deferred, landing only the event field?**
  That variant costs **zero** version bumps: the payload carries the version,
  `read_events` surfaces it, and a SQL user reaches it with
  `json_extract(payload, '$.pylob_version') FROM event WHERE kind =
  'session_started'`. It is safe to split, unusually — `researcher-ergonomics`
  decision 5 argued against splitting schema landings because a half-landed
  column produces files indistinguishable from complete ones, and that hazard is
  absent here, since a later column is filled from a field the log already
  carries.

  Recommendation: do not split. `session` projects three of `SessionStarted`'s
  four fields, and hiding the fourth behind `json_extract` makes the provenance
  fact the one session fact a researcher cannot query the way they query the
  others — which is most of what "recorded" is supposed to mean here. But it is
  the lever to pull if the maintainer wants the property before the tag and the
  schema bump after it.

- **Should `read_meta` grow a companion that reports the file's engine version?**
  Deliberately not proposed. `read_events` already yields the `SessionStarted`,
  and `read_meta`'s value is that it works on a file whose log is unusable
  (`sqlite.py:1692-1699`) — a property a projection of the log cannot have.
  Adding `read_session()` is a real ergonomic question and a separate change.
