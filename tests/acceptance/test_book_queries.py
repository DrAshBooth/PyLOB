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
Two requirements here are written *per instrument* -- the snapshot and the
last-trade price -- and one instrument cannot tell a book apart from the
engine holding it, so those tests take `engine_factory(instruments=...)` and
ask their queries of one instrument while a second one is loaded.
"""

#: The second instrument those tests load. It sorts alphabetically *before*
#: the default "FAKE", so a query that pooled the books, or answered from
#: whichever one it reached first, would answer with this one.
OTHER = "AAA"

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


def test_last_price_is_reported_per_instrument(engine_factory):
    """Last-trade price is reporting, not matching state / requirement statement.

    "the price of the most recent trade per instrument" -- the clause has no
    scenario of its own, and a single last-trade price for the whole engine
    would satisfy both scenarios above. A second instrument is what makes
    "most recent" ambiguous, and the contract resolves it per instrument: a
    trade in one is not news about the other.
    """
    engine = engine_factory(instruments=((OTHER, "USD"),))

    engine.limit("ask", 5, 101.0, tid=1)
    engine.limit("bid", 5, 101.0, tid=2)

    assert engine.last_price() == 101.0
    assert engine.last_price(OTHER) is None  # no trade there yet

    engine.limit("ask", 3, 97.0, tid=3, instrument=OTHER)
    engine.limit("bid", 3, 97.0, tid=4, instrument=OTHER)

    # The later trade is the last one in its own instrument only; the first
    # instrument still reports the last trade that happened in it.
    assert engine.last_price(OTHER) == 97.0
    assert engine.last_price() == 101.0


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


def test_snapshot_and_queries_are_scoped_to_one_instrument(engine_factory):
    """Book snapshot is complete and consistent / requirement statement.

    "A book snapshot for an instrument SHALL list every resting order ...
    exactly once ... and SHALL agree with the price and volume queries taken
    at the same moment" -- *for an instrument* has no scenario of its own,
    and in a one-instrument engine there is nothing for a query to be scoped
    to. The second instrument here is quoted inside the first one's spread
    and the wrong way round, so pooling the two books would move every price
    query: the best bid would read 100 instead of 99 and the best ask 100.5
    instead of 101.
    """
    engine = engine_factory(instruments=((OTHER, "USD"),))

    best_bid = engine.limit("bid", 5, 99, tid=1)
    deep_bid = engine.limit("bid", 7, 98, tid=2)
    best_ask = engine.limit("ask", 4, 101, tid=3)
    deep_ask = engine.limit("ask", 6, 102, tid=4)

    other_best_bid = engine.limit("bid", 3, 100, tid=5, instrument=OTHER)
    other_deep_bid = engine.limit("bid", 4, 99.5, tid=100, instrument=OTHER)
    other_best_ask = engine.limit("ask", 2, 100.5, tid=101, instrument=OTHER)
    other_deep_ask = engine.limit("ask", 8, 103, tid=102, instrument=OTHER)

    bids = engine.snapshot("bid")
    asks = engine.snapshot("ask")
    other_bids = engine.snapshot("bid", OTHER)
    other_asks = engine.snapshot("ask", OTHER)

    # Every resting order appears in the snapshot of the instrument it was
    # submitted in, in matching-priority order, and in no other snapshot.
    assert [entry.idNum for entry in bids] == [best_bid.idNum, deep_bid.idNum]
    assert [entry.idNum for entry in asks] == [best_ask.idNum, deep_ask.idNum]
    assert [entry.idNum for entry in other_bids] == [
        other_best_bid.idNum,
        other_deep_bid.idNum,
    ]
    assert [entry.idNum for entry in other_asks] == [
        other_best_ask.idNum,
        other_deep_ask.idNum,
    ]

    # ... and the price and volume queries agree with the snapshot of the
    # instrument they name, not with the other one's.
    assert engine.best("bid") == bids[0].price == 99
    assert engine.worst("bid") == bids[-1].price == 98
    assert engine.best("ask") == asks[0].price == 101
    assert engine.worst("ask") == asks[-1].price == 102
    assert engine.best("bid", OTHER) == other_bids[0].price == 100
    assert engine.worst("bid", OTHER) == other_bids[-1].price == 99.5
    assert engine.best("ask", OTHER) == other_asks[0].price == 100.5
    assert engine.worst("ask", OTHER) == other_asks[-1].price == 103

    # Volume is the named instrument's alone: pooled, the bid side would
    # answer 19 at a price both books' bids are marketable against.
    assert engine.volume_at("bid", 98) == sum(entry.available for entry in bids) == 12
    assert (
        engine.volume_at("bid", 98, OTHER)
        == sum(entry.available for entry in other_bids)
        == 7
    )
