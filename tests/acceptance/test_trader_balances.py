"""Acceptance tests for the trader-balances contract.

Contract: `openspec/changes/spec-commissions-balances/specs/trader-balances/spec.md`.
A balance is a running per-(trader, instrument-or-currency) bucket moved by
trades and commissions. The values are floating-point sums with no currency
quantization, so every balance comparison goes through `approx_money`, never
`==` and never `round()`.

`engine_factory(commissions=..., self_matching=...)` sets the schedule every
seeded trader gets and the trader ids allowed to match their own resting
orders; the plain `engine` fixture leaves commissions at zero and self-matching
off for everyone.
"""

FAKE = "FAKE"
USD = "USD"


# --------------------------------------------------------------------------
# Requirement: Trades move balances symmetrically
# --------------------------------------------------------------------------


def test_single_trade_moves_both_sides(engine_factory, approx_money):
    """Trades move balances symmetrically / Single trade, both sides."""
    engine = engine_factory(commissions=dict(min=2.5, max_pct=1, per_unit=0.01))

    engine.limit("ask", 5, 101, tid=1)
    taker = engine.limit("bid", 5, 101, tid=2)

    assert taker.fulfilled == 5

    # Trade is 5 @ 101 = 505; each side pays the 2.5 commission floor (the 1%
    # cap is 5.05 and the per-unit charge 0.05).
    assert engine.balance(2, FAKE) == approx_money(5)
    assert engine.balance(2, USD) == approx_money(-507.5)
    assert engine.balance(1, FAKE) == approx_money(-5)
    assert engine.balance(1, USD) == approx_money(502.5)


def test_trade_movements_are_exactly_offsetting(engine, approx_money):
    """Trades move balances symmetrically / requirement text: the four movements offset.

    With commissions at zero the "before commissions" qualifier is literal, so
    the buyer's two movements and the seller's two movements have to cancel
    exactly: no value is created or destroyed by a trade.
    """
    engine.limit("ask", 5, 101, tid=1)
    taker = engine.limit("bid", 5, 101, tid=2)

    assert taker.fulfilled == 5

    buyer_instrument = engine.balance(2, FAKE)
    buyer_currency = engine.balance(2, USD)
    seller_instrument = engine.balance(1, FAKE)
    seller_currency = engine.balance(1, USD)

    # The individual movements: +Q / -Q x P for the buyer, the mirror for the
    # seller.
    assert buyer_instrument == approx_money(5)
    assert buyer_currency == approx_money(-505)
    assert seller_instrument == approx_money(-5)
    assert seller_currency == approx_money(505)

    # And the invariant itself, stated as the invariant rather than as four
    # numbers: each pair sums to zero across the two traders.
    assert buyer_instrument + seller_instrument == approx_money(0)
    assert buyer_currency + seller_currency == approx_money(0)


def test_movements_offset_across_several_trades(engine, approx_money):
    """Trades move balances symmetrically / requirement text: the invariant holds per trade.

    The requirement is per trade, so it survives a taker sweeping several
    resting orders at different prices -- the offset is not an artefact of a
    single clean fill.
    """
    engine.limit("ask", 3, 101, tid=1)
    engine.limit("ask", 2, 102, tid=1)
    taker = engine.limit("bid", 5, 102, tid=2)

    assert [(trade.price, trade.qty) for trade in taker.trades] == [(101, 3), (102, 2)]

    # 3 x 101 + 2 x 102 = 507.
    assert engine.balance(2, FAKE) == approx_money(5)
    assert engine.balance(2, USD) == approx_money(-507)
    assert engine.balance(1, FAKE) == approx_money(-5)
    assert engine.balance(1, USD) == approx_money(507)

    assert engine.balance(1, FAKE) + engine.balance(2, FAKE) == approx_money(0)
    assert engine.balance(1, USD) + engine.balance(2, USD) == approx_money(0)


# --------------------------------------------------------------------------
# Requirement: Balances track, they do not gate
# --------------------------------------------------------------------------


def test_selling_without_holdings_executes(engine, approx_money):
    """Balances track, they do not gate / Selling without holdings.

    Short selling is a first-class research workload: a trader holding nothing
    sells anyway, the trade executes in full, and the balance simply records
    the negative. An engine that grew a holdings check here would break this
    test, which is the point of it.
    """
    assert engine.balance(1, FAKE) == approx_money(0)

    engine.limit("bid", 5, 101, tid=2)
    seller = engine.limit("ask", 5, 101, tid=1)

    assert seller.fulfilled == 5
    assert not seller.cancelled
    assert engine.balance(1, FAKE) == approx_money(-5)
    assert engine.balance(1, USD) == approx_money(505)


