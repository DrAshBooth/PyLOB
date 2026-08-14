"""The in-memory engine at its boundaries: bad input, gated walks, heap hygiene.

Three P1 findings from the pre-retirement review (`docs/engine-review-2026-08.md`),
one section each. They are together in one module because they are one kind of
test -- none of them is a scenario from a frozen spec, and none of them would
be caught by asking the engine to trade correctly. They are the cases where an
engine that trades correctly still ends up wrong.

`lob-d6i` -- the input gate
    A limit price of `float("nan")` was accepted. Every comparison against NaN
    is false, so the price heap could neither float it to the top nor evict it,
    and a marketable ask walked straight past a better resting bid: the book
    ended **crossed between two different traders**, permanently, silently, and
    reproduced faithfully on replay. The same boundary leaked
    `decimal.InvalidOperation` for `inf` and for very large prices, raised
    `OverflowError` from the middle of an execution for a very large quantity
    (after both fills were committed and before either was settled), credited
    commission on negative prices, and ignored a price supplied with a market
    order. Every one of those now raises `InvalidOrder` before the clock moves.

`lob-rp4` -- the cost of stepping over your own order
    A taker whose own resting orders are at the front of a level steps over
    them. That step used to re-read the level's queue from the front, making
    the walk O(k^2): the single-agent gym ADR-0002 names as the target workload
    ran at 10 orders/sec with 3,200 of its own orders resting. The tests here
    pin the *shape* of the cost, not a wall-clock number, by counting the queue
    steps the walk takes -- a contended machine changes the seconds and not the
    step count.

`lob-n3n` -- the heaps
    `_compact` gated on `_best`, which matching keeps short, so the gate never
    opened and `_worst` grew one entry per level creation for the life of the
    process (499,000 entries and a 234ms first query, measured at 1M
    operations). And a price can be in a heap twice, which let one match walk
    receive the same level twice with its skip cursor reset.

The engine is exercised through its public surface wherever the finding can be
seen there. It cannot always be: a heap's length is the subject of `lob-n3n`,
so those tests read `BookSide._best` and `._worst` directly and say so.
"""

import math
from contextlib import closing, contextmanager

import pytest
from PyLOB.engine import (
    MAX_QTY,
    InvalidOrder,
    OrderBook,
    PriceLevel,
    PyLOBError,
)

INSTRUMENT = "FAKE"
CURRENCY = "USD"
TICK = 0.01


class ListSink:
    """Every event the engine emits, in order.

    The engine gates event *construction* on a sink being attached (ADR-0002),
    so "nothing was emitted" is only a meaningful assertion with one attached.
    """

    def __init__(self):
        self.events = []

    def consume(self, event):
        self.events.append(event)


def make_book(tick_size=TICK, sink=None, traders=(1, 2, 3), self_matching=()):
    """A configured book: one instrument, `traders`, self-matching off by default."""
    book = OrderBook(tick_size=tick_size, sink=sink)
    book.configure_instrument(INSTRUMENT, CURRENCY)
    for tid in traders:
        book.configure_trader(tid, str(tid), allow_self_matching=tid in self_matching)
    return book


def assert_not_crossed(book):
    """The best bid is below the best ask, or one side is empty.

    `book-queries` never states this outright because a matching engine cannot
    normally produce a crossed book: anything marketable trades on arrival. It
    is the invariant lob-d6i broke.
    """
    bid = book.getBestBid(INSTRUMENT)
    ask = book.getBestAsk(INSTRUMENT)
    assert bid is None or ask is None or bid < ask, (
        "book is crossed: best bid %r >= best ask %r" % (bid, ask)
    )


@contextmanager
def counted_queue_steps():
    """Count the orders a match walk steps over, as a list of one int.

    `PriceLevel.__iter__` is the queue walk: `match` advances a cursor over it
    and every step through a level's FIFO goes through it. Counting steps is
    the machine-independent form of the O(k^2) measurement -- the review's
    numbers were seconds on a contended machine, and this is the same quantity
    with the machine taken out.
    """
    steps = [0]
    original = PriceLevel.__iter__

    def counting(self):
        for order in original(self):
            steps[0] += 1
            yield order

    PriceLevel.__iter__ = counting
    try:
        yield steps
    finally:
        PriceLevel.__iter__ = original


# --------------------------------------------------------------------------
# lob-d6i: the input gate
# --------------------------------------------------------------------------


