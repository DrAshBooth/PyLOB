"""Acceptance tests for the commissions contract.

Contract: `openspec/changes/spec-commissions-balances/specs/commissions/spec.md`.
The schedule is `min(max_pct * V / 100, max(min_commission, per_unit * Q))` over
the order's cumulative fills, computed in floating point with no currency
quantization -- so every comparison goes through `approx_money`, never `==`.

`engine_factory(commissions=...)` sets the schedule every seeded trader gets;
the plain `engine` fixture leaves commissions at zero.
"""


def test_floor_dominates_a_small_fill(engine_factory, approx_money):
    """Capped per-unit-with-floor formula / Floor dominates a small fill."""
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=0.01))

    engine.limit("ask", 5, 101, tid=1)
    taker = engine.limit("bid", 5, 101, tid=2)

    # V = 505: the 1% cap is 5.05 and the per-unit charge 0.05, so the 2.5
    # floor decides.
    assert taker.fulfilled == 5
    assert taker.commission == approx_money(2.5)
