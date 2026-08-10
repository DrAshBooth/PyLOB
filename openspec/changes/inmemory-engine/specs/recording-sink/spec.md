## Purpose

The contract between the matching core and persistence: the core emits a
complete, ordered stream of lifecycle events; sinks consume it off the hot
path. The SQLite sink turns that stream into queryable history.

## ADDED Requirements

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