def test_a_nan_bid_cannot_bury_the_book_and_leave_it_crossed():
    """lob-d6i's headline, verbatim: bids 104 / nan / 95 / 106, then an ask at 105.

    Before the gate: the NaN order rested, `getBestBid` reported 104.0 with
    106.0 resting underneath it, and the ask at 105 traded nothing and rested
    -- a book crossed between two different traders, which no later operation
    could unwind because the NaN entry could never reach the top of the heap
    to be evicted.
    """
    book = make_book()
    book.submit(1, INSTRUMENT, "bid", "limit", 5, 104.0)
    with pytest.raises(InvalidOrder):
        book.submit(1, INSTRUMENT, "bid", "limit", 5, float("nan"))
    book.submit(1, INSTRUMENT, "bid", "limit", 5, 95.0)
    book.submit(1, INSTRUMENT, "bid", "limit", 5, 106.0)

    assert book.getBestBid(INSTRUMENT) == 106.0
    assert book.getWorstBid(INSTRUMENT) == 95.0
    assert [order.price for order in book.snapshot(INSTRUMENT, "bid")] == [
        106.0,
        104.0,
        95.0,
    ]

    order, trades = book.submit(2, INSTRUMENT, "ask", "limit", 5, 105.0)
    assert [(trade.price, trade.qty) for trade in trades] == [(106.0, 5)]
    assert order.filled and not order.resting
    assert book.getBestBid(INSTRUMENT) == 104.0
    assert book.getBestAsk(INSTRUMENT) is None
    assert_not_crossed(book)


@pytest.mark.parametrize(
    "price",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        0,
        0.0,
        -0.01,
        -100.0,
        True,
        False,
        "100.0",
        b"100.0",
        (100.0,),
        10**400,
    ],
    ids=repr,
)
def test_a_limit_order_with_an_unusable_price_is_refused(price):
    """Non-finite, non-positive, non-numeric, and `bool` all raise `InvalidOrder`.

    `True` is an `int` in Python, so it passes every numeric test that is not
    written to exclude it; `10**400` is a real number that no float can hold.
    """
    book = make_book()
    with pytest.raises(InvalidOrder):
        book.submit(1, INSTRUMENT, "bid", "limit", 5, price)


def test_the_price_gate_raises_the_librarys_own_exception():
    """`order-lifecycle`: an invalid submission raises *a library exception*.

    `decimal.InvalidOperation` is not one, and a caller catching `PyLOBError`
    around a submission was not catching it (lob-d6i).
    """
    book = make_book()
    for price in (float("inf"), "abc", 1e40, -1.0):
        with pytest.raises(PyLOBError):
            book.submit(1, INSTRUMENT, "bid", "limit", 5, price)
        with pytest.raises(ValueError):
            # `InvalidOrder` is a `ValueError` too, for callers who never read
            # these docs and catch what bad input usually raises.
            book.submit(1, INSTRUMENT, "bid", "limit", 5, price)


def test_a_price_under_half_a_tick_is_refused():
    """A price that quantizes to zero is not a price this book can hold.

    Zero is refused as a submitted price; it has to be refused as a *working*
    price too, or the same order arrives at the same place by a different road.
    """
    book = make_book(tick_size=1.0)
    with pytest.raises(InvalidOrder):
        book.submit(1, INSTRUMENT, "bid", "limit", 5, 0.4)
    assert book.getBestBid(INSTRUMENT) is None


def test_a_refused_submission_moves_nothing_at_all():
    """No state change, no event, no counter -- the gate runs before all of it.

    The counters are read through the next accepted order rather than off the
    engine: an identifier, a priority stamp or a clock tick consumed by a
    refused submission would show up there, which is the only place it could
    ever matter.
    """
    sink = ListSink()
    book = make_book(sink=sink)
    first, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)
    before_events = list(sink.events)
    before_book = book.snapshot(INSTRUMENT, "bid")
    before_orders = list(book.orders())
    before_time = book.time

    for price in (float("nan"), float("inf"), -1.0, 0.0, "abc", True, 1e40):
        with pytest.raises(InvalidOrder):
            book.submit(2, INSTRUMENT, "ask", "limit", 3, price)
    for qty in (0, -1, 1.5, True, 10**400, MAX_QTY + 1):
        with pytest.raises(InvalidOrder):
            book.submit(2, INSTRUMENT, "ask", "limit", qty, 100.0)
    with pytest.raises(InvalidOrder):
        book.submit(2, INSTRUMENT, "ask", "market", 3, 100.0)

    assert sink.events == before_events
    assert book.snapshot(INSTRUMENT, "bid") == before_book
    assert list(book.orders()) == before_orders
    assert book.time == before_time
    assert book.balance(2, CURRENCY) == 0.0

    second, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)
    assert second.idNum == first.idNum + 1
    assert second.priority == first.priority + 1
    assert second.timestamp == first.timestamp + 1


