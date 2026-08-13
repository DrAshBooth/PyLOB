"""Acceptance tests for the order-lifecycle contract.

Contract: `openspec/specs/order-lifecycle/spec.md`. Every test here names the
requirement and scenario it encodes, and asserts the *target* behaviour --
what an engine has to do, not what any engine does today.

Fixtures come from `tests/acceptance/conftest.py`: `engine` is an empty book
reached through the engine-neutral adapter surface, never through an engine's
own API, so a scenario here states a contract rather than an implementation.
"""

import pytest


# --------------------------------------------------------------------------
# Requirement: Market orders are immediate-or-cancel
# --------------------------------------------------------------------------


def test_trades_price_at_the_maker(engine):
    """Market orders are immediate-or-cancel / Trades price at the maker."""
    engine.limit("ask", 3, 101, tid=100)
    engine.limit("ask", 2, 102, tid=101)

    taker = engine.market("bid", 5, tid=102)

    assert [(trade.price, trade.qty) for trade in taker.trades] == [(101, 3), (102, 2)]


def test_market_remainder_is_cancelled(engine):
    """Market orders are immediate-or-cancel / Remainder is cancelled when liquidity runs out."""
    engine.limit("ask", 3, 101, tid=100)
    engine.limit("ask", 2, 102, tid=101)

    taker = engine.market("bid", 8, tid=102)

    assert taker.fulfilled == 5
    assert taker.cancelled
    assert not taker.resting
    assert engine.snapshot("bid") == ()


def test_market_order_on_an_empty_opposite_side(engine):
    """Market orders are immediate-or-cancel / Market order on an empty opposite side."""
    engine.limit("bid", 4, 99, tid=100)
    before = engine.snapshot("bid")

    taker = engine.market("bid", 6, tid=101)

    assert taker.trades == ()
    assert taker.fulfilled == 0
    assert taker.cancelled
    assert not taker.resting
    # The book is untouched: the market order left no entry of its own, and
    # the resting bid is exactly where it was.
    assert engine.snapshot("bid") == before
    assert engine.snapshot("ask") == ()


# --------------------------------------------------------------------------
# Requirement: Order identifiers are unique and stable
# --------------------------------------------------------------------------


def test_cancel_targets_exactly_one_order(engine):
    """Order identifiers are unique and stable / Cancel targets exactly one order."""
    first = engine.limit("bid", 5, 100, tid=100)
    second = engine.limit("bid", 5, 100, tid=101)
    third = engine.limit("bid", 5, 100, tid=102)

    engine.cancel(second)

    assert second.cancelled
    assert not second.resting
    assert not first.cancelled and first.resting
    assert not third.cancelled and third.resting
    assert [entry.idNum for entry in engine.snapshot("bid")] == [
        first.idNum,
        third.idNum,
    ]


def test_cancel_with_an_unknown_identifier_raises(engine):
    """Order identifiers are unique and stable / Unknown identifier raises."""
    resting = engine.limit("bid", 5, 100, tid=100)
    before = engine.snapshot("bid")

    with pytest.raises(Exception):
        engine.cancel(resting.idNum + 4242, side="bid")

    assert engine.snapshot("bid") == before


def test_modify_with_an_unknown_identifier_raises(engine):
    """Order identifiers are unique and stable / Unknown identifier raises."""
    resting = engine.limit("bid", 5, 100, tid=100)
    before = engine.snapshot("bid")

    with pytest.raises(Exception):
        engine.modify(resting.idNum + 4242, qty=3, price=100, side="bid")

    assert engine.snapshot("bid") == before


def test_identifiers_stay_unique_across_a_reload(engine):
    """Order identifiers are unique and stable / requirement statement.

    "unique within the book's lifetime, including across reloads of persisted
    state" -- the clause has no scenario of its own, and `reopen()` is the
    fixture surface for it.
    """
    first = engine.limit("bid", 5, 100, tid=100)
    issued = first.idNum

    engine.reopen()
    later = engine.limit("bid", 3, 99, tid=101)

    assert later.idNum != issued
    assert [entry.idNum for entry in engine.snapshot("bid")] == [issued, later.idNum]


def test_externally_supplied_duplicate_identifier_is_rejected(engine):
    """Order identifiers are unique and stable / Externally supplied duplicate is rejected."""
    engine.limit("bid", 5, 100, tid=100, idNum=7, timestamp=1)

    with pytest.raises(Exception):
        engine.limit("bid", 3, 99, tid=101, idNum=7, timestamp=2)

    assert [(entry.idNum, entry.qty) for entry in engine.snapshot("bid")] == [(7, 5)]


