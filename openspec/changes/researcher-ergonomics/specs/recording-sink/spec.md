## Purpose

The contract between the matching core and persistence: the core emits a
complete, ordered stream of lifecycle events; sinks consume it off the hot
path. The SQLite sink turns that stream into queryable history.

## ADDED Requirements

### Requirement: A recording carries the experiment's own identifiers

The SQLite sink SHALL accept caller-supplied metadata — a seed, an episode
number, a label, or any other key/value the experiment names — and persist it
inside the recorded database, queryable by SQL alongside the session's events
and readable back through the library. The metadata SHALL be written when the
recording opens, before any event is written, so that a session whose process
dies before its first flush still identifies itself. Reading it SHALL NOT
require the log to be complete. Its absence SHALL mean the caller supplied
none, and SHALL NOT be an error.

The metadata is provenance about the recording and SHALL NOT enter the event
stream, SHALL NOT affect matching, and SHALL NOT be an input to replay.

#### Scenario: A sweep's files name themselves

- **WHEN** many sessions are recorded, each given a distinct seed and episode
  number as metadata
- **THEN** each file returns its own seed and episode, without reference to
  its filename

#### Scenario: A killed session still names itself

- **WHEN** a session is given metadata and its process is killed before the
  sink's first flush, leaving a database with no events in it
- **THEN** the file still returns that metadata

#### Scenario: No metadata is not an error

- **WHEN** a session is recorded without metadata and its metadata is read
- **THEN** the answer is empty and no exception is raised

#### Scenario: Metadata does not reach the stream

- **WHEN** the same seeded workload is recorded twice, once with metadata and
  once without
- **THEN** the two recorded event streams are identical, and so are the
  trades, book states, balances and commissions

### Requirement: Each trade's balance movements are recorded per leg

The sink SHALL expose, for every recorded trade, the individual balance
movements that trade caused — one row per (trader, symbol) movement carrying
the signed amount — in addition to the running balance totals it already
keeps. Aggregating those movements by trader and symbol SHALL reproduce the
recorded balance totals within a floating-point tolerance, and SHALL do so for
every session the sink can record.

Each movement SHALL be denominated in the currency that was in force for its
instrument when that trade executed, not the currency in force at any other
moment. Where an instrument had no declared currency when a trade executed,
that trade SHALL contribute its two instrument movements and no currency
movement, matching what the engine settled.

#### Scenario: The legs sum to the balances

- **WHEN** a session trading two instruments against two currencies, with
  commissions charged, is recorded and closed
- **THEN** aggregating the per-leg movements by trader and symbol reproduces
  every row of the recorded balance totals within a floating-point tolerance

#### Scenario: A currency change mid-session

- **WHEN** an instrument's settlement currency is changed part way through a
  recorded session and further trades execute
- **THEN** each trade's currency movements are recorded in the currency in
  force when that trade executed, and the aggregate still reproduces the
  recorded balance totals

#### Scenario: An unconfigured instrument moves one leg

- **WHEN** a trade executes in an instrument whose currency was never declared
- **THEN** that trade contributes its two instrument movements and no currency
  movement, and the aggregate still reproduces the recorded balance totals

#### Scenario: The movements add no information

- **WHEN** a recorded log is re-folded into a fresh database
- **THEN** the per-leg movements of the new database are identical to the
  original's, because they are derived from recorded trades and not from any
  state the sink kept