@pytest.mark.parametrize(
    "qty",
    [0, -1, 1.5, True, False, "5", 10**400, MAX_QTY + 1],
    ids=repr,
)
def test_an_unusable_quantity_is_refused(qty):
    book = make_book()
    with pytest.raises(InvalidOrder):
        book.submit(1, INSTRUMENT, "bid", "limit", qty, 100.0)


def test_the_quantity_ceiling_is_the_last_exact_integer():
    """`MAX_QTY` is 2**53: accepted, and one more is not.

    Past 2**53 a float can no longer hold every integer, so an order's `value`
    and its trader's balance stop being able to represent what they are told.
    """
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "bid", "limit", MAX_QTY, 100.0)
    assert order.qty == MAX_QTY and order.resting
    with pytest.raises(InvalidOrder):
        book.submit(1, INSTRUMENT, "bid", "limit", MAX_QTY + 1, 100.0)


def test_a_quantity_that_overflows_the_arithmetic_never_reaches_a_fill():
    """lob-d6i: `qty=10**400` raised `OverflowError` *after* both fills committed.

    The maker was left with `fulfilled > 0`, no balance had moved, and the sink
    had seen an `Accepted` with no `Filled` behind it -- a stream that replays
    into a different book.
    """
    sink = ListSink()
    book = make_book(sink=sink)
    maker, _ = book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.0)
    before = list(sink.events)

    with pytest.raises(InvalidOrder):
        book.submit(2, INSTRUMENT, "bid", "limit", 10**400, 100.0)

    assert maker.fulfilled == 0 and maker.resting
    assert book.book(INSTRUMENT).asks.level_at(100.0).volume == 5
    assert book.balance(1, CURRENCY) == 0.0 and book.balance(2, CURRENCY) == 0.0
    assert book.balance(1, INSTRUMENT) == 0.0 and book.balance(2, INSTRUMENT) == 0.0
    assert sink.events == before


def test_a_notional_that_overflows_to_infinity_is_refused():
    """The silent half of the same finding: `qty * price` reaching `inf`.

    A legal quantity and a legal price whose product is not: the ledger goes to
    +/-inf and the percentage commission cap stops capping, since `min(inf, x)`
    is `x`. Nothing raises on its own -- floats overflow quietly -- so the gate
    has to ask.

    The tick is enormous because that is the only way to get a price this large
    onto a grid at all; on an ordinary tick the price/tick ratio is refused
    first.
    """
    book = make_book(tick_size=1e270)
    with pytest.raises(InvalidOrder):
        book.submit(1, INSTRUMENT, "bid", "limit", MAX_QTY, 1e300)
    assert book.getBestBid(INSTRUMENT) is None


def test_a_market_order_may_not_carry_a_price():
    """Silently ignoring it is how a caller believes a market order was capped.

    The same reasoning that made `modifyOrder(price=None)` mean "leave the
    price alone" rather than "become a market order" (lob-crf).
    """
    book = make_book()
    book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.0)
    with pytest.raises(InvalidOrder):
        book.submit(2, INSTRUMENT, "bid", "market", 5, 100.0)
    with pytest.raises(InvalidOrder):
        book.submit(2, INSTRUMENT, "bid", "market", 5, float("nan"))

    order, trades = book.submit(2, INSTRUMENT, "bid", "market", 5)
    assert [(trade.price, trade.qty) for trade in trades] == [(100.0, 5)]
    assert order.price is None


@pytest.mark.parametrize(
    "tick",
    [
        "0.05",
        True,
        False,
        None,
        float("nan"),
        float("inf"),
        0,
        0.0,
        -0.01,
        [0.01],
    ],
    ids=repr,
)
def test_an_unusable_tick_size_is_refused_at_construction(tick):
    """Including the unhashable one, which `lru_cache` used to turn into a `TypeError`."""
    with pytest.raises(InvalidOrder):
        OrderBook(tick_size=tick)


