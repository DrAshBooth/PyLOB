## Purpose

Fill accounting for the order lifecycle: matching credits fills to the correct
orders and never allows any order — fresh, resting, or repriced — to trade
beyond its unfulfilled remainder.

## Requirements

### Requirement: Fills are credited to the orders that traded

When a trade executes, the fill quantity and fill value SHALL be credited to
exactly the two orders that participated in the trade, and no others,
regardless of any difference between an order's public identifier (`idNum`)
and its internal row identity.

#### Scenario: Identifier sequences diverge

- **WHEN** the public identifier and internal row identity of an order differ
  (e.g., after order rows have been inserted by paths that advance one
  sequence but not the other) and a trade executes against that order
- **THEN** the participating orders' `fulfilled` quantity and fill value
  increase by exactly the traded quantity and value, and no other order's
  accounting changes

### Requirement: An order never trades beyond its unfulfilled remainder

The quantity an order is eligible to trade SHALL be its stated quantity minus
what it has already fulfilled. An order whose fills equal its stated quantity
SHALL trade no further.

#### Scenario: Partially filled order is repriced across the book

- **WHEN** an order of qty 10 with 4 already fulfilled is repriced so that it
  crosses resting liquidity of 20
- **THEN** it trades at most 6, its fulfilled total ends at exactly 10, and
  the resting counterparty retains 14 unfulfilled

#### Scenario: Subsequent order receives honestly accounted liquidity

- **WHEN** after the reprice above, a fresh order arrives sized within the
  counterparty's remaining 14
- **THEN** it is filled in full

#### Scenario: Fresh order path is unaffected

- **WHEN** a new order with no prior fills is processed
- **THEN** its eligible quantity is its full stated quantity

### Requirement: Order lifecycle preserves fill accounting

Add, cancel, modify, crossing, partial fill, and market-order execution SHALL
each leave every order's fulfilled quantity between zero and its stated
quantity, and the sum of fills per trade equal on both sides.

#### Scenario: Lifecycle invariants hold across the reference workload

- **WHEN** a workload exercising limit adds, crossing limit orders, partial
  fills, market orders, cancels, and modifies is run against a fresh book
- **THEN** at every step no order has `fulfilled` below zero or above its
  stated quantity, and each trade's quantity is debited/credited equally to
  its bid and ask sides
