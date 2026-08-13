"""Acceptance tests for the book-queries contract.

Contract: `openspec/specs/book-queries/spec.md`. Every test here names the
requirement and scenario it encodes, and asserts the *target* behaviour --
what an engine has to do, not what any engine does today.

The read side leans on one premise from the order-lifecycle contract: under
immediate-or-cancel market orders the book holds priced limit orders only, so
every query has a defined answer. Two scenarios below exercise exactly that
premise, by asking the queries about a side a market order has just run
through: an engine that rested the remainder instead of cancelling it would
have an unpriced order in the book, invisible to the best-price query and
still counted by volume-at-price.

Fixtures come from `tests/acceptance/conftest.py`: `engine` is an empty book
reached through the engine-neutral adapter surface, never through an engine's
own API, so a scenario here states a contract rather than an implementation.
"""

# --------------------------------------------------------------------------
# Requirement: Best and worst prices reflect resting limit orders
# --------------------------------------------------------------------------


def test_non_empty_side_reports_prices(engine):
    """Best and worst prices reflect resting limit orders / Non-empty side always reports a price."""
    engine.limit("bid", 5, 98, tid=1)
    engine.limit("bid", 5, 99, tid=2)
    engine.limit("ask", 4, 101, tid=3)
    engine.limit("ask", 4, 102, tid=4)

    assert engine.best("bid") == 99
    assert engine.worst("bid") == 98
    assert engine.best("ask") == 101
    assert engine.worst("ask") == 102


def test_partially_filled_order_still_reports_a_price(engine):
    """Best and worst prices reflect resting limit orders / Non-empty side always reports a price."""
    engine.limit("bid", 5, 99, tid=1)
    engine.limit("ask", 3, 99, tid=2)

    # 2 of the 5 remain: the order still rests, so the side still has a price.
    assert engine.best("bid") == 99
    assert engine.worst("bid") == 99


def test_market_order_leaves_the_price_queries_intact(engine):
    """Best and worst prices reflect resting limit orders / Non-empty side always reports a price."""
    engine.limit("ask", 3, 101, tid=100)
    engine.limit("bid", 5, 97, tid=101)

    # Outruns the ask side: the remainder is cancelled, not rested, so the
    # only thing left on the bid side is the priced limit order.
    engine.market("bid", 8, tid=102)

    assert engine.best("bid") == 97
    assert engine.worst("bid") == 97


def test_empty_side_reports_none(engine):
    """Best and worst prices reflect resting limit orders / Empty side reports None."""
    cancelled = engine.limit("bid", 5, 99, tid=1)
    engine.limit("ask", 5, 101, tid=2)

    engine.cancel(cancelled)

    assert engine.best("bid") is None
    assert engine.worst("bid") is None
    assert engine.best("ask") == 101

    engine.limit("bid", 5, 101, tid=3)  # fills the ask in full

    assert engine.best("ask") is None
    assert engine.worst("ask") is None


def test_side_emptied_by_an_unfillable_market_order_reports_none(engine):
    """Best and worst prices reflect resting limit orders / Empty side reports None."""
    engine.limit("ask", 3, 101, tid=100)

    engine.market("bid", 8, tid=102)

    # The unfilled 5 is cancelled, so None here means "empty", and every
    # other query agrees that the side is empty.
    assert engine.best("bid") is None
    assert engine.worst("bid") is None
    assert engine.snapshot("bid") == ()
    assert engine.volume_at("bid", 1) == 0


# --------------------------------------------------------------------------
# Requirement: Volume at price answers the marketable question
# --------------------------------------------------------------------------


def test_volume_at_price_aggregates_across_levels(engine):
    """Volume at price answers the marketable question / Aggregates across price levels."""
    engine.limit("bid", 5, 99, tid=1)
    engine.limit("bid", 5, 98, tid=2)

    assert engine.volume_at("bid", 98) == 10

    engine.limit("ask", 5, 101, tid=3)
    engine.limit("ask", 5, 102, tid=4)

    assert engine.volume_at("ask", 102) == 10


