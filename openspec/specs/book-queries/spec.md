## Purpose

The read side of the book: best and worst prices, volume available at a
price, last-trade price, and the book snapshot. Under IOC market orders the
book contains only priced limit orders, so every query has a defined answer.
## Requirements
### Requirement: Best and worst prices reflect resting limit orders

Best bid for an instrument SHALL be the highest resting bid price on that
instrument; best ask the lowest resting ask price on it; worst bid/ask the
opposite extremes. Orders resting on any other instrument the engine holds
SHALL NOT contribute. A price SHALL be returned as `None` only when that side
of that instrument's book is empty. Cancelled and fully-filled orders SHALL
not contribute.

#### Scenario: Non-empty side always reports a price

- **WHEN** at least one limit order rests on a side
- **THEN** the best- and worst-price queries for that side return numeric
  prices, never `None`

#### Scenario: Empty side reports None

- **WHEN** every order on a side has been cancelled or fully filled
- **THEN** best- and worst-price queries for that side return `None`

#### Scenario: Another instrument's orders do not move these prices

- **WHEN** one engine holds bids of 5@99 on instrument A and 5@150 on
  instrument B, and asks of 5@101 on A and 5@60 on B
- **THEN** A's best bid is 99 and A's best ask is 101, and B's best bid is 150
  and B's best ask is 60
- **AND** an instrument with no resting orders reports `None` on both sides,
  however much rests on the others

### Requirement: Volume at price answers the marketable question

Volume-at-price for an instrument, side S and price P SHALL return the total
unfulfilled quantity of resting S-side orders **on that instrument** that an
opposite-side order priced at P would be eligible to match (bids priced >= P
when S is bid; asks priced <= P when S is ask), and 0 when nothing qualifies.
Orders resting on any other instrument the engine holds SHALL NOT contribute,
whatever their price.

#### Scenario: Aggregates across price levels

- **WHEN** bids of 5@99 and 5@98 rest and volume-at-price is asked for the
  bid side at 98
- **THEN** the answer is 10

#### Scenario: Excludes non-marketable levels

- **WHEN** bids of 5@99 and 5@97 rest and volume-at-price is asked for the
  bid side at 98
- **THEN** the answer is 5

#### Scenario: Another instrument's volume at the same price is excluded

- **WHEN** one engine holds a bid of 5@99 on instrument A and a bid of 7@99 on
  instrument B, and volume-at-price is asked for the bid side at 99
- **THEN** the answer is 5 for A and 7 for B, and 0 for an instrument holding
  nothing

### Requirement: Last-trade price is reporting, not matching state

The book SHALL expose the price of the most recent trade per instrument as a
reporting value. Matching SHALL NOT depend on it (IOC market orders always
price at the maker), and after reloading persisted state the reported value
SHALL equal the last trade in the persisted record.

#### Scenario: Updates on every trade

- **WHEN** a trade executes at 101.0
- **THEN** the last-trade price for that instrument reports 101.0

#### Scenario: Survives reload

- **WHEN** a book's state is persisted after a trade at 101.0 and later
  reloaded
- **THEN** the last-trade price still reports 101.0

### Requirement: Book snapshot is complete and consistent

A book snapshot for an instrument SHALL list every resting order (side,
identifier, unfulfilled quantity, price, priority position) exactly once,
ordered by matching priority, and SHALL agree with the price and volume
queries taken at the same moment.

#### Scenario: Snapshot agrees with queries

- **WHEN** a snapshot is taken and best-bid/best-ask are queried with no
  intervening operations
- **THEN** the snapshot's top-of-book orders carry exactly those prices

### Requirement: Depth is available as an aggregated price ladder

The book SHALL answer, for one instrument and one side, an ordered ladder of
price levels: every distinct price at which orders rest on that side, best
price first, each paired with the total unfulfilled quantity resting at
exactly that price. A caller SHALL be able to bound the answer to the best N
levels. A side with nothing resting SHALL yield an empty ladder rather than an
error, and a bound that is not a positive whole number SHALL raise a library
exception.

#### Scenario: Orders at one price aggregate into one level

- **WHEN** bids of 5@99, 3@99 and 4@98 rest and the bid ladder is requested
- **THEN** the ladder reads (99, 8) then (98, 4), in that order

#### Scenario: Partial fills are excluded from the level's volume

- **WHEN** a resting bid of 10@99 has 4 fulfilled and the bid ladder is
  requested
- **THEN** that level's volume is 6

#### Scenario: The ladder is bounded to the best levels

- **WHEN** bids rest at 99, 98 and 97 and the bid ladder is requested bounded
  to 2 levels
- **THEN** the ladder holds exactly the 99 and 98 levels, in that order

#### Scenario: Empty side yields an empty ladder

- **WHEN** every order on a side has been cancelled or fully filled and that
  side's ladder is requested
- **THEN** the ladder is empty and no exception is raised

#### Scenario: A non-positive bound raises

- **WHEN** a ladder is requested with a bound of 0
- **THEN** a library exception is raised

### Requirement: The ladder agrees with the other read-side queries

A ladder taken with no intervening operations SHALL agree with the
best-price, volume-at-price and snapshot queries taken at the same moment: its
first entry's price SHALL be that side's best price, its last entry's price
that side's worst price, its volumes accumulated from the best level downwards
SHALL equal volume-at-price at each of those prices, and its (price, volume)
pairs SHALL equal the snapshot's unfulfilled quantities aggregated by price.

#### Scenario: The ends of the ladder are the ends of the side

- **WHEN** an unbounded ladder is taken on a non-empty side
- **THEN** its first entry's price equals that side's best price and its last
  entry's price equals that side's worst price

#### Scenario: Cumulative depth equals volume at price

- **WHEN** an unbounded bid ladder is taken and its volumes are accumulated
  from the best level downwards
- **THEN** the running total at each of its prices equals the volume-at-price
  query for the bid side at that price

#### Scenario: The ladder is the snapshot, aggregated

- **WHEN** an unbounded ladder and a snapshot are taken for the same
  instrument and side with no intervening operations
- **THEN** aggregating the snapshot's unfulfilled quantities by price
  reproduces the ladder exactly, levels and order alike

