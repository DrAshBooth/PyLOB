"""The values the legacy oracle was checking, checked directly (lob-6oj).

`tests/test_differential.py` runs both engines over a seeded workload and
compares them. That is an excellent test and it is about to be deleted: it
needs the legacy SQL engine, and retiring it takes the oracle with it. The
2026-08 review measured what that costs by injecting 99 deliberate bugs --
five of them survived every test in the suite *except* the differential one,
and all five were in money-and-book code:

    a level's cached volume counting gross quantity rather than what is left
    of an order that already has fills             `PriceLevel.append`

    a cancel taking an order out of the queue and leaving its quantity in the
    cache                                          `PriceLevel.discard`

    a passive quantity change moving the cache the wrong way   `BookSide.resize`

    the last trade price recorded from the taker's limit instead of the price
    the trade actually happened at                 `OrderBook._execute`

    the tick read as a binary double rather than as the decimal the caller
    wrote, so the grid is 0.05000000000000000277 and not a twentieth
                                                   `_tick_decimal`

None of those five is exotic. They are ordinary bookkeeping, and the reason
the suite missed them is that ordinary bookkeeping is what a differential test
picks up for free and what a scenario test has to ask about deliberately.
`PriceLevel.volume` has four update sites and only `fill` had a durable test;
`last_price` was asserted only where the maker's limit and the taker's happen
to be equal; the grid was asserted only at prices where a double and a decimal
agree.

So this module asks directly. Each section names the value it is pinning:

    the cached level volume    every update site, against a count of the
                              orders actually resting
    the last trade price      an execution where maker and taker named
                              *different* limits, so the two candidate answers
                              are distinguishable
    the price grid            exact half-ticks and prices whose double is not
                              the decimal the caller wrote
    the commission formula    its one guard, which the engine's own call sites
                              cannot reach
    the arrival stamp         `priority` is never read by matching -- FIFO
                              comes from dict insertion order -- so it is
                              asserted as a value, which is what the spec
                              names it as
    what an operation refuses  the cancel and modify error paths, none of
                              which had a test
    the defaults and the compat surface   including `processOrder`, which had
                              no coverage on either branch

Everything here goes through the public API, and nothing here needs a second
engine to answer.
"""

from __future__ import annotations

import pytest
from PyLOB.engine import (
    InvalidOrder,
    OrderBook,
    Trader,
    UnknownOrder,
    commission_for,
    quantize_price,
)
from PyLOB.events import Accepted, Modified

INSTRUMENT = "FAKE"
CURRENCY = "USD"
TICK = 0.01


class ListSink:
    """Every event the engine emits, in order."""

    def __init__(self):
        self.events = []

    def consume(self, event):
        self.events.append(event)


def make_book(tick_size=TICK, sink=None, traders=(1, 2, 3), **schedule):
    """A configured book: one instrument, `traders`, self-matching off."""
    book = OrderBook(tick_size=tick_size, sink=sink)
    book.configure_instrument(INSTRUMENT, CURRENCY)
    for tid in traders:
        book.configure_trader(tid, str(tid), **schedule)
    return book


def resting_quantity(book, side):
    """What is actually resting on `side`, counted order by order.

    The check `PriceLevel.volume` is a cache *of*: the snapshot lists every
    resting order exactly once (`book-queries`), so summing its `remaining` is
    the answer the cache is supposed to be maintaining incrementally.
    """
    return sum(order.remaining for order in book.snapshot(INSTRUMENT, side))


def queried_volume(book, side):
    """`getVolumeAtPrice` at a price the whole of `side` is marketable against.

    The bid side answers for levels at or above the query price and the ask
    side for levels at or below it, so the extremes ask about the whole side.
    """
    return book.getVolumeAtPrice(INSTRUMENT, side, 0.01 if side == "bid" else 100_000.0)


def assert_volume_cache_is_honest(book):
    """The cached volume agrees with a count of the orders resting on it."""
    for side in ("bid", "ask"):
        assert queried_volume(book, side) == resting_quantity(book, side), (
            "%s volume cache disagrees with the book: cache %r, orders %r"
            % (side, queried_volume(book, side), resting_quantity(book, side))
        )


