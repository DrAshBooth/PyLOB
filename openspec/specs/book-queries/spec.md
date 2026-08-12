## Purpose

The read side of the book: best and worst prices, volume available at a
price, last-trade price, and the book snapshot. Under IOC market orders the
book contains only priced limit orders, so every query has a defined answer.

## Requirements

### Requirement: Best and worst prices reflect resting limit orders

Best bid SHALL be the highest resting bid price; best ask the lowest resting
ask price; worst bid/ask the opposite extremes. A price SHALL be returned as
`None` only when that side of the book is empty. Cancelled and fully-filled
orders SHALL not contribute.

#### Scenario: Non-empty side always reports a price

- **WHEN** at least one limit order rests on a side
- **THEN** the best- and worst-price queries for that side return numeric
  prices, never `None`

#### Scenario: Empty side reports None

- **WHEN** every order on a side has been cancelled or fully filled
- **THEN** best- and worst-price queries for that side return `None`

### Requirement: Volume at price answers the marketable question

Volume-at-price for side S at price P SHALL return the total unfulfilled
quantity of resting S-side orders that an opposite-side order priced at P
would be eligible to match (bids priced >= P when S is bid; asks priced <= P
when S is ask), and 0 when nothing qualifies.

#### Scenario: Aggregates across price levels

- **WHEN** bids of 5@99 and 5@98 rest and volume-at-price is asked for the
  bid side at 98
- **THEN** the answer is 10

#### Scenario: Excludes non-marketable levels

- **WHEN** bids of 5@99 and 5@97 rest and volume-at-price is asked for the
  bid side at 98
- **THEN** the answer is 5

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
