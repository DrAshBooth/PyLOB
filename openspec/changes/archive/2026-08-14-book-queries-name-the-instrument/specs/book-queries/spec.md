## MODIFIED Requirements

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