# --------------------------------------------------------------------------
# Requirement: Invalid submissions raise library exceptions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("qty", [0, -3])
def test_non_positive_quantity_raises(engine, qty):
    """Invalid submissions raise library exceptions / Non-positive quantity."""
    engine.limit("ask", 5, 101, tid=100)
    before = engine.snapshot("ask")

    # An `Exception`, not a `BaseException`: a library error the caller can
    # catch, never a process exit.
    with pytest.raises(Exception):
        engine.limit("bid", qty, 101, tid=101)

    assert engine.snapshot("ask") == before
    assert engine.trades() == ()


# --------------------------------------------------------------------------
# Requirement: Modify is validated and priority-aware
# --------------------------------------------------------------------------


def test_modify_with_a_side_mismatch_raises(engine):
    """Modify is validated and priority-aware / Side mismatch raises."""
    resting = engine.limit("bid", 5, 100, tid=100)

    with pytest.raises(Exception):
        engine.modify(resting, qty=9, side="ask")

    assert resting.side == "bid"
    assert resting.qty == 5
    assert resting.price == 100
    assert resting.resting


def test_quantity_reduced_below_fills_clamps(engine):
    """Modify is validated and priority-aware / Quantity reduced below fills clamps."""
    resting = engine.limit("bid", 10, 100, tid=100)
    engine.limit("ask", 6, 100, tid=101)
    assert resting.fulfilled == 6

    engine.modify(resting, qty=4)

    assert resting.qty == 6
    assert resting.fulfilled == 6
    assert not resting.resting
    assert engine.snapshot("bid") == ()


def test_price_change_loses_time_priority(engine):
    """Modify is validated and priority-aware / Price change loses time priority."""
    first = engine.limit("bid", 5, 100, tid=100)
    second = engine.limit("bid", 5, 100, tid=101)
    assert [entry.idNum for entry in engine.snapshot("bid")] == [
        first.idNum,
        second.idNum,
    ]

    engine.modify(first, price=99)
    engine.modify(first, price=100)

    assert [entry.idNum for entry in engine.snapshot("bid")] == [
        second.idNum,
        first.idNum,
    ]


def test_quantity_decrease_keeps_time_priority(engine):
    """Modify is validated and priority-aware / Quantity decrease keeps time priority."""
    first = engine.limit("bid", 8, 100, tid=100)
    second = engine.limit("bid", 5, 100, tid=101)

    engine.modify(first, qty=3)

    assert first.qty == 3
    assert [entry.idNum for entry in engine.snapshot("bid")] == [
        first.idNum,
        second.idNum,
    ]

    # ... and priority is the matching order, not just the listing order.
    taker = engine.limit("ask", 3, 100, tid=102)
    assert [trade.bid for trade in taker.trades] == [first.idNum]


# --------------------------------------------------------------------------
# Requirement: Price-time priority is deterministic
# --------------------------------------------------------------------------


def _replay_same_timestamp(engine):
    """Two asks at one price carrying one timestamp, then a taker for one of them.

    The identifiers descend (200 then 100) so that an engine ordering by
    identifier rather than by arrival gets the opposite answer.
    """
    first = engine.limit("ask", 3, 101, tid=100, idNum=200, timestamp=17)
    second = engine.limit("ask", 3, 101, tid=101, idNum=100, timestamp=17)
    taker = engine.market("bid", 3, tid=102)
    return first, second, taker


def test_same_timestamp_arrivals_keep_arrival_order(engine_factory):
    """Price-time priority is deterministic / Same-timestamp arrivals keep arrival order."""
    # "on every replay": the same replay on two independent books has to reach
    # the same answer, so a tie cannot be left to whatever order the storage
    # happens to return.
    for _ in range(2):
        first, second, taker = _replay_same_timestamp(engine_factory())

        assert [(trade.ask, trade.qty) for trade in taker.trades] == [(first.idNum, 3)]
        assert first.fulfilled == 3
        assert second.fulfilled == 0


# --------------------------------------------------------------------------
# Requirement: Prices are quantized to the tick
# --------------------------------------------------------------------------


def test_non_decimal_tick(engine_factory):
    """Prices are quantized to the tick / Non-decimal tick."""
    engine = engine_factory(tick_size=0.05)

    resting = engine.limit("bid", 5, 100.03, tid=100)

    assert resting.price == pytest.approx(100.05)
    assert [entry.price for entry in engine.snapshot("bid")] == [pytest.approx(100.05)]
