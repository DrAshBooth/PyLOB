## Purpose

The contract for an order's life: what submissions are accepted, how orders
are identified, how cancel and modify behave, how market orders execute, and
how priority is assigned and retained.

## ADDED Requirements

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
