## ADDED Requirements

### Requirement: A recording preserves the queue order of resting orders

The sink SHALL record, for every order still resting when a session ends, a
value that places that order in its price level's queue, such that reading one
instrument's resting orders on one side — best price first, and that value
ascending within a price — yields them in the matching order `book-queries`
requires of a snapshot. Those values SHALL be distinct among the orders
recorded as resting, so the ordering is total and leaves the reader no tie to
break. This is the SQL route to queue order, and it is the only one: neither
the order in which the engine accepted orders nor the order in which it last
touched them reproduces the queue, since a modification can move an order
without touching the first and can touch the second without moving the order.

The value itself is not a contract, and a reader relying on any of the
following is relying on an accident of this implementation:

- **How it is allocated.** It SHALL NOT be assumed contiguous, dense, or to
  begin at any particular number. A recording whose resting orders carry
  values with gaps between them is complete, not damaged.
- **What it means across instruments.** Orders recorded on different
  instruments SHALL NOT be compared by it. Whether the values come from one
  series per engine or one series per instrument is not something a recording
  promises, and both satisfy this requirement.
- **That it is an arrival order.** An order that lost time priority to a
  modification carries a later value than orders accepted after it. A reader
  who wants the order in which the engine accepted orders has the sequence
  number of each acceptance, which the engine's stream already makes
  monotonically increasing.
- **That it means anything for an order that is not resting.** A cancelled
  order, a fully filled one, and a market order that never rested each carry a
  value too, and nothing is promised about it.

#### Scenario: The resting orders read back as the book

- **WHEN** a session that modified, cancelled and partly filled orders across
  two instruments is recorded and closed, and one instrument's resting orders
  on one side are read back best price first and by the recorded ordering
  value within a price
- **THEN** they are that instrument's snapshot of that side, order for order,
  each carrying the quantity it still has available to trade

#### Scenario: An order that lost priority is recorded behind the one that overtook it

- **WHEN** two orders rest at one price, the earlier-accepted of the two is
  repriced away and back while the other stays, and the session is recorded
  and closed
- **THEN** the recording orders the later-accepted order ahead of the repriced
  one, which reading the two by the order in which they were accepted would
  not

#### Scenario: The order survives a reload

- **WHEN** a recorded session is replayed into a fresh engine that is itself
  recording, and both recordings' resting orders are read back for the same
  instrument and side
- **THEN** the two read back in the same order