def test_buying_without_funds_executes(engine, approx_money):
    """Balances track, they do not gate / requirement text: no sufficient-funds check.

    The mirror of short selling: a trader with no currency balance buys, and
    the currency balance goes negative rather than the order being rejected or
    the fill being trimmed to what the balance affords.
    """
    assert engine.balance(2, USD) == approx_money(0)

    engine.limit("ask", 5, 101, tid=1)
    buyer = engine.limit("bid", 5, 101, tid=2)

    assert buyer.fulfilled == 5
    assert not buyer.cancelled
    assert engine.balance(2, USD) == approx_money(-505)
    assert engine.balance(2, FAKE) == approx_money(5)


def test_negative_balances_do_not_gate_later_orders(engine, approx_money):
    """Balances track, they do not gate / requirement text: an existing deficit gates nothing.

    Not just the first order: a trader already deep in the red goes on
    accumulating, so no margin check appears once a balance is negative
    either.
    """
    for _ in range(3):
        engine.limit("bid", 5, 101, tid=2)
        seller = engine.limit("ask", 5, 101, tid=1)
        assert seller.fulfilled == 5

    assert engine.balance(1, FAKE) == approx_money(-15)
    assert engine.balance(2, USD) == approx_money(-1515)

    # Still no gate: the next order is accepted and matched like any other.
    engine.limit("bid", 5, 101, tid=2)
    last = engine.limit("ask", 5, 101, tid=1)

    assert last.fulfilled == 5
    assert engine.balance(1, FAKE) == approx_money(-20)
    assert engine.balance(2, USD) == approx_money(-2020)


# --------------------------------------------------------------------------
# Requirement: Self-matching is gated per trader
# --------------------------------------------------------------------------


def test_own_resting_order_is_skipped(engine, approx_money):
    """Self-matching is gated per trader / Own order is skipped."""
    own_ask = engine.limit("ask", 5, 101, tid=1)
    other_ask = engine.limit("ask", 5, 102, tid=3)

    taker = engine.limit("bid", 5, 102, tid=1)

    # Trader 1's own ask is the best, and is passed over: the bid trades with
    # trader 3 at 102, the worse price.
    assert [(trade.price, trade.qty) for trade in taker.trades] == [(102, 5)]
    assert other_ask.fulfilled == 5

    # The skipped order is untouched -- not cancelled, not filled, still
    # resting at the front of the book.
    assert own_ask.fulfilled == 0
    assert not own_ask.cancelled
    assert own_ask.resting
    assert engine.best("ask") == approx_money(101)
    assert [entry.idNum for entry in engine.snapshot("ask")] == [own_ask.idNum]

    # Trader 1 bought 5 @ 102 from trader 3; the resting ask moved nothing.
    assert engine.balance(1, FAKE) == approx_money(5)
    assert engine.balance(1, USD) == approx_money(-510)
    assert engine.balance(3, FAKE) == approx_money(-5)
    assert engine.balance(3, USD) == approx_money(510)


def test_self_matching_flag_enables_matching_own_order(engine_factory, approx_money):
    """Self-matching is gated per trader / Flag enables self-match."""
    engine = engine_factory(self_matching=(1,))

    own_ask = engine.limit("ask", 5, 101, tid=1)
    other_ask = engine.limit("ask", 5, 102, tid=3)

    taker = engine.limit("bid", 5, 102, tid=1)

    # Same setup, flag set: the best ask is now eligible and matching takes it
    # at 101, leaving trader 3's ask alone.
    assert [(trade.price, trade.qty) for trade in taker.trades] == [(101, 5)]
    assert own_ask.fulfilled == 5
    assert other_ask.fulfilled == 0
    assert other_ask.resting

    # Both sides of the trade are the same trader, so the four movements
    # cancel within one balance sheet.
    assert engine.balance(1, FAKE) == approx_money(0)
    assert engine.balance(1, USD) == approx_money(0)


def test_self_matching_is_gated_per_trader(engine_factory, approx_money):
    """Self-matching is gated per trader / requirement text: the flag is per trader.

    The flag belongs to the trader, not to the book: with it set for trader 1
    only, trader 3's own resting order is still skipped in the same book.
    """
    engine = engine_factory(self_matching=(1,))

    gated_ask = engine.limit("ask", 5, 101, tid=3)
    other_ask = engine.limit("ask", 5, 102, tid=1)

    taker = engine.limit("bid", 5, 102, tid=3)

    assert [(trade.price, trade.qty) for trade in taker.trades] == [(102, 5)]
    assert gated_ask.fulfilled == 0
    assert gated_ask.resting
    assert other_ask.fulfilled == 5
    assert engine.balance(3, FAKE) == approx_money(5)
    assert engine.balance(3, USD) == approx_money(-510)
