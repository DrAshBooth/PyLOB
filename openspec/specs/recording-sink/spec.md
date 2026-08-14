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