def test_a_tick_below_the_grids_precision_refuses_prices_with_a_library_error():
    """`OrderBook(tick_size=1e-40)` constructs and then quantizes nothing.

    The 40 significant digits `_CTX` carries apply to the price/tick *ratio*,
    so this tick is usable only for prices within forty digits of it -- which
    no ordinary price is. The engine documents the range and refuses the rest
    with its own exception instead of a `decimal` one.
    """
    book = OrderBook(tick_size=1e-40)
    book.configure_instrument(INSTRUMENT, CURRENCY)
    with pytest.raises(InvalidOrder):
        book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)


def test_a_price_beyond_the_grid_is_refused_at_the_ordinary_tick():
    """The same ratio, approached from the other end: 1e40 on a 0.0001 tick."""
    book = make_book(tick_size=0.0001)
    with pytest.raises(InvalidOrder):
        book.submit(1, INSTRUMENT, "bid", "limit", 5, 1e40)
    assert book.getBestBid(INSTRUMENT) is None


@pytest.mark.parametrize(
    "price",
    [float("nan"), float("inf"), -1.0, 0.0, "abc", True, 1e40],
    ids=repr,
)
def test_a_modify_to_an_unusable_price_is_refused_and_changes_nothing(price):
    """A modification is a submission at new terms and goes through one gate.

    A NaN arriving this way would bury the book exactly as one arriving on the
    submission path does.
    """
    sink = ListSink()
    book = make_book(sink=sink)
    order, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)
    before = list(sink.events)
    before_time = book.time

    with pytest.raises(InvalidOrder):
        book.modifyOrder(order.idNum, dict(side="bid", qty=None, price=price))

    assert order.price == 100.0 and order.qty == 5 and order.resting
    assert book.getBestBid(INSTRUMENT) == 100.0
    assert sink.events == before
    assert book.time == before_time


def test_a_modify_to_an_unusable_quantity_is_refused():
    book = make_book()
    order, _ = book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)
    for qty in (0, -1, 1.5, True, 10**400, MAX_QTY + 1):
        with pytest.raises(InvalidOrder):
            book.modifyOrder(order.idNum, dict(side="bid", qty=qty, price=None))
    assert order.qty == 5 and order.resting


def test_a_fill_is_settled_or_it_does_not_happen():
    """`_execute` computes its arithmetic before it commits either fill.

    The gate makes the overflow that exposed this unreachable, so what is left
    to assert is the conservation it protects: after a fill, both sides moved,
    and the two instrument legs and the two cash legs net out.
    """
    book = make_book()
    book.configure_trader(1, "1", commission_min=0.5, commission_max_percnt=1.0)
    book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.0)
    maker = book.snapshot(INSTRUMENT, "ask")[0]
    taker, trades = book.submit(2, INSTRUMENT, "bid", "market", 3)

    assert len(trades) == 1 and maker.fulfilled == 3 and taker.fulfilled == 3
    assert book.balance(1, INSTRUMENT) == -3.0
    assert book.balance(2, INSTRUMENT) == 3.0
    cash = book.balance(1, CURRENCY) + book.balance(2, CURRENCY)
    assert cash == pytest.approx(-(maker.commission + taker.commission))
    assert math.isfinite(cash)


# --------------------------------------------------------------------------
# lob-rp4: the cost of stepping over your own order
# --------------------------------------------------------------------------


def test_a_gated_order_does_not_shield_the_order_behind_it():
    """The taker's own order is stepped over; the next order in the queue fills.

    Mutation survivors M10/M47: a self-matching gate that stopped the walk
    instead of stepping past it, or that skipped the wrong order, is invisible
    to a suite that never puts a gated maker in front of a tradeable one.
    """
    book = make_book()
    mine, _ = book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.0)
    theirs, _ = book.submit(2, INSTRUMENT, "ask", "limit", 5, 100.0)

    taker, trades = book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)

    assert [(trade.price, trade.qty, trade.ask_idNum) for trade in trades] == [
        (100.0, 5, theirs.idNum)
    ]
    assert mine.fulfilled == 0 and mine.qty == 5 and mine.resting
    assert theirs.filled
    # It kept its place: still the front of the level, and still first out.
    assert [order.idNum for order in book.snapshot(INSTRUMENT, "ask")] == [mine.idNum]
    assert taker.filled


