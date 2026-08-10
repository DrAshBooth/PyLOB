## Purpose

Per-trader running balances in each instrument and each currency, updated by
trades and commissions, so strategy research has online PnL without external
bookkeeping.

## ADDED Requirements

### Requirement: Trades move balances symmetrically

For each trade of quantity Q at price P in an instrument denominated in
currency C: the buyer's instrument balance SHALL increase by Q and their C
balance decrease by Q x P; the seller's instrument balance SHALL decrease by
Q and their C balance increase by Q x P. The four movements SHALL be exactly
offsetting across the two traders (before commissions).

#### Scenario: Single trade, both sides

- **WHEN** trader 2 buys 5 @ 101 FAKE (USD) from trader 1, each with
  commission parameters min=2.5, max_pct=1, per_unit=0.01
- **THEN** trader 2's balances read FAKE +5, USD -507.5 (trade -505,
  commission -2.5) and trader 1's read FAKE -5, USD +502.5

### Requirement: Balances track, they do not gate

Balance state SHALL NOT prevent order acceptance or matching: no margin
check, no sufficient-funds check, negative balances are permitted and
recorded. Any gating behavior is a future, explicitly-specified feature.

#### Scenario: Selling without holdings

- **WHEN** a trader with FAKE balance 0 sells 5 FAKE
- **THEN** the trade executes normally and the balance records -5

### Requirement: Self-matching is gated per trader

A trader's incoming order SHALL NOT match their own resting orders unless the
trader's `allow_self_matching` flag is set; gated resting orders are skipped
and remain in the book, with matching continuing past them in priority order.

#### Scenario: Own order is skipped

- **WHEN** trader 1 has a resting ask at 101 (best) and trader 3 a resting
  ask at 102, and trader 1 submits a marketable bid at 102 with
  allow_self_matching unset
- **THEN** the bid trades with trader 3 at 102 and trader 1's resting ask is
  untouched

#### Scenario: Flag enables self-match

- **WHEN** the same setup occurs but trader 1 has allow_self_matching set
- **THEN** the bid trades with trader 1's own resting ask at 101