# --------------------------------------------------------------------------
# the cached level volume
# --------------------------------------------------------------------------


def test_a_cancel_takes_its_quantity_out_of_the_level():
    """`PriceLevel.discard` maintains the cache; the order leaving is not enough.

    One of the five the oracle was carrying: a cancel that removed the order
    from the queue and left its quantity in the cached volume. Every book
    query that reads the cache -- which is all of them -- then reports depth
    that is not there.
    """
    book = make_book()
    keep, _ = book.submit(1, INSTRUMENT, "bid", "limit", 4, 100.0)
    doomed, _ = book.submit(2, INSTRUMENT, "bid", "limit", 7, 100.0)
    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 100.0) == 11

    book.cancelOrder("bid", doomed.idNum)

    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 100.0) == 4
    assert book.book(INSTRUMENT).bids.level_at(100.0).volume == 4
    assert_volume_cache_is_honest(book)

    # And the level's last order leaving takes the level with it.
    book.cancelOrder("bid", keep.idNum)
    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 100.0) == 0
    assert book.book(INSTRUMENT).bids.level_at(100.0) is None
    assert book.getBestBid(INSTRUMENT) is None


def test_a_passive_quantity_decrease_moves_the_volume_down():
    """`BookSide.resize` is the passive half of modify, and it owns the cache.

    A pure quantity decrease keeps its place in the queue (`order-lifecycle`),
    so it never goes through `remove`/`add` and the cache has to be adjusted in
    place -- with the sign of the change, which is the third of the five.
    """
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "ask", "limit", 10, 100.0)
    book.submit(2, INSTRUMENT, "ask", "limit", 2, 100.0)
    assert book.getVolumeAtPrice(INSTRUMENT, "ask", 100.0) == 12

    trades, _ = book.modifyOrder(order.idNum, dict(side="ask", qty=6, price=None))

    assert trades == []
    assert order.qty == 6 and order.priority == 1, "a decrease keeps its stamp"
    assert book.getVolumeAtPrice(INSTRUMENT, "ask", 100.0) == 8
    assert_volume_cache_is_honest(book)


def test_a_quantity_increase_moves_the_volume_up():
    """The other direction, which costs time priority and so re-queues."""
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "ask", "limit", 4, 100.0)
    behind, _ = book.submit(2, INSTRUMENT, "ask", "limit", 2, 100.0)

    book.modifyOrder(order.idNum, dict(side="ask", qty=9, price=None))

    assert book.getVolumeAtPrice(INSTRUMENT, "ask", 100.0) == 11
    assert [o.idNum for o in book.snapshot(INSTRUMENT, "ask")] == [
        behind.idNum,
        order.idNum,
    ], "an increase goes to the back of the queue"
    assert_volume_cache_is_honest(book)


def test_a_decrease_to_the_filled_quantity_empties_the_level():
    """The clamp case: nothing left to trade, so the order leaves the book."""
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "ask", "limit", 10, 100.0)
    book.submit(2, INSTRUMENT, "bid", "limit", 6, 100.0)
    assert order.fulfilled == 6
    assert book.getVolumeAtPrice(INSTRUMENT, "ask", 100.0) == 4

    book.modifyOrder(order.idNum, dict(side="ask", qty=6, price=None))

    assert order.filled and not order.resting
    assert book.getVolumeAtPrice(INSTRUMENT, "ask", 100.0) == 0
    assert book.book(INSTRUMENT).asks.level_at(100.0) is None


def test_a_partly_filled_remainder_rests_at_what_is_left_of_it():
    """`PriceLevel.append` counts `remaining`, not `qty` -- the first of the five.

    An order that crosses, takes what is there and rests the rest arrives at
    its level with fills already against it. Counting its gross quantity
    inflates the level by exactly the amount it just traded away, which is the
    one case a book of untouched orders never produces.
    """
    book = make_book()
    book.submit(1, INSTRUMENT, "ask", "limit", 3, 100.0)

    taker, trades = book.submit(2, INSTRUMENT, "bid", "limit", 8, 100.0)

    assert len(trades) == 1 and taker.fulfilled == 3 and taker.resting
    assert taker.qty == 8 and taker.remaining == 5
    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 100.0) == 5
    assert book.book(INSTRUMENT).bids.level_at(100.0).volume == 5
    assert_volume_cache_is_honest(book)