def test_a_gated_order_keeps_its_place_its_quantity_and_its_fills():
    """`trader-balances`: skipped, not consumed -- including a partial fill it already has."""
    book = make_book()
    mine, _ = book.submit(1, INSTRUMENT, "ask", "limit", 10, 100.0)
    book.submit(3, INSTRUMENT, "bid", "limit", 4, 100.0)
    assert mine.fulfilled == 4
    theirs, _ = book.submit(2, INSTRUMENT, "ask", "limit", 5, 100.0)

    _, trades = book.submit(1, INSTRUMENT, "bid", "limit", 5, 100.0)

    assert [trade.ask_idNum for trade in trades] == [theirs.idNum]
    assert mine.fulfilled == 4 and mine.qty == 10 and mine.remaining == 6
    assert [order.idNum for order in book.snapshot(INSTRUMENT, "ask")] == [mine.idNum]
    assert book.book(INSTRUMENT).asks.level_at(100.0).volume == 6


def test_the_cursor_resumes_where_it_was_when_a_fill_invalidates_it():
    """Two tradeable orders behind the gated one, taken by one taker in turn.

    A fill takes its maker out of the level's dict and so invalidates the
    cursor over it. The replacement is walked back over the skipped prefix --
    which cannot have moved, because a skipped order stays exactly where it is
    -- and landing one order either side of where the old cursor was is
    invisible unless the walk has to make *two* fills in one level. Every
    other gated test here fills once and stops, so the walk-back is taken and
    never checked: an overshoot silently steps over a maker the taker was
    entitled to, and an undershoot re-reads one it has already passed.
    """
    book = make_book()
    mine, _ = book.submit(1, INSTRUMENT, "ask", "limit", 5, 100.0)
    first, _ = book.submit(2, INSTRUMENT, "ask", "limit", 2, 100.0)
    second, _ = book.submit(3, INSTRUMENT, "ask", "limit", 2, 100.0)
    behind, _ = book.submit(2, INSTRUMENT, "ask", "limit", 4, 100.0)

    taker, trades = book.submit(1, INSTRUMENT, "bid", "limit", 6, 100.0)

    assert [(trade.ask_idNum, trade.qty) for trade in trades] == [
        (first.idNum, 2),
        (second.idNum, 2),
        (behind.idNum, 2),
    ]
    assert taker.filled
    assert mine.fulfilled == 0 and mine.resting
    assert [order.idNum for order in book.snapshot(INSTRUMENT, "ask")] == [
        mine.idNum,
        behind.idNum,
    ]
    assert book.book(INSTRUMENT).asks.level_at(100.0).volume == 7


def _gated_walk_steps(k):
    """Queue steps taken by one taker crossing k of its own orders to reach one more.

    The level is deliberately *not* single-owner -- trader 2's order sits at
    the back -- so this measures the cursor, not the whole-level short circuit.
    """
    book = make_book()
    for _ in range(k):
        book.submit(1, INSTRUMENT, "ask", "limit", 1, 100.0)
    book.submit(2, INSTRUMENT, "ask", "limit", 1, 100.0)
    with counted_queue_steps() as steps:
        _, trades = book.submit(1, INSTRUMENT, "bid", "limit", 1, 100.0)
    assert len(trades) == 1, "the taker must reach trader 2's order"
    return steps[0]


def test_the_gated_walk_costs_one_step_per_order_skipped():
    """Linear in k, asserted as a ratio of step counts rather than of seconds.

    Doubling k doubles the work of a linear walk and quadruples that of the
    quadratic one this replaces (measured at 4.1x per doubling: k=8000 took
    664ms, and one submission in a k=3200 gym took a tenth of a second). A
    contended machine changes neither number here, because neither is a clock.
    """
    small = _gated_walk_steps(200)
    large = _gated_walk_steps(400)

    # The walk steps over 200 orders and then fills against the 201st. The
    # fill invalidates the cursor, so the replacement is walked back over the
    # skipped prefix: 2k + O(1), and nothing that grows faster.
    assert small <= 3 * 200 + 32, small
    assert large <= 3 * 400 + 32, large
    assert large / small < 2.6, (small, large)