def test_volume_at_price_excludes_non_marketable_levels(engine):
    """Volume at price answers the marketable question / Excludes non-marketable levels."""
    engine.limit("bid", 5, 99, tid=1)
    engine.limit("bid", 5, 97, tid=2)

    assert engine.volume_at("bid", 98) == 5
    assert engine.volume_at("bid", 100) == 0  # nothing qualifies

    engine.limit("ask", 5, 101, tid=3)
    engine.limit("ask", 5, 103, tid=4)

    assert engine.volume_at("ask", 102) == 5
    assert engine.volume_at("ask", 100) == 0


# --------------------------------------------------------------------------
# Requirement: Last-trade price is reporting, not matching state
# --------------------------------------------------------------------------


def test_last_price_updates_on_every_trade(engine):
    """Last-trade price is reporting, not matching state / Updates on every trade."""
    assert engine.last_price() is None

    engine.limit("ask", 5, 101.0, tid=1)
    engine.limit("bid", 5, 101.0, tid=2)

    assert engine.last_price() == 101.0

    engine.limit("ask", 2, 102.5, tid=1)
    engine.limit("bid", 2, 102.5, tid=2)

    assert engine.last_price() == 102.5


def test_last_price_survives_reload(engine):
    """Last-trade price is reporting, not matching state / Survives reload."""
    engine.limit("ask", 5, 101.0, tid=1)
    engine.limit("bid", 5, 101.0, tid=2)
    assert engine.last_price() == 101.0

    reloaded = engine.reopen()

    assert reloaded.last_price() == 101.0


# --------------------------------------------------------------------------
# Requirement: Book snapshot is complete and consistent
# --------------------------------------------------------------------------


def test_snapshot_agrees_with_queries(engine):
    """Book snapshot is complete and consistent / Snapshot agrees with queries."""
    best_bid = engine.limit("bid", 5, 99, tid=1)
    deep_bid = engine.limit("bid", 7, 98, tid=2)
    later_bid = engine.limit("bid", 3, 99, tid=3)  # same price, behind best_bid
    best_ask = engine.limit("ask", 4, 101, tid=4)
    deep_ask = engine.limit("ask", 6, 103, tid=5)

    cancelled = engine.limit("bid", 2, 99.5, tid=100)
    engine.cancel(cancelled)

    # Takes best_bid in full and 1 of later_bid: a filled order leaves, a
    # partially filled one stays with its remainder.
    engine.limit("ask", 6, 99, tid=101)

    bids = engine.snapshot("bid")
    asks = engine.snapshot("ask")

    # Every resting order exactly once, in matching-priority order: price
    # first, then time. Nothing cancelled or fully filled.
    assert [entry.idNum for entry in bids] == [later_bid.idNum, deep_bid.idNum]
    assert {best_bid.idNum, cancelled.idNum}.isdisjoint(e.idNum for e in bids)
    assert [entry.idNum for entry in asks] == [best_ask.idNum, deep_ask.idNum]
    assert [entry.price for entry in bids] == [99, 98]
    assert [entry.price for entry in asks] == [101, 103]
    assert [entry.available for entry in bids] == [2, 7]
    assert [entry.available for entry in asks] == [4, 6]
    for entry in bids + asks:
        assert entry.available == entry.qty - entry.fulfilled

    # ... and the price and volume queries agree with it at the same moment.
    assert engine.best("bid") == bids[0].price
    assert engine.worst("bid") == bids[-1].price
    assert engine.best("ask") == asks[0].price
    assert engine.worst("ask") == asks[-1].price
    assert engine.volume_at("bid", bids[-1].price) == sum(e.available for e in bids)
    assert engine.volume_at("ask", asks[-1].price) == sum(e.available for e in asks)
    assert engine.volume_at("bid", bids[0].price) == bids[0].available