def test_the_cache_survives_a_mixed_sequence_of_every_update():
    """Fill, append, discard and resize interleaved, checked after each one.

    The four update sites in one sequence, against the count of what is
    actually resting -- the invariant the differential harness was checking
    incidentally on every operation of every workload.
    """
    book = make_book()
    orders = []
    for price, qty, side, tid in (
        (100.0, 5, "bid", 1),
        (100.0, 3, "bid", 2),
        (99.5, 7, "bid", 1),
        (101.0, 4, "ask", 2),
        (101.5, 6, "ask", 3),
    ):
        order, _ = book.submit(tid, INSTRUMENT, side, "limit", qty, price)
        orders.append(order)
        assert_volume_cache_is_honest(book)

    book.submit(3, INSTRUMENT, "ask", "limit", 6, 100.0)  # fills, then rests
    assert_volume_cache_is_honest(book)
    book.cancelOrder("bid", orders[2].idNum)
    assert_volume_cache_is_honest(book)
    book.modifyOrder(orders[3].idNum, dict(side="ask", qty=2, price=None))
    assert_volume_cache_is_honest(book)
    book.modifyOrder(orders[4].idNum, dict(side="ask", qty=9, price=None))
    assert_volume_cache_is_honest(book)
    book.modifyOrder(orders[4].idNum, dict(side="ask", qty=9, price=99.0))
    assert_volume_cache_is_honest(book)
    book.submit(1, INSTRUMENT, "bid", "market", 4)
    assert_volume_cache_is_honest(book)

    assert resting_quantity(book, "bid") + resting_quantity(book, "ask") > 0


def test_the_volume_query_snaps_its_price_to_the_grid():
    """`getVolumeAtPrice` asks about a grid point, because a book has no others.

    A query price off the tick is not a price this book can hold, so the
    question it names is the one about the grid point it rounds to -- and on
    the bid side rounding down instead of to nearest would exclude the level
    the caller was asking about.
    """
    book = make_book()
    book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)

    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 100.0) == 5
    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 100.001) == 5
    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 99.999) == 5
    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 100.009) == 0


# --------------------------------------------------------------------------
# the last trade price
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "taker_side, maker_price, taker_price",
    [("bid", 100.0, 105.0), ("ask", 100.0, 95.0)],
)
def test_the_last_price_is_the_price_the_trade_happened_at(
    taker_side, maker_price, taker_price
):
    """`book-queries`: the price of the most recent *trade*, which is the maker's.

    The fourth of the five, and the reason it survived everything else: almost
    every test in the suite crosses at a single price, where the maker's limit
    and the taker's are the same number and the two possible answers are
    indistinguishable. Here they are ten ticks apart.
    """
    maker_side = "ask" if taker_side == "bid" else "bid"
    book = make_book()
    book.submit(1, INSTRUMENT, maker_side, "limit", 5, maker_price)

    _, trades = book.submit(2, INSTRUMENT, taker_side, "limit", 5, taker_price)

    assert [trade.price for trade in trades] == [maker_price]
    assert book.getLastPrice(INSTRUMENT) == maker_price


def test_the_last_price_is_the_last_makers_price_when_a_walk_sweeps_levels():
    """A taker crossing several levels leaves the price of its final execution."""
    book = make_book()
    for price in (100.0, 100.5, 101.0):
        book.submit(1, INSTRUMENT, "ask", "limit", 2, price)

    _, trades = book.submit(2, INSTRUMENT, "bid", "limit", 5, 102.0)

    assert [trade.price for trade in trades] == [100.0, 100.5, 101.0]
    assert book.getLastPrice(INSTRUMENT) == 101.0


def test_a_market_order_leaves_the_makers_price_behind_it():
    """The market order has no price of its own, so only the maker's can be it."""
    book = make_book()
    book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.25)
    book.submit(2, INSTRUMENT, "bid", "market", 5)
    assert book.getLastPrice(INSTRUMENT) == 100.25


# --------------------------------------------------------------------------
# the price grid
# --------------------------------------------------------------------------


