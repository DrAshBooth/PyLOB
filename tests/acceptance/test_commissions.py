"""Acceptance tests for the commissions contract.

Contract: `openspec/changes/spec-commissions-balances/specs/commissions/spec.md`.
The schedule is `min(max_pct * V / 100, max(min_commission, per_unit * Q))` over
the order's cumulative fills, computed in floating point with no currency
quantization -- so every comparison goes through `approx_money`, never `==`.

`engine_factory(commissions=...)` sets the schedule every seeded trader gets;
the plain `engine` fixture leaves commissions at zero.

Where a scenario is about what the *charge* did to a balance rather than what
the order reports, it is measured against a control engine running the same
trades with no schedule at all: the difference between the two is the
commission, whatever sign convention the balance itself uses. `_charged` is
that measurement.
"""

INSTRUMENT = "FAKE"
CURRENCY = "USD"


def _charged(commissioned, control, tid, instrument=CURRENCY):
    """What the schedule cost `tid`: the balance the control kept and this one did not.

    Both engines must have been driven through the same trades. Reading the
    difference rather than the balance keeps these tests about commission and
    leaves the balance sign convention to the trader-balances contract.
    """
    return control.balance(tid, instrument) - commissioned.balance(tid, instrument)


def _mirror(script, commissioned, control):
    """Drive both engines through `script`; return what it returned on the commissioned one."""
    ref = script(commissioned)
    script(control)
    return ref


# --------------------------------------------------------------------------
# Requirement: Commission follows the capped per-unit-with-floor formula
# --------------------------------------------------------------------------


def test_floor_dominates_a_small_fill(engine_factory, approx_money):
    """Capped per-unit-with-floor formula / Floor dominates a small fill."""
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=0.01))

    engine.limit("ask", 5, 101, tid=1)
    taker = engine.limit("bid", 5, 101, tid=2)

    # V = 505: the 1% cap is 5.05 and the per-unit charge 0.05, so the 2.5
    # floor decides.
    assert taker.fulfilled == 5
    assert taker.commission == approx_money(2.5)


def test_per_unit_beats_the_floor_with_the_cap_slack(engine_factory, approx_money):
    """Capped per-unit-with-floor formula / Per-unit beats the floor, cap does not bind."""
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=0.01))

    engine.limit("ask", 500, 101, tid=1)
    taker = engine.limit("bid", 500, 101, tid=2)

    # V = 50500: the per-unit charge is 5.0, over the 2.5 floor and well under
    # the 505.0 cap.
    assert taker.fulfilled == 500
    assert taker.commission == approx_money(5.0)


def test_percentage_cap_binds_ahead_of_the_floor(engine_factory, approx_money):
    """Capped per-unit-with-floor formula / Percentage cap binds.

    The load-bearing case: the cap (1.0) is *below* the floor (2.5), and the
    commission is the cap. `min_commission` floors the per-unit charge, never
    the order's commission.
    """
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=1.0))

    engine.limit("ask", 10, 10, tid=1)
    taker = engine.limit("bid", 10, 10, tid=2)

    # V = 100. per-unit charge = max(2.5, 1.0 * 10) = 10.0; cap = 1% of 100
    # = 1.0. min(1.0, 10.0) = 1.0 -- under the 2.5 floor, which does not apply.
    assert taker.fulfilled == 10
    assert taker.commission == approx_money(1.0)
    assert taker.commission < 2.5


def test_an_order_with_no_fills_carries_zero_commission(engine_factory, approx_money):
    """Capped per-unit-with-floor formula / an order with no fills carries zero commission.

    Q = 0, so the floor never fires on its own -- a resting order that has
    traded nothing owes nothing, and nothing has left its owner's balance.
    """
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=0.01))

    resting = engine.limit("bid", 5, 100, tid=1)

    assert resting.fulfilled == 0
    assert resting.resting
    assert resting.commission == approx_money(0.0)
    assert engine.balance(1, CURRENCY) == approx_money(0.0)


def test_commission_is_not_quantized_to_currency_precision(
    engine_factory, approx_money
):
    """Capped per-unit-with-floor formula / no rounding or currency quantization.

    The contract is the exact value of the formula. Parameters here put the
    result at 0.03535 -- below a cent, and not a round number of them, so any
    quantization step would be visible.
    """
    engine = engine_factory(commissions=dict(min=0.0, max_pct=0.007, per_unit=0.01))

    engine.limit("ask", 5, 101, tid=1)
    taker = engine.limit("bid", 5, 101, tid=2)

    # V = 505: cap = 0.007 * 505 / 100 = 0.03535, under the 0.05 per-unit
    # charge. Rounding to cents would give 0.04, to whole units 0.0.
    assert taker.fulfilled == 5
    assert taker.commission == approx_money(0.03535)


