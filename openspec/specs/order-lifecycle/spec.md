## Purpose

The contract for an order's life: what submissions are accepted, how orders
are identified, how cancel and modify behave, how market orders execute, and
how priority is assigned and retained.
## Requirements
### Requirement: Market orders are immediate-or-cancel

A market order SHALL execute immediately against available opposite-side
liquidity in priority order, and any unfilled remainder SHALL be cancelled.
A market order SHALL never rest in the book.

#### Scenario: Remainder is cancelled when liquidity runs out

- **WHEN** a market bid for 8 arrives and total resting ask liquidity is 5
- **THEN** 5 is filled, the order finishes with fulfilled=5 and cancelled
  remainder 3, and no entry for it rests in the book

#### Scenario: Market order on an empty opposite side

- **WHEN** a market order arrives and the opposite side is empty
- **THEN** nothing fills, the order is cancelled in full, and the book is
  unchanged

#### Scenario: Trades price at the maker

- **WHEN** a market order matches resting limit orders
- **THEN** each fill prices at the resting order's limit price

### Requirement: Order identifiers are unique and stable

Each accepted order SHALL receive an identifier unique across every instrument
the engine holds, for that engine's lifetime, including across reloads of
persisted state. Identifiers SHALL NOT be scoped to an instrument: an
identifier issued for an order on one instrument SHALL NOT be issued again for
an order on another, and an identifier SHALL NOT be reissued after its order
is cancelled or fully filled.

Operations addressing an identifier SHALL affect at most one order, and SHALL
reach that order without being told which instrument it belongs to.

The scope is one engine, not the process: identifiers issued by two engines
are unrelated, and nothing may be inferred from comparing them.

#### Scenario: Cancel targets exactly one order

- **WHEN** cancel is called with an identifier
- **THEN** exactly the one order with that identifier is cancelled

#### Scenario: Cancel needs no instrument

- **WHEN** cancel is called, naming no instrument, with the identifier of an
  order resting on one of the several instruments an engine holds
- **THEN** exactly that order is cancelled and no order on any other
  instrument is touched

#### Scenario: Unknown identifier raises

- **WHEN** cancel or modify is called with an identifier no order has
- **THEN** a library exception is raised and the book is unchanged

#### Scenario: Identifiers do not restart per instrument

- **WHEN** orders are submitted alternately on two instruments of one engine
- **THEN** no two of them carry the same identifier, and no identifier issued
  on one instrument is issued again on the other

#### Scenario: Externally supplied duplicate is rejected

- **WHEN** an order is submitted (data-replay path) carrying an identifier the
  engine has already issued
- **THEN** the submission is rejected with a library exception

#### Scenario: A duplicate from another instrument is rejected

- **WHEN** an order is submitted (data-replay path) on one instrument carrying
  an identifier already issued on a different instrument of the same engine
- **THEN** the submission is rejected with a library exception and neither
  instrument's book changes

#### Scenario: A finished order's identifier is not reissued

- **WHEN** an order has been cancelled or fully filled and a later submission
  (data-replay path) carries its identifier
- **THEN** the submission is rejected with a library exception

### Requirement: Invalid submissions raise library exceptions

Submissions with non-positive quantity, unknown order type, unknown side, or
(for limit orders) missing price SHALL raise a library exception. The API
SHALL never terminate the host process.

#### Scenario: Non-positive quantity

- **WHEN** an order with qty <= 0 is submitted
- **THEN** an exception is raised, no state changes, and the process continues

### Requirement: Modify is validated and priority-aware

Modify SHALL reject a side change, clamp a quantity reduction below the
already-fulfilled amount to the fulfilled amount, and re-assign time priority
when the modification is not strictly passive: a price change or a quantity
increase moves the order to the back of its price level's queue; a pure
quantity decrease keeps its place.

#### Scenario: Side mismatch raises

- **WHEN** modify is called with a side different from the order's side
- **THEN** an exception is raised and the order is unchanged

#### Scenario: Quantity reduced below fills clamps

- **WHEN** an order with fulfilled=6 is modified to qty=4
- **THEN** its quantity becomes 6 and it is treated as fully filled

#### Scenario: Price change loses time priority

- **WHEN** orders A then B rest at price P, and A is modified to a different
  price and then modified back to P while B remains
- **THEN** B holds queue priority ahead of A at P

#### Scenario: Quantity decrease keeps time priority

- **WHEN** two orders A then B rest at the same price and A's quantity is
  reduced
- **THEN** A still holds queue priority ahead of B

### Requirement: Price-time priority is deterministic

Orders at a better price SHALL match first; among equal prices, arrival order
SHALL decide, using a total order (arrival sequence number) so that equal
timestamps cannot produce nondeterministic matching.

#### Scenario: Same-timestamp arrivals keep arrival order

- **WHEN** two orders arrive carrying the same timestamp value at the same
  price (data-replay path)
- **THEN** the earlier-submitted order matches first, on every replay

### Requirement: Prices are quantized to the tick

Order prices SHALL be quantized to the nearest multiple of the configured
tick size, for any positive tick size. Quantization SHALL be exact for
decimal ticks and correct (nearest multiple) for non-decimal ticks.

#### Scenario: Non-decimal tick

- **WHEN** the tick size is 0.05 and a limit order at 100.03 is submitted
- **THEN** its working price is 100.05