def test_an_exact_half_tick_rounds_to_even():
    """The grid introduces no directional bias, as Python's own `round` does not.

    Excluded by generator constraint from the differential workloads -- half
    ticks are exactly the inputs a random generator is told to avoid, because
    two engines rounding differently is noise there. It is not noise here: a
    grid that rounds every half tick up walks a whole book's prices upward.
    """
    assert quantize_price(100.00005, 0.0001) == 100.0
    assert quantize_price(100.00015, 0.0001) == 100.0002
    assert quantize_price(2.665, 0.01) == 2.66
    assert quantize_price(2.675, 0.01) == 2.68
    assert quantize_price(0.75, 0.5) == 1.0
    assert quantize_price(1.25, 0.5) == 1.0


def test_the_grid_is_the_decimal_the_caller_wrote_on_both_sides():
    """Price and tick are both read through `str`, and both have to be.

    `Decimal(0.05)` is 0.05000000000000000277, the double; `Decimal("0.05")` is
    a twentieth. The same is true of the price, and the difference shows only
    where the double falls on the far side of a grid boundary from the decimal
    -- which is the fifth of the five, and which no test asked about because
    every price in the suite was already on the grid.
    """
    # The tick: 0.3 as a double is a hair under three tenths, which puts
    # 0.75 / 0.3 a hair *over* the exact 2.5 and rounds it up to 0.9.
    assert quantize_price(0.75, 0.3) == 0.6
    assert quantize_price(1.35, 0.3) == 1.2

    # The price: 2.665 as a double is 2.66500000000000003552..., which is over
    # the half tick rather than exactly on it, and rounds to 2.67.
    assert quantize_price(2.665, 0.01) == 2.66
    assert quantize_price(100.00005, 0.0001) == 100.0

    # And the same grid through the engine, which is where it matters.
    book = make_book(tick_size=0.01)
    order, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 2.665)
    assert order.price == 2.66
    assert book.getBestBid(INSTRUMENT) == 2.66
    assert book.clipPrice(2.665) == 2.66


def test_the_grid_is_a_spacing_and_not_a_count_of_decimal_places():
    """0.05, 0.25, 1 and 5 are four different grids, not one two-digit one.

    The legacy engine computed `round(price, log10(1/tick))`, which cannot
    tell them apart; `order-lifecycle` requires the nearest multiple of the
    tick for any positive tick.
    """
    assert quantize_price(100.03, 0.05) == 100.05
    assert quantize_price(100.03, 0.25) == 100.0
    assert quantize_price(100.03, 1) == 100.0
    assert quantize_price(102.5, 5) == 100.0
    assert quantize_price(103.0, 5) == 105.0


# --------------------------------------------------------------------------
# the commission formula
# --------------------------------------------------------------------------


def test_an_order_with_no_fills_owes_nothing_whatever_the_floor_says():
    """`commission_for`'s zero-quantity guard, which is public and unreachable.

    Inside the engine the guard cannot fire: `_charge` runs from `_execute`,
    after a fill, so the cumulative quantity it passes is at least one -- and
    at a cumulative quantity of zero the cumulative value is zero too, which
    collapses the percentage cap and hides the guard behind it. `commission_for`
    is exported, though, and it is exported as *the* one implementation of the
    `commissions` formula for anyone reproducing a charge, so the claim its
    docstring makes -- an order with no fills owes nothing, whatever the floor
    says -- is a claim about its own arguments and not about how the engine
    happens to call it.
    """
    schedule = Trader(
        tid=1, commission_min=2.5, commission_max_percnt=1.0, commission_per_unit=0.01
    )

    assert commission_for(schedule, 0, 0.0) == 0.0
    assert commission_for(schedule, 0, 500.0) == 0.0
    assert commission_for(schedule, -1, 500.0) == 0.0
    # One fill in, and the floor is exactly what it charges.
    assert commission_for(schedule, 1, 500.0) == 2.5


# --------------------------------------------------------------------------
# the arrival stamp
# --------------------------------------------------------------------------


