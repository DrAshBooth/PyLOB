# ADR-0008: An additive, inert event field does not bump `STREAM_VERSION`

Status: Accepted
Date: 2026-08-14

## Context

`recordings-name-their-engine` adds `pylob_version` to `SessionStarted`, so a
recording says which engine produced it. The field is defaulted and no replay
path reads it. The question it forces is general and had never been asked:
**does adding a field to an event bump `STREAM_VERSION`?**

It has been 1 since the beginning. Two places key off it, and both are exact
equality with **no window at all**:

- `replay.py` — a stream whose `stream_version` is not this release's raises
  `ReplayError`.
- `sqlite.py` — `check_log` reads the opening `SessionStarted` payload and
  raises `EventLogError`, and `read_events` runs `check_log` first, so the log
  cannot be read back at all.

So a bump would make every recording ever made **unreadable and unreplayable**.
ADR-0007 does not reach this: it put the *schema* readers on a window, and
nothing analogous exists for the stream.

The constant's own rule is narrower than "the events changed". It is bumped
when a change "would make an older persisted stream replay **wrongly** rather
than merely incompletely". `replay()` touches exactly two fields of
`SessionStarted`: `tick_size`, to build the engine, and `stream_version`, to
refuse. A stream without `pylob_version` therefore replays not merely
completely but *identically*.

The one thing a bump would buy is a clean failure when an old PyLOB meets a
new file. Today that failure is a `TypeError` out of a dataclass constructor,
rather than the `EventLogError` this module raises for every other unreadable
file — the same shape of defect as a bare `KeyError` escaping a public API.

## Decision

**An additive field that no replay path reads does not bump `STREAM_VERSION`.**
The test is the constant's own: would an older stream replay *wrongly*? If it
replays identically, the version does not move.

**`decode_event` refuses a field it does not understand**, raising
`EventLogError` naming the field and the likely cause. Refused, not dropped:
dropping would let a future load-bearing field vanish silently and the stream
replay wrongly, which is the exact failure `STREAM_VERSION` exists to prevent.

That pairing is the point. The refusal buys what the bump would have bought —
a clean, named failure when an old reader meets a new file — permanently, for
every additive field this library ever adds, without stranding a single
recording.

**Where this differs from ADR-0007, deliberately.** That ADR softened the
schema readers by giving them a window, because a version bump there was
otherwise a hard cut. This one avoids *needing* a window rather than building
one. The stream readers stay strict, and stay strict cheaply, because the
version stops moving for changes that do not warrant it. Both ADRs answer the
same underlying question — what does an old file mean to a new reader — and
they answer it differently because the two versions guard different things: the
schema guards what a *query* can honestly answer, the stream guards whether a
*replay* is faithful.

## Alternatives considered

- **Bump to 2.** Rejected. It strands every existing recording for reading and
  replay both, to buy one clean error message that decision-by-`decode_event`
  buys for nothing. Its cost is also front-loaded onto exactly the population
  that has the least warning: whoever recorded a session with the release
  before it.

- **Bump to 2 and build `MIN_REPLAYABLE_STREAM_VERSION`.** Rejected, and this
  is the one worth stating, because it is the "do it properly" option. It means
  applying ADR-0007's honesty test to the stream and threading a window through
  `replay` and `check_log` — a larger and more consequential decision than the
  feature that provoked it, taken inside that feature's change. If the stream
  ever does need a window, it should be decided on its own evidence and not as
  a subclause. Nothing here forecloses it.

- **Drop unknown fields silently in `decode_event`.** Rejected. It is the
  lenient reading and it is exactly wrong for this library: a future field that
  *is* load-bearing would vanish without a word and the replay would be
  faithful-looking and wrong. Refusing is the only answer that stays correct
  when the next field is not inert.

- **Do not record the version at all**, and tell callers to pass
  `meta={"pylob": PyLOB.__version__}`. Rejected in the change itself rather
  than here, on the grounds the sink already settled for `session_end`: a
  forgotten metadata key and a killed run are indistinguishable afterwards,
  which is why the file says it rather than the caller.

## Consequences

- **Every recording ever made stays readable and replayable.** That is what
  this buys and it is the whole of it.

- **`STREAM_VERSION` now has a worked example**, which it lacked. The rule
  "wrongly rather than incompletely" was stated and never exercised; the first
  change to test it is recorded beside it.

- **`decode_event` becomes the forward-compatibility boundary.** A file from a
  newer PyLOB fails there, by name, rather than deep in a constructor. Every
  additive field from now on inherits that protection — but only for readers
  that have it, which is why this landed before v1.0.0 rather than after.

- **A future field that a replay path *does* read must bump the version**, and
  will then face the stranding problem this ADR declined to solve. The bill is
  deferred, not cancelled. What is gained is that it comes due only when
  something genuinely changes how a stream replays, instead of every time an
  event grows a field.

- **The two version constants now behave differently on purpose**, and a reader
  meeting both needs this ADR and ADR-0007 to see why. Both cite each other.
