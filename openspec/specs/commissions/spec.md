## Purpose

Commission charged to each trader per order, in the spirit of an
interactive-brokers-style schedule: a per-unit rate with a floor, capped by a
percentage of fill value.

## Requirements

### Requirement: Commission follows the capped per-unit-with-floor formula

For an order with cumulative filled quantity Q > 0 and cumulative fill value
V (sum of qty x price over its fills), the commission SHALL equal
`min(max_pct x V / 100, max(min_commission, per_unit x Q))`, using the
owning trader's configured `commission_max_percnt`, `commission_min`, and
`commission_per_unit`. An order with no fills SHALL carry zero commission.

The percentage cap SHALL bind ahead of the floor: where
`max_pct x V / 100 < min_commission`, the commission is the cap, not the
floor. `min_commission` is a floor on the per-unit charge only, not on the
order's commission.

Commission SHALL be computed in floating-point with no rounding or currency
quantization step; the contract is the exact value of the formula, and
acceptance tests SHALL compare within a floating-point tolerance rather than
for bit equality. Any rounding to currency precision is a future,
explicitly-specified feature.

#### Scenario: Floor dominates a small fill

- **WHEN** a trader with min=2.5, max_pct=1, per_unit=0.01 fills 5 @ 101
  (V=505)
- **THEN** the order's commission is 2.5

#### Scenario: Per-unit beats the floor, cap does not bind

- **WHEN** the same trader fills 500 @ 101 (V=50500)
- **THEN** the order's commission is 5.0 (per_unit x 500, below the 505 cap)

#### Scenario: Percentage cap binds

- **WHEN** a trader with min=2.5, max_pct=1, per_unit=1.0 fills 10 @ 10
  (V=100, per-unit charge would be 10)
- **THEN** the order's commission is 1.0 (1% of 100)

### Requirement: Commission is recomputed on cumulative fills

Commission SHALL be a function of the order's cumulative fills, recomputed as
fills accrue (not summed per trade), and the trader SHALL be charged exactly
the recomputed total — increments debit only the difference.

#### Scenario: Two partial fills charge the formula once

- **WHEN** an order fills 3 @ 100 and later 2 @ 100 under min=2.5, max_pct=1,
  per_unit=0.01
- **THEN** total commission charged is 2.5 (the formula over Q=5, V=500), not
  5.0 (2.5 per fill)

### Requirement: Commission settles in the instrument's currency

Commission SHALL be debited from the trader's balance in the currency of the
traded instrument.

#### Scenario: USD instrument charges USD

- **WHEN** a trader pays 2.5 commission on a FAKE (USD-denominated) fill
- **THEN** the trader's USD balance decreases by 2.5 and the FAKE balance is
  unaffected by the commission

### Requirement: Cancelling a partially filled order keeps its commission

Cancelling an order with fills SHALL leave the commission computed over the
filled portion; cancelling an unfilled order SHALL charge nothing.

#### Scenario: Cancel after partial fill

- **WHEN** an order with fulfilled=5 (commission 2.5) is cancelled
- **THEN** the trader's charged commission remains 2.5
