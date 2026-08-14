"""Acceptance tests for the order-lifecycle contract.

Contract: `openspec/specs/order-lifecycle/spec.md`. Every test here names the
requirement and scenario it encodes, and asserts the *target* behaviour --
what an engine has to do, not what any engine does today.

Fixtures come from `tests/acceptance/conftest.py`: `engine` is an empty book
reached through the engine-neutral adapter surface, never through an engine's
own API, so a scenario here states a contract rather than an implementation.

The keyword-surface requirements below are about a library offering *two*
spellings of one operation, so their tests need to say which spelling they
mean. `engine.cancel(..., keyword=True)` and `engine.modify(..., keyword=True)`
are that switch: which spelling is which, and what each names its clock, is
engine knowledge and lives in `tests/harness/` with the rest of it. The
scenarios that assert on what was *recorded* rather than on the book take
`engine_factory(capture=True)` and read `engine.recorded()`.
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


def test_a_fully_filled_order_cannot_be_modified(engine):
    """Modify is validated and priority-aware / Fully filled order cannot be modified."""
    resting = engine.limit("bid", 10, 100, tid=100)
    engine.limit("ask", 10, 100, tid=101)
    assert resting.fulfilled == 10 and not resting.resting

    with pytest.raises(Exception):
        engine.modify(resting, qty=25)

    assert resting.qty == 10
    assert resting.fulfilled == 10
    assert not resting.resting
    assert engine.snapshot("bid") == ()


def test_an_order_finished_by_the_clamp_stays_finished(engine):
    """Modify is validated and priority-aware / An order finished by the clamp stays finished."""
    resting = engine.limit("bid", 10, 100, tid=100)
    engine.limit("ask", 6, 100, tid=101)
    engine.modify(resting, qty=4)
    assert resting.qty == 6 and not resting.resting

    with pytest.raises(Exception):
        engine.modify(resting, qty=20)

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


# --------------------------------------------------------------------------
# Requirement: Cancel and modify are reachable by identifier and keyword
# --------------------------------------------------------------------------


def test_cancelling_by_identifier_alone(engine_factory):
    """Cancel and modify are reachable by identifier and keyword / Cancelling by identifier alone.

    Three orders share a price, so an operation that took the identifier for
    something else -- a side, a position in the queue -- would reach the wrong
    one of them rather than nothing at all.
    """
    engine = engine_factory(capture=True)
    first = engine.limit("bid", 5, 100, tid=100)
    doomed = engine.limit("bid", 5, 100, tid=101)
    third = engine.limit("bid", 5, 100, tid=102)

    engine.cancel(doomed, side=None, keyword=True)

    assert doomed.cancelled and not doomed.resting
    assert not first.cancelled and first.resting
    assert not third.cancelled and third.resting
    assert [entry.idNum for entry in engine.snapshot("bid")] == [
        first.idNum,
        third.idNum,
    ]

    # ... and the stream carries the cancellation the other spelling emits:
    # the caller's own cancel, not the engine cancelling a remainder of its
    # own, which is the distinction a replay re-issues on.
    cancellations = [
        event for event in engine.recorded() if event["kind"] == "cancelled"
    ]
    assert [(event["idNum"], event["reason"]) for event in cancellations] == [
        (doomed.idNum, "requested")
    ]


def _mixed_workload(engine, keyword):
    """Submissions, cancellations and modifications, in one spelling or the other.

    Every shape the two operations have: a modification that raises quantity
    and so re-queues, one that lowers it and so does not, a reprice of an order
    that has *already traded* with its quantity left unnamed -- which is where
    "leave that one alone" and "leave what is left of it" come apart, and so
    the one modification a second copy of the rule is likeliest to get wrong --
    a cancel addressed by identifier alone and one that names the side, and an
    arrival that trades against what a reprice moved.
    """
    resting = engine.limit("ask", 5, 101, tid=1)
    deep = engine.limit("ask", 7, 102, tid=2)
    bid = engine.limit("bid", 4, 99, tid=3)

    engine.modify(bid, qty=6, keyword=keyword)
    engine.modify(bid, qty=5, keyword=keyword)
    engine.limit("ask", 2, 99, tid=5)  # takes 2 of the bid, leaving 3
    engine.modify(bid, price=98, keyword=keyword)
    engine.modify(deep, price=101.5, keyword=keyword)
    engine.cancel(resting, side=None, keyword=keyword)
    engine.limit("bid", 3, 102, tid=4)  # crosses the repriced ask
    engine.cancel(bid, side="bid", keyword=keyword)


def test_either_spelling_records_the_same_stream(engine_factory):
    """Cancel and modify are reachable by identifier and keyword / Either spelling records the same stream.

    Against a real recorded stream, event for event, rather than by reading
    the two implementations: a keyword spelling that re-derived the clamp rule
    or the re-queueing rule instead of delegating would pass every scenario
    written about its *own* behaviour and fail here, which is the only place
    the second copy would show.
    """
    keyworded = engine_factory(capture=True)
    positional = engine_factory(capture=True)

    _mixed_workload(keyworded, keyword=True)
    _mixed_workload(positional, keyword=False)

    recorded = keyworded.recorded()
    assert recorded == positional.recorded()

    # The comparison is only worth what the workload put in it: two identical
    # empty streams would pass the line above.
    kinds = {event["kind"] for event in recorded}
    assert {"accepted", "modified", "cancelled", "filled"} <= kinds
    requeued = {
        event["reprioritized"] for event in recorded if event["kind"] == "modified"
    }
    assert requeued == {True, False}, "both halves of the priority rule ran"


def test_a_side_that_is_not_the_orders_still_raises(engine):
    """Cancel and modify are reachable by identifier and keyword / A side that is not the order's still raises."""
    resting = engine.limit("bid", 5, 100, tid=100)
    before = engine.snapshot("bid")

    with pytest.raises(Exception):
        engine.cancel(resting, side="ask", keyword=True)

    assert not resting.cancelled
    assert resting.resting
    assert engine.snapshot("bid") == before