def test_priority_is_a_strictly_increasing_arrival_stamp():
    """`priority` is a value, not merely an ordering that happens to hold.

    Matching's FIFO comes from the level dict's insertion order, so nothing in
    the engine reads this field: stamping every order the same number leaves
    the whole suite green while the spec's named tie-break -- the arrival
    sequence number -- becomes a constant. Asserting the ordering is not
    enough, because a constant is non-decreasing; it has to be asserted
    strictly, and across both sides and both instruments, since one counter
    serves all of them.
    """
    book = make_book()
    stamps = []
    for tid, side, price in (
        (1, "bid", 99.0),
        (2, "ask", 101.0),
        (3, "bid", 98.0),
        (1, "ask", 102.0),
        (2, "bid", 97.0),
    ):
        order, _ = book.submit(tid, INSTRUMENT, side, "limit", 1, price)
        stamps.append(order.priority)

    assert stamps == sorted(set(stamps)), stamps
    assert all(b > a for a, b in zip(stamps, stamps[1:])), stamps
    assert stamps == list(range(stamps[0], stamps[0] + len(stamps)))


def test_a_repricing_modify_takes_a_fresh_stamp_and_a_passive_one_does_not():
    """`order-lifecycle`: a price change or an increase costs time priority.

    Asserted as the stamp rather than as the queue position, because the queue
    position is maintained by `remove`/`add` and would hold even if the stamp
    were never written.
    """
    book = make_book()
    first, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 99.0)
    second, _ = book.submit(2, INSTRUMENT, "bid", "limit", 5, 99.0)
    assert second.priority > first.priority

    book.modifyOrder(first.idNum, dict(side="bid", qty=None, price=98.0))
    assert first.priority > second.priority

    kept = first.priority
    book.modifyOrder(first.idNum, dict(side="bid", qty=2, price=None))
    assert first.priority == kept, "a pure decrease keeps its place and its stamp"


def test_the_recorded_accepted_carries_the_orders_own_stamp():
    """The stream records `priority`, so the number in it has to be the number.

    A replayer re-submits from `Accepted` and the engine re-stamps from its own
    counter; if the recorded value were a constant, nothing downstream would
    notice until two orders in a replayed book had to be ordered against each
    other.
    """
    sink = ListSink()
    book = make_book(sink=sink)
    orders = [
        book.submit(1, INSTRUMENT, "bid", "limit", 1, 99.0 - i)[0] for i in range(4)
    ]

    accepted = [event for event in sink.events if isinstance(event, Accepted)]
    assert len(accepted) == 4
    assert [event.priority for event in accepted] == [o.priority for o in orders]
    stamps = [event.priority for event in accepted]
    assert all(b > a for a, b in zip(stamps, stamps[1:])), stamps

    # A modify re-stamps, and the `Modified` carries the new stamp.
    book.modifyOrder(orders[0].idNum, dict(side="bid", qty=None, price=95.0))
    modified = [event for event in sink.events if isinstance(event, Modified)]
    assert len(modified) == 1
    assert modified[0].reprioritized
    assert modified[0].priority == orders[0].priority > stamps[-1]


# --------------------------------------------------------------------------
# what an operation refuses
# --------------------------------------------------------------------------


def test_cancel_refuses_a_side_that_is_not_the_orders():
    """`order-lifecycle`: every way of naming an order that does not exist raises.

    A cancel that accepts the wrong side is a cancel that succeeded against an
    order the caller did not mean, which is how lob-0rb's silent no-op lost
    orders in the other direction.
    """
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)

    with pytest.raises(InvalidOrder):
        book.cancelOrder("ask", order.idNum)

    assert order.resting and not order.cancelled
    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 100.0) == 5
    # Naming it by identifier alone is still allowed, and still works.
    book.cancelOrder(None, order.idNum)
    assert order.cancelled


def test_cancel_refuses_an_order_that_is_already_cancelled():
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)
    book.cancelOrder("bid", order.idNum)

    with pytest.raises(InvalidOrder):
        book.cancelOrder("bid", order.idNum)


def test_cancel_refuses_an_order_with_nothing_left_to_cancel():
    """A fully filled order is finished, and finishing it again is a mistake.

    `commissions` says cancelling does not refund, so a cancel here would move
    nothing and still tell the caller it had done something.
    """
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.0)
    book.submit(2, INSTRUMENT, "bid", "limit", 5, 100.0)
    assert order.fulfilled == 5 and order.filled and not order.resting

    with pytest.raises(InvalidOrder):
        book.cancelOrder("ask", order.idNum)

    assert not order.cancelled and order.cancel_reason is None


