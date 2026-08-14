## ADDED Requirements

### Requirement: A recording names the library version that produced it

The engine SHALL state, in the event that opens every stream, the version of
this library that emitted it. The SQLite sink SHALL persist that statement among
the engine-provided session facts it projects, queryable by SQL alongside the
session's events and readable back through the library, without the caller
having supplied anything.

The statement SHALL travel in the event stream rather than being applied by
whatever writes the file. A recording derived from a recorded log — a log
re-folded into a fresh database, or a session replayed into an engine that
records through its own sink — SHALL therefore name the version that produced
the events, and not the version that performed the derivation.

A recording that states no version SHALL read back as stating none, and that
SHALL NOT be an error: it means the recording predates version stamping, which
is a true and complete answer to the question asked.

The version identifies the release and nothing more. It SHALL NOT affect
matching, SHALL NOT be an input to replay, and SHALL NOT be the version that
governs whether a recorded stream can be replayed at all — a recording that
names a release is not thereby promised to be replayable by it.

The version is provided by the engine and SHALL be distinct from the
caller-supplied metadata of "A recording carries the experiment's own
identifiers". Recording it SHALL NOT place anything among that metadata.

#### Scenario: A recording says which engine produced it

- **WHEN** a session is recorded and closed
- **THEN** the file names the version of the library that ran it, without
  reference to its filename and without the caller having supplied any metadata

#### Scenario: A derived recording keeps the original's answer

- **WHEN** a recorded log that names one version is re-folded into a fresh
  database by a library that names another
- **THEN** the new recording names the version the log states, and not the
  version that performed the re-recording

#### Scenario: An older recording says it does not know

- **WHEN** a recording made before versions were stamped is read
- **THEN** it reports no producing version, no exception is raised, and its
  events read back unchanged

#### Scenario: The version does not reach the caller's metadata

- **WHEN** a session is recorded without caller-supplied metadata
- **THEN** reading that metadata is empty, and the version that produced the
  recording is still available