def test_fill_value_sums_qty_times_price_over_the_fills(engine_factory, approx_money):
    """Capped per-unit-with-floor formula / V is the sum of qty x price over the fills."""
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=2.0))

    engine.limit("ask", 5, 100, tid=1)
    engine.limit("ask", 5, 102, tid=3)
    taker = engine.limit("bid", 10, 102, tid=2)

    # Two fills at different prices: V = 5 * 100 + 5 * 102 = 1010, so the cap
    # is 10.10 -- not 10.0 (Q x first price) and not 10.20 (Q x last price).
    # The per-unit charge, 20.0, is well over it.
    assert taker.fulfilled == 10
    assert taker.commission == approx_money(10.10)


# --------------------------------------------------------------------------
# Requirement: Commission is recomputed on cumulative fills
# --------------------------------------------------------------------------


def test_two_partial_fills_charge_the_formula_once(engine_factory, approx_money):
    """Recomputed on cumulative fills / Two partial fills charge the formula once."""
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=0.01))
    control = engine_factory()

    def script(book):
        maker = book.limit("bid", 5, 100, tid=1)
        book.limit("ask", 3, 100, tid=2)
        book.limit("ask", 2, 100, tid=3)
        return maker

    maker = _mirror(script, engine, control)

    # Q = 5, V = 500: floor 2.5 under both the 5.0 cap and above the 0.05
    # per-unit charge. Accruing the formula per fill would charge 5.0.
    assert maker.fulfilled == 5
    assert maker.commission == approx_money(2.5)
    assert _charged(engine, control, tid=1) == approx_money(2.5)


def test_an_increment_debits_only_the_difference(engine_factory, approx_money):
    """Recomputed on cumulative fills / increments debit only the difference.

    A schedule whose commission genuinely grows between the two fills: the
    second fill must move the balance by the delta (2.0), not by the whole
    recomputed total (5.0).
    """
    engine = engine_factory(commissions=dict(min=0.0, max_pct=100, per_unit=1.0))
    control = engine_factory()

    maker = _mirror(lambda book: book.limit("bid", 5, 100, tid=1), engine, control)

    _mirror(lambda book: book.limit("ask", 3, 100, tid=2), engine, control)
    after_first = _charged(engine, control, tid=1)
    commission_after_first = maker.commission

    _mirror(lambda book: book.limit("ask", 2, 100, tid=3), engine, control)
    after_second = _charged(engine, control, tid=1)

    # Q = 3, V = 300 -> 3.0; then Q = 5, V = 500 -> 5.0. The cap (100% of V)
    # never binds, so the per-unit charge is the commission throughout.
    assert commission_after_first == approx_money(3.0)
    assert maker.commission == approx_money(5.0)
    assert after_first == approx_money(3.0)
    assert after_second == approx_money(5.0)
    assert after_second - after_first == approx_money(2.0)


# --------------------------------------------------------------------------
# Requirement: Commission settles in the instrument's currency
# --------------------------------------------------------------------------


def test_usd_instrument_charges_usd(engine_factory, approx_money):
    """Settles in the instrument's currency / USD instrument charges USD."""
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=0.01))
    control = engine_factory()

    def script(book):
        book.limit("ask", 5, 101, tid=1)
        return book.limit("bid", 5, 101, tid=2)

    taker = _mirror(script, engine, control)

    assert taker.commission == approx_money(2.5)
    # FAKE is USD-denominated: the 2.5 comes out of USD, and the FAKE position
    # is exactly what the same trade produced with no schedule at all.
    assert _charged(engine, control, tid=2, instrument=CURRENCY) == approx_money(2.5)
    assert engine.balance(2, INSTRUMENT) == approx_money(control.balance(2, INSTRUMENT))


# --------------------------------------------------------------------------
# Requirement: Cancelling a partially filled order keeps its commission
# --------------------------------------------------------------------------


def test_cancel_after_partial_fill_keeps_the_commission(engine_factory, approx_money):
    """Cancelling keeps its commission / Cancel after partial fill."""
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=0.01))
    control = engine_factory()

    def script(book):
        maker = book.limit("bid", 10, 100, tid=1)
        book.limit("ask", 5, 100, tid=2)
        book.cancel(maker)
        return maker

    maker = _mirror(script, engine, control)

    # fulfilled = 5, V = 500 -> the 2.5 floor; cancelling the unfilled
    # remainder neither refunds it nor charges the untraded 5.
    assert maker.fulfilled == 5
    assert maker.cancelled
    assert maker.commission == approx_money(2.5)
    assert _charged(engine, control, tid=1) == approx_money(2.5)


def test_cancelling_an_unfilled_order_charges_nothing(engine_factory, approx_money):
    """Cancelling keeps its commission / cancelling an unfilled order charges nothing."""
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=0.01))

    resting = engine.limit("bid", 5, 100, tid=1)
    engine.cancel(resting)

    assert resting.fulfilled == 0
    assert resting.cancelled
    assert resting.commission == approx_money(0.0)
    assert engine.balance(1, CURRENCY) == approx_money(0.0)
