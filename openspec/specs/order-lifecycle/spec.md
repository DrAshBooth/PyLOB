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

### Requirement: Cancel and modify are reachable by identifier and keyword

The library SHALL offer cancel and modify operations that take the order's
identifier as their first argument and every other input by keyword only,
alongside the existing positional operations, which SHALL keep their present
names, signatures and behaviour. The keyword operations SHALL apply the same
validation, make the same state change, and emit the same events as the
existing ones, so that a session driven through either is indistinguishable in
the recorded stream and on replay.

#### Scenario: Cancelling by identifier alone

- **WHEN** the keyword cancel is called with only an order identifier
- **THEN** exactly the one order with that identifier is cancelled, and the
  stream carries the same cancellation event the existing cancel would have
  emitted

#### Scenario: Either spelling records the same stream

- **WHEN** one workload of submissions, cancellations and modifications is run
  twice against fresh books with a recording sink, once through the keyword
  operations and once through the existing positional ones
- **THEN** the two recorded event streams are identical

#### Scenario: A side that is not the order's still raises

- **WHEN** the keyword cancel is given a side the named order does not have
- **THEN** a library exception is raised and the book is unchanged

#### Scenario: An unknown identifier still raises

- **WHEN** the keyword cancel or modify is called with an identifier no order
  has
- **THEN** a library exception is raised and the book is unchanged

### Requirement: The keyword operations name the clock once

Every operation on the keyword surface that accepts a caller-supplied clock
value SHALL name that parameter identically, so that stepping simulated time
is one name across submission, modification and cancellation. The existing
positional operations SHALL keep whatever name they already use.

#### Scenario: One name across the three operations

- **WHEN** an order is submitted, modified and cancelled through the keyword
  surface, each with a caller-supplied clock value
- **THEN** the same keyword name carries that value in all three calls, and
  each emitted event carries the value its own call supplied

### Requirement: A modification names what it changes

The keyword modify SHALL treat an omitted quantity or price as "leave that one
alone", and SHALL raise a library exception when neither is named, rather than
applying and recording a modification that changes nothing.

#### Scenario: An omitted field is left alone

- **WHEN** a resting limit order is modified through the keyword surface with
  a new quantity and no price
- **THEN** its quantity becomes the new one, its price is unchanged, and it
  does not become a market order

#### Scenario: A modification that names nothing raises

- **WHEN** the keyword modify is called with neither a quantity nor a price
- **THEN** a library exception is raised, the order is unchanged, and no event
  is emitted

#### Scenario: Clamping and priority rules are unchanged

- **WHEN** an order with fulfilled=6 is modified through the keyword surface
  to a quantity of 4
- **THEN** its quantity becomes 6 and it is treated as fully filled, exactly
  as the existing modify would have done