def test_an_order_is_filled_at_exactly_its_quantity():
    """`filled` is `fulfilled >= qty`; the boundary is the whole of the property.

    A strict `>` never becomes true at all, which leaves every completed order
    reading as live -- and the cancel refusal above reading as available.
    """
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.0)
    book.submit(2, INSTRUMENT, "bid", "limit", 3, 100.0)
    assert not order.filled and order.resting and order.remaining == 2

    book.submit(3, INSTRUMENT, "bid", "limit", 2, 100.0)
    assert order.fulfilled == order.qty == 5
    assert order.filled and not order.resting and order.remaining == 0


def test_a_modification_missing_one_of_its_fields_is_refused():
    """`None` means "leave this alone"; absence is a malformed update (lob-crf).

    Defaulting a missing key to `None` makes the two indistinguishable, so a
    caller who mistyped `qty` silently gets the order they did not ask for.
    """
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)

    for update in (
        dict(qty=3, price=None),
        dict(side="bid", price=None),
        dict(side="bid", qty=3),
        {},
    ):
        with pytest.raises(InvalidOrder):
            book.modifyOrder(order.idNum, update)

    assert order.qty == 5 and order.price == 100.0 and order.resting


def test_a_cancelled_order_matches_nothing_if_it_is_handed_back_to_match():
    """`match` refuses to trade for an order that has left.

    Reachable because the decomposition `submit` is built from is public: an
    order accepted, cancelled, and then passed to `match` has no claim on the
    book, and trading it would move four balances for an order the stream
    records as gone.
    """
    book = make_book()
    maker, _ = book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.0)
    taker = book.create_order(2, INSTRUMENT, "bid", "limit", 5, 101.0)
    book.cancelOrder("bid", taker.idNum)

    assert book.match(taker) == []
    assert maker.fulfilled == 0 and maker.resting
    assert book.balance(1, CURRENCY) == 0.0 and book.balance(2, CURRENCY) == 0.0


def test_match_and_rest_still_refuse_an_order_from_another_engine():
    """The guard `match` and `rest` share, from the other side of the boundary."""
    mine = make_book()
    theirs = make_book()
    stranger, _ = theirs.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)

    with pytest.raises(UnknownOrder):
        mine.match(stranger)
    with pytest.raises(UnknownOrder):
        mine.rest(stranger)


# --------------------------------------------------------------------------
# the defaults and the compat surface
# --------------------------------------------------------------------------


def test_an_unconfigured_trader_neither_self_matches_nor_owes_commission():
    """The default schedule is zero and the default gate is on.

    `trader()` invents a `Trader` for an identifier nobody configured, and
    what it invents is policy: a default that allowed self-matching would let
    an unconfigured trader trade with itself -- the one thing
    `trader-balances` gates -- and a default floor would charge a commission
    the host never set.
    """
    book = OrderBook(tick_size=TICK)
    book.configure_instrument(INSTRUMENT, CURRENCY)

    mine, _ = book.submit(9, INSTRUMENT, "ask", "limit", 5, 100.0)
    own, trades = book.submit(9, INSTRUMENT, "bid", "limit", 5, 100.0)
    assert trades == [], "an unconfigured trader must not match itself"
    assert mine.fulfilled == 0 and mine.resting and own.resting

    other, trades = book.submit(8, INSTRUMENT, "bid", "limit", 5, 100.0)
    assert len(trades) == 1
    assert mine.commission == 0.0 and other.commission == 0.0
    assert book.trader(9).allow_self_matching is False
    assert book.trader(9).commission_min == 0.0


def test_an_unconfigured_instrument_moves_its_own_leg_and_invents_no_currency():
    """`_settle` books what it knows and leaves the rest recoverable from the fill.

    Inventing a currency here would put a cash leg in the ledger that no
    `InstrumentConfigured` event describes, so a replay would not reproduce it.
    """
    book = OrderBook(tick_size=TICK)
    book.configure_trader(1, "1", commission_min=2.0)
    book.configure_trader(2, "2", commission_min=2.0)

    book.submit(1, "NAKED", "ask", "limit", 5, 100.0)
    _, trades = book.submit(2, "NAKED", "bid", "limit", 5, 100.0)

    assert len(trades) == 1
    assert book.book("NAKED").currency is None
    assert sorted(book.holdings()) == [(1, "NAKED", -5.0), (2, "NAKED", 5.0)]


