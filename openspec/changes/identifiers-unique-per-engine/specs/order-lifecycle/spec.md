## MODIFIED Requirements

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