def test_an_unknown_identifier_still_raises_on_the_keyword_surface(engine):
    """Cancel and modify are reachable by identifier and keyword / An unknown identifier still raises."""
    resting = engine.limit("bid", 5, 100, tid=100)
    before = engine.snapshot("bid")

    with pytest.raises(Exception):
        engine.cancel(resting.idNum + 4242, side=None, keyword=True)
    with pytest.raises(Exception):
        engine.modify(resting.idNum + 4242, qty=3, side=None, keyword=True)

    assert engine.snapshot("bid") == before


# --------------------------------------------------------------------------
# Requirement: The keyword operations name the clock once
# --------------------------------------------------------------------------


def test_one_name_across_the_three_operations(engine_factory):
    """The keyword operations name the clock once / One name across the three operations.

    The "same keyword name" half of the scenario is enforced where the name is
    known: `tests/harness/` passes the clock to submission, modification and
    cancellation under one keyword, so a surface that spelled any of the three
    differently would not reach this assertion at all. What is left to assert
    here is the other half -- that each emitted event carries the value its own
    call supplied, rather than a clock the engine kept for itself.

    The three values are far apart and not consecutive on purpose: an engine
    that dropped the clock it was handed and advanced its own by one would
    land on the right answer for stamps chosen 1000, 1001, 1002.
    """
    engine = engine_factory(capture=True)

    order = engine.limit("bid", 5, 100, tid=100, idNum=11, timestamp=1000.0)
    engine.modify(order, qty=3, timestamp=1010.0, keyword=True)
    engine.cancel(order, side=None, timestamp=1025.0, keyword=True)

    stamped = {
        event["kind"]: event["timestamp"]
        for event in engine.recorded()
        if event["kind"] in ("accepted", "modified", "cancelled")
    }
    assert stamped == {"accepted": 1000.0, "modified": 1010.0, "cancelled": 1025.0}


# --------------------------------------------------------------------------
# Requirement: A modification names what it changes
# --------------------------------------------------------------------------


def test_an_omitted_field_is_left_alone(engine):
    """A modification names what it changes / An omitted field is left alone."""
    resting = engine.limit("bid", 5, 100, tid=100)

    engine.modify(resting, qty=3, keyword=True)

    assert resting.qty == 3
    assert resting.price == 100
    assert resting.resting
    # "and it does not become a market order": an unpriced order would cross
    # every level and never rest, so both halves of this matter.
    assert resting.state.order_type == "limit"
    assert [entry.price for entry in engine.snapshot("bid")] == [100]


def test_a_modification_that_names_nothing_raises(engine_factory):
    """A modification names what it changes / A modification that names nothing raises."""
    engine = engine_factory(capture=True)
    resting = engine.limit("bid", 5, 100, tid=100)
    before = engine.snapshot("bid")
    quiet = len(engine.recorded())

    with pytest.raises(Exception):
        engine.modify(resting, keyword=True)

    assert resting.qty == 5
    assert resting.price == 100
    assert resting.resting
    assert engine.snapshot("bid") == before
    # "and no event is emitted": a modification that changed nothing would
    # still cost every recorded session a row, which is the reason to refuse it
    # rather than apply it.
    assert len(engine.recorded()) == quiet


def test_clamping_and_priority_rules_are_unchanged(engine):
    """A modification names what it changes / Clamping and priority rules are unchanged.

    The same scenario as "Quantity reduced below fills clamps" above, asked of
    the keyword spelling: "exactly as the existing modify would have done".
    """
    resting = engine.limit("bid", 10, 100, tid=100)
    engine.limit("ask", 6, 100, tid=101)
    assert resting.fulfilled == 6

    engine.modify(resting, qty=4, keyword=True)

    assert resting.qty == 6
    assert resting.fulfilled == 6
    assert not resting.resting
    assert engine.snapshot("bid") == ()