def test_a_market_order_is_not_resting_even_before_it_is_cancelled():
    """`resting` is derived, so it cannot be caught mid-processing saying otherwise.

    `submit` cancels a market remainder immediately, which hides the property
    behind the cancellation flag; accepting one without matching it is the
    only way to observe the derivation on its own.
    """
    book = make_book()
    order = book.create_order(1, INSTRUMENT, "bid", "market", 5)

    assert order.resting is False
    assert not order.cancelled and order.fulfilled == 0
    with pytest.raises(InvalidOrder):
        book.rest(order)


def test_process_order_writes_back_what_the_engine_assigned():
    """The legacy dict-quote shape: callers read the identity back out of the quote.

    Untested on either branch before now. What the quote comes back holding is
    the entire compat contract -- the identifier, the engine's timestamp, and
    the *quantized* working price rather than the one that was asked for.
    """
    book = make_book()
    quote = dict(
        tid=1, instrument=INSTRUMENT, side="bid", type="limit", qty=5, price=100.004
    )

    trades, returned = book.processOrder(quote)

    assert returned is quote
    assert trades == []
    order = book.order(quote["idNum"])
    assert order is not None and order.tid == 1 and order.qty == 5
    assert quote["price"] == 100.0 == order.price
    assert quote["timestamp"] == order.timestamp == book.time
    assert order.resting and book.getBestBid(INSTRUMENT) == 100.0


def test_process_order_from_data_uses_the_quotes_own_identity():
    """`fromData` is the replay path: the quote's `idNum` and `timestamp` are used.

    Ignoring the flag makes a replayed session re-number every order and
    re-stamp every clock reading, which is precisely the divergence
    `_assign_idNum`'s high-water mark exists to prevent.
    """
    book = make_book()
    quote = dict(
        tid=2,
        instrument=INSTRUMENT,
        side="ask",
        type="limit",
        qty=3,
        price=101.0,
        idNum=77,
        timestamp=12.5,
    )

    book.processOrder(quote, fromData=True)

    assert quote["idNum"] == 77 and quote["timestamp"] == 12.5
    order = book.order(77)
    assert order is not None and order.timestamp == 12.5 and order.qty == 3
    assert book.time == 12.5

    # The high-water mark moved with it, so nothing assigned later collides.
    later, _ = book.submit(1, INSTRUMENT, "bid", "limit", 1, 99.0)
    assert later.idNum > 77


def test_process_order_reports_the_trades_it_caused():
    """The other branch of the same call: a crossing quote comes back with fills."""
    book = make_book()
    book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.0)

    trades, quote = book.processOrder(
        dict(tid=2, instrument=INSTRUMENT, side="bid", type="market", qty=4)
    )

    assert [(trade.price, trade.qty) for trade in trades] == [(100.0, 4)]
    assert quote["price"] is None, "a market order has no working price"
    assert book.getVolumeAtPrice(INSTRUMENT, "ask", 100.0) == 1


def test_print_lists_both_sides_under_their_own_headings():
    """Eyeball output, and the only assertion it is worth is that it is complete.

    Nothing parses this, so the format is not pinned here beyond what
    `OrderBook.print`'s docstring promises: three sections in order, and each
    side's resting orders under its own heading. Dropping a section is a
    silent halving of a debugging tool.
    """
    book = make_book()
    bid, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 99.0)
    ask, _ = book.submit(2, INSTRUMENT, "ask", "limit", 4, 101.0)

    text = book.print(INSTRUMENT)

    bids_at = text.index("Bids")
    asks_at = text.index("Asks")
    trades_at = text.index("Trades")
    assert bids_at < asks_at < trades_at
    assert "%d)" % bid.idNum in text[bids_at:asks_at]
    assert "%d)" % ask.idNum in text[asks_at:trades_at]
    assert "last price" in text[trades_at:]