def test_a_level_that_is_wholly_the_takers_own_costs_one_comparison():
    """The single-agent case: quoting a level and then crossing it.

    ADR-0002's named workload. Every order at the touch belongs to the taker,
    so the gate's answer is the same for all of them and the level knows it in
    advance -- the walk must not pay per order to find that out, and it must
    still reach the level behind.
    """
    counts = {}
    for k in (100, 1600):
        book = make_book()
        for _ in range(k):
            book.submit(1, INSTRUMENT, "ask", "limit", 1, 100.00)
        behind, _ = book.submit(2, INSTRUMENT, "ask", "limit", 1, 100.01)
        with counted_queue_steps() as steps:
            _, trades = book.submit(1, INSTRUMENT, "bid", "limit", 1, 100.01)
        # The walk skipped a level of k orders and filled at the next one.
        assert [(t.price, t.ask_idNum) for t in trades] == [(100.01, behind.idNum)]
        assert len(book.snapshot(INSTRUMENT, "ask")) == k
        counts[k] = steps[0]

    assert counts[1600] == counts[100], counts
    assert counts[1600] <= 20, counts


def test_the_short_circuit_does_not_apply_to_a_trader_that_allows_self_matching():
    """`allow_self_matching` still trades against your own resting orders."""
    book = make_book(self_matching=(3,))
    mine, _ = book.submit(3, INSTRUMENT, "ask", "limit", 5, 100.0)
    _, trades = book.submit(3, INSTRUMENT, "bid", "limit", 5, 100.0)

    assert [(trade.price, trade.qty) for trade in trades] == [(100.0, 5)]
    assert mine.filled
    assert book.snapshot(INSTRUMENT, "ask") == ()


def test_a_level_that_gains_a_second_trader_is_walked_order_by_order():
    """The short circuit is a property of the level, maintained as it fills up."""
    book = make_book()
    book.submit(1, INSTRUMENT, "ask", "limit", 1, 100.0)
    theirs, _ = book.submit(2, INSTRUMENT, "ask", "limit", 1, 100.0)
    book.submit(1, INSTRUMENT, "ask", "limit", 1, 100.0)

    _, trades = book.submit(1, INSTRUMENT, "bid", "limit", 1, 100.0)

    assert [trade.ask_idNum for trade in trades] == [theirs.idNum]
    assert len(book.snapshot(INSTRUMENT, "ask")) == 2


# --------------------------------------------------------------------------
# lob-n3n: the heaps
# --------------------------------------------------------------------------


def test_the_worst_price_heap_is_bounded_under_churn():
    """20,000 create/consume cycles over one live level (the review's shape).

    Measured before the fix: `_worst` held 20,001 entries against a single live
    level, because `_compact`'s gate read `_best` -- which matching drains --
    and `_worst` is peeked only by `getWorst*`. Both heaps now answer to the
    same bound, and it is the bound `_compact`'s docstring claims.

    The level has to be emptied *by matching* rather than by cancellation, and
    that is the whole finding: matching pops the stale entry off `_best` on its
    way past, so the heap the gate was reading stayed short and the gate never
    opened. A cancel-only churn grows both heaps together and compacts itself.
    """
    book = make_book()
    book.submit(1, INSTRUMENT, "bid", "limit", 1, 99.0)  # the one live level

    for _ in range(20_000):
        book.submit(1, INSTRUMENT, "bid", "limit", 1, 100.0)
        book.submit(2, INSTRUMENT, "ask", "limit", 1, 100.0)

    bids = book.book(INSTRUMENT).bids
    live = len(bids)
    assert live == 1
    assert len(bids._worst) <= 2 * live + 16, len(bids._worst)
    assert len(bids._best) <= 2 * live + 16, len(bids._best)
    assert book.getWorstBid(INSTRUMENT) == 99.0
    assert book.getBestBid(INSTRUMENT) == 99.0


def test_a_level_is_handed_out_once_per_walk_despite_a_duplicated_price():
    """A price re-created before its stale entry surfaced is in the heap twice.

    Handing the level over twice in one walk restarts the caller's cursor into
    it from the front, which is how a market order came to yield 902 levels out
    of a book that had two. The walk drops the second copy, which is also how
    the heap sheds it.
    """
    book = make_book()
    book.submit(1, INSTRUMENT, "bid", "limit", 1, 100.0)  # the better price, on top
    stale, _ = book.submit(1, INSTRUMENT, "bid", "limit", 1, 99.0)
    book.cancelOrder("bid", stale.idNum)  # 99.0 is stale but not on top
    book.submit(1, INSTRUMENT, "bid", "limit", 1, 99.0)  # ... and now duplicated

    bids = book.book(INSTRUMENT).bids
    stored = 99.0 * bids._best_sign
    assert bids._best.count(stored) == 2, "the duplicate this test needs is not there"

    with closing(bids.match_levels(None)) as walk:
        handed_out = [level.price for level in walk]

    assert handed_out == [100.0, 99.0]
    assert bids._best.count(stored) == 1
    assert book.getBestBid(INSTRUMENT) == 100.0
    assert book.getWorstBid(INSTRUMENT) == 99.0


