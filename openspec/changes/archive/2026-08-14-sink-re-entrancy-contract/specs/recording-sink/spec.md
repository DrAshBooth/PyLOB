## ADDED Requirements

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
