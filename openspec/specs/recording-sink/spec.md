## Purpose

The contract between the matching core and persistence: the core emits a
complete, ordered stream of lifecycle events; sinks consume it off the hot
path. The SQLite sink turns that stream into queryable history.
## Requirements
### Requirement: The engine emits a complete lifecycle event stream

The engine SHALL emit one event for every order acceptance, fill (per trade,
carrying both sides' identifiers, price, quantity), cancellation (including
IOC remainder cancels), and modification, in the order the transitions
occurred, each carrying a monotonically increasing sequence number.

#### Scenario: A crossing order's full story is emitted

- **WHEN** a limit bid crosses a resting ask and fills in full
- **THEN** the stream contains the bid's acceptance event followed by exactly
  one fill event referencing both orders, the trade price, and quantity, with
  ascending sequence numbers

#### Scenario: IOC remainder emits a cancel

- **WHEN** a market order fills partially and its remainder is cancelled
- **THEN** the stream contains fill event(s) followed by a cancellation event
  carrying the cancelled remainder quantity

### Requirement: Sinks are optional and off the hot path

The engine SHALL run with zero sinks attached, with no persistence side
effects. Attaching a sink SHALL NOT change any matching outcome: for the same
input order stream, trades, book states, balances, and commissions SHALL be
identical with and without sinks.

#### Scenario: Sink presence does not change outcomes

- **WHEN** the same seeded workload runs once with no sink and once with the
  SQLite sink attached
- **THEN** the resulting trades, final book snapshot, balances, and
  commissions are identical

### Requirement: The SQLite sink persists replayable, queryable history

The SQLite sink SHALL persist every received event such that (a) order
history, trade history, final balances, and commissions are queryable by SQL
after the session, and (b) the persisted stream is sufficient to reconstruct
the book state and reporting values (including last-trade price) at end of
session.

#### Scenario: History is queryable after close

- **WHEN** a session with trades runs with the SQLite sink and closes
- **THEN** SQL queries against the sink database return the session's orders,
  trades, per-trader balances, and commissions

#### Scenario: State reconstructs from the log

- **WHEN** a persisted session's events are replayed into a fresh engine
- **THEN** the reconstructed book snapshot and last-trade price equal the
  original session's end state

### Requirement: A sink observes the stream and does not act on the engine

The engine SHALL deliver each event to a sink synchronously, within the
operation that produced it, so a sink is called while the engine's structures
are part-way through an update.

A sink SHALL NOT call into the engine from `consume` — neither a query nor a
mutation. Every event carries what a consumer needs to maintain its own view,
so a sink that needs engine state derives it from the stream.

The engine SHALL NOT detect, refuse, or compensate for a sink that calls back
into it. Values read by such a sink, and book states produced by such a sink,
are outside the guarantees of `book-queries` and `order-lifecycle`; those
guarantees describe state observed between operations, which is every state a
caller of the engine's own methods can reach.

#### Scenario: A query from inside consume is answered, not refused

- **WHEN** a sink queries the book from `consume` while a fill is being
  recorded
- **THEN** the engine answers the query without raising, and the answer may
  disagree with the same query taken after the operation returns — a best
  price may read `None` for a side whose snapshot and volume queries report a
  resting order

#### Scenario: Queries taken between operations agree

- **WHEN** the same queries are taken after the operation that emitted the
  event has returned
- **THEN** they agree with each other and with the snapshot, as `book-queries`
  requires

#### Scenario: A mutation from inside consume is performed, not refused

- **WHEN** a sink cancels a resting order from `consume` while a match walk is
  in progress
- **THEN** the engine performs the cancellation, the walk in progress is not
  protected from it, and the resulting book state is undefined by this
  specification

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