def test_a_compaction_mid_walk_does_not_re_yield_the_level_being_held():
    """The other way a price is duplicated, and the one the fixed gate opened.

    `_compact` fires from `_drop`, so a level emptying mid-walk rebuilds
    `_best` from the live levels -- re-adding the price of a level the walk
    has popped and is still holding, which then comes back to the top of the
    heap and is handed out a second time, with the caller's skip cursor into
    it reset to the front.

    That was previously unreachable, because the gate `_compact` read never
    opened; tightening it (the first half of lob-n3n) is what makes this walk
    have to survive on its own terms. The level kept alive here is the top of
    the book, emptied by nobody, while everything under it drains.
    """
    book = make_book()
    prices = [101.0] + [100.0 - i * TICK for i in range(100)]
    for price in prices:
        book.submit(1, INSTRUMENT, "bid", "limit", 1, price)
    bids = book.book(INSTRUMENT).bids
    assert len(bids._worst) == 101, len(bids._worst)

    handed_out = []
    with closing(bids.match_levels(None)) as walk:
        for level in walk:
            handed_out.append(level.price)
            if level.price != 101.0:
                # What a fill does to an exhausted level: drop it, which is
                # what calls `_compact`.
                for order in list(level):
                    bids.remove(order)

    assert handed_out == sorted(prices, reverse=True)
    assert len(handed_out) == len(set(handed_out)), "a level was handed out twice"
    assert len(bids) == 1
    assert book.getBestBid(INSTRUMENT) == 101.0
    assert book.getWorstBid(INSTRUMENT) == 101.0
    assert bids._best.count(101.0 * bids._best_sign) == 1


def test_a_sweep_that_compacts_mid_walk_leaves_the_book_intact():
    """The same thing through the front door: a market order across 92 levels.

    The taker is gated against the top of the book, so that level survives the
    whole walk while the levels under it drain and compaction fires underneath
    it. What must come out the other side is the ordinary contract -- the fills
    the taker was entitled to, the gated order untouched, and both heaps
    bounded and still answering correctly.
    """
    book = make_book()
    mine, _ = book.submit(1, INSTRUMENT, "bid", "limit", 1, 101.0)  # gated, survives
    for i in range(100):
        book.submit(2, INSTRUMENT, "bid", "limit", 1, 100.0 - i * TICK)

    taker, trades = book.submit(1, INSTRUMENT, "ask", "market", 92)

    assert len(trades) == 92 and taker.fulfilled == 92
    assert all(trade.bid_tid == 2 for trade in trades)
    assert [trade.price for trade in trades] == [100.0 - i * TICK for i in range(92)]
    assert mine.fulfilled == 0 and mine.resting

    bids = book.book(INSTRUMENT).bids
    assert len(bids) == 9  # the gated level, plus the 8 the taker did not reach
    assert len(bids._worst) <= 2 * len(bids) + 16, len(bids._worst)
    assert len(bids._best) <= 2 * len(bids) + 16, len(bids._best)
    assert book.getBestBid(INSTRUMENT) == 101.0
    assert book.getWorstBid(INSTRUMENT) == pytest.approx(100.0 - 99 * TICK)


def test_the_heaps_still_answer_correctly_after_heavy_churn():
    """Compaction is a rebuild of live state, so the queries it feeds must agree.

    Brute force against the level dict, which is the record compaction rebuilds
    from -- the check that the bound in `_compact` was tightened without
    tightening what it returns.
    """
    book = make_book()
    resting = []
    for i in range(400):
        price = 100.0 + (i % 40) * TICK
        order, _ = book.submit(1, INSTRUMENT, "bid", "limit", 1, price)
        resting.append(order)
        if i % 3 == 0 and resting:
            book.cancelOrder("bid", resting.pop(0).idNum)

    bids = book.book(INSTRUMENT).bids
    live = {order.price for order in resting}
    assert book.getBestBid(INSTRUMENT) == max(live)
    assert book.getWorstBid(INSTRUMENT) == min(live)
    assert {level.price for level in bids.levels()} == live
    assert len(bids._worst) <= 2 * len(live) + 16
    assert len(bids._best) <= 2 * len(live) + 16
