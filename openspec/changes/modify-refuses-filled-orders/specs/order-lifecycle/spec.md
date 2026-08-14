## MODIFIED Requirements

### Requirement: Modify is validated and priority-aware

Modify SHALL reject a side change, reject an order that is already fully
filled, clamp a quantity reduction below the already-fulfilled amount to the
fulfilled amount, and re-assign time priority when the modification is not
strictly passive: a price change or a quantity increase moves the order to the
back of its price level's queue; a pure quantity decrease keeps its place.

A fully filled order is finished, whether it reached that state by trading or
by the clamp above. Modify SHALL raise a library exception naming that as the
reason and leave the order and the book unchanged; raising the stated quantity
SHALL NOT return it to the book. Quantity wanted after an order finishes is
new quantity, and is submitted as a new order.

#### Scenario: Side mismatch raises

- **WHEN** modify is called with a side different from the order's side
- **THEN** an exception is raised and the order is unchanged

#### Scenario: Quantity reduced below fills clamps

- **WHEN** an order with fulfilled=6 is modified to qty=4
- **THEN** its quantity becomes 6 and it is treated as fully filled

#### Scenario: Fully filled order cannot be modified

- **WHEN** an order with qty=10 and fulfilled=10 is modified to qty=25
- **THEN** an exception is raised, the order keeps qty=10 with nothing
  available to trade, and no entry for it rests in the book

#### Scenario: An order finished by the clamp stays finished

- **WHEN** an order with fulfilled=6 has been clamped to qty=6 by an earlier
  modify, and is then modified to a larger quantity
- **THEN** an exception is raised and the order keeps qty=6, out of the book

#### Scenario: Price change loses time priority

- **WHEN** orders A then B rest at price P, and A is modified to a different
  price and then modified back to P while B remains
- **THEN** B holds queue priority ahead of A at P

#### Scenario: Quantity decrease keeps time priority

- **WHEN** two orders A then B rest at the same price and A's quantity is
  reduced
- **THEN** A still holds queue priority ahead of B
