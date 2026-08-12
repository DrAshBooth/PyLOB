"""Order lifecycle tests: add, cross, partially fill, market, cancel, modify.

These are the safety net for the issue #8 fixes: everything here describes
behaviour the current engine already gets right, so the suite must stay green
before and after those fixes land.

Two invariants from the order-lifecycle contract are checked after *every*
operation, via `assert_invariants` (the operation wrappers below call it, so no
individual test has to remember to):

1. `0 <= fulfilled <= qty` for every row in `trade_order`.
2. A trade moves the same quantity on both of its sides. The `trade` table
   stores one `qty` per trade shared by the two orders, so the observable form
   is the accounting it feeds: each order's `fulfilled` equals the quantity of
   the trades it took part in, and the bid side's fulfilled total equals the
   ask side's.

The contract also describes an engine PyLOB is not yet -- market remainders
that cancel instead of resting, library exceptions on bad input, price changes
that surrender time priority. None of that is asserted here.
"""

INSTRUMENT = "FAKE"

# Orders whose fulfilment escaped [0, qty].
FULFILLED_BOUNDS = """
select order_id, idNum, side, qty, fulfilled
from trade_order
where fulfilled < 0 or fulfilled > qty
"""

# Each order's recorded fulfilment beside the quantity the trade log actually
# credits to it. `bid_order` and `ask_order` are summed separately so that an
# order appearing on both sides of a trade is counted once per side.
TRADE_CREDITS = """
select
    trade_order.order_id,
    trade_order.fulfilled,
    (select coalesce(sum(qty), 0) from trade where trade.bid_order = trade_order.order_id)
    + (select coalesce(sum(qty), 0) from trade where trade.ask_order = trade_order.order_id)
from trade_order
"""

SIDE_TOTALS = """
select side, coalesce(sum(fulfilled), 0)
from trade_order
group by side
"""


def assert_invariants(book):
    """Assert the two lifecycle invariants over the whole database."""
    crsr = book.db.cursor()

    unbounded = crsr.execute(FULFILLED_BOUNDS).fetchall()
    assert unbounded == [], "rows violating 0 <= fulfilled <= qty: %r" % (unbounded,)

    miscredited = [row for row in crsr.execute(TRADE_CREDITS) if row[1] != row[2]]
    assert miscredited == [], (
        "orders whose fulfilled disagrees with the trades they took part in "
        "(order_id, fulfilled, traded): %r" % (miscredited,)
    )

    totals = dict(crsr.execute(SIDE_TOTALS).fetchall())
    assert totals.get("bid", 0) == totals.get("ask", 0), (
        "bid and ask sides disagree on total quantity traded: %r" % (totals,)
    )


# --------------------------------------------------------------------------
# operation wrappers -- every book-mutating call in this module goes through
# one of these, so the invariants are re-checked after each operation
# --------------------------------------------------------------------------


def place_limit(book, side, qty, price, tid, instrument=INSTRUMENT):
    """Submit a limit order; return `(trades, quote)`."""
    order = dict(
        type="limit", side=side, instrument=instrument, qty=qty, price=price, tid=tid
    )
    trades, quote = book.processOrder(order, False, False)
    assert_invariants(book)
    return trades, quote


def place_market(book, side, qty, tid, instrument=INSTRUMENT):
    """Submit a market order; return `(trades, quote)`."""
    order = dict(type="market", side=side, instrument=instrument, qty=qty, tid=tid)
    trades, quote = book.processOrder(order, False, False)
    assert_invariants(book)
    return trades, quote


def cancel(book, side, idNum):
    book.cancelOrder(side, idNum)
    assert_invariants(book)


def modify(book, idNum, side, qty, price, tid):
    """Modify an order; return `(trades, quote)`.

    `price` is always passed explicitly: `modifyOrder` reads it unconditionally
    and treats a missing price as a price improvement.
    """
    trades, quote = book.modifyOrder(
        idNum, dict(side=side, qty=qty, price=price, tid=tid)
    )
    assert_invariants(book)
    return trades, quote


# --------------------------------------------------------------------------
# introspection helpers
# --------------------------------------------------------------------------


ORDER_STATE = """
select order_id, side, order_type, price, qty, fulfilled, cancel, event_dt
from trade_order
where idNum = ?
"""

COLUMNS = (
    "order_id",
    "side",
    "order_type",
    "price",
    "qty",
    "fulfilled",
    "cancel",
    "event_dt",
)


def order_state(book, idNum):
    """The stored row for `idNum`, as a dict."""
    row = book.db.cursor().execute(ORDER_STATE, (idNum,)).fetchone()
    assert row is not None, "no order with idNum=%r" % (idNum,)
    return dict(zip(COLUMNS, row))


def resting(book, side, instrument=INSTRUMENT):
    """Orders still live on `side`, in the engine's own matching priority order."""
    sql = book.active_orders + book.best_quotes_order_asc
    rows = book.db.cursor().execute(sql, dict(instrument=instrument, side=side))
    return [
        dict(
            idNum=idNum,
            qty=qty,
            fulfilled=fulfilled,
            price=price,
            available=qty - fulfilled,
        )
        for idNum, qty, fulfilled, price, event_dt, instrument in rows
    ]


def trade_log(book, instrument=INSTRUMENT):
    """Every trade, oldest first, as `(bid_order, ask_order, price, qty)`."""
    sql = """
        select trade.bid_order, trade.ask_order, trade.price, trade.qty
        from trade
        inner join trade_order on trade_order.order_id = trade.bid_order
        where trade_order.instrument = :instrument
        order by trade.trade_id
    """
    return book.db.cursor().execute(sql, dict(instrument=instrument)).fetchall()


# --------------------------------------------------------------------------
# adding limit orders
# --------------------------------------------------------------------------


def test_uncrossed_limit_order_rests_in_the_book(lob):
    trades, quote = place_limit(lob, "bid", 5, 99, tid=100)

    assert trades == []
    assert order_state(lob, quote["idNum"])["fulfilled"] == 0
    assert lob.getBestBid(INSTRUMENT) == 99
    assert resting(lob, "bid") == [
        dict(idNum=quote["idNum"], qty=5, fulfilled=0, price=99, available=5)
    ]


def test_orders_on_opposite_sides_of_the_spread_do_not_match(lob):
    place_limit(lob, "bid", 5, 99, tid=100)
    trades, _ = place_limit(lob, "ask", 5, 101, tid=101)

    assert trades == []
    assert trade_log(lob) == []
    assert lob.getBestBid(INSTRUMENT) == 99
    assert lob.getBestAsk(INSTRUMENT) == 101


def test_orders_on_the_same_side_do_not_match_each_other(lob):
    place_limit(lob, "bid", 5, 99, tid=100)
    trades, _ = place_limit(lob, "bid", 5, 100, tid=101)

    assert trades == []
    assert lob.getBestBid(INSTRUMENT) == 100
    assert [order["available"] for order in resting(lob, "bid")] == [5, 5]


# --------------------------------------------------------------------------
# crossing limit orders
# --------------------------------------------------------------------------


def test_crossing_limit_order_fully_matches_the_resting_order(lob):
    _, maker = place_limit(lob, "ask", 5, 101, tid=100)
    trades, taker = place_limit(lob, "bid", 5, 101, tid=101)

    assert len(trades) == 1
    bid_order, ask_order, _, price, qty = trades[0]
    assert (bid_order, ask_order, price, qty) == (
        taker["order_id"],
        maker["order_id"],
        101,
        5,
    )

    assert order_state(lob, maker["idNum"])["fulfilled"] == 5
    assert order_state(lob, taker["idNum"])["fulfilled"] == 5
    assert resting(lob, "ask") == []
    assert resting(lob, "bid") == []


def test_crossing_limit_order_trades_at_the_resting_price(lob):
    place_limit(lob, "ask", 5, 101, tid=100)
    trades, _ = place_limit(lob, "bid", 5, 103, tid=101)

    assert [(price, qty) for _, _, _, price, qty in trades] == [(101, 5)]


def test_crossing_limit_order_walks_the_book_in_price_time_priority(lob):
    _, far = place_limit(lob, "ask", 5, 103, tid=100)
    _, best_early = place_limit(lob, "ask", 5, 101, tid=101)
    _, best_late = place_limit(lob, "ask", 5, 101, tid=102)
    _, middle = place_limit(lob, "ask", 5, 102, tid=103)

    trades, taker = place_limit(lob, "bid", 12, 103, tid=109)

    # best price first; within a price level, the earlier arrival first.
    assert [(ask_order, price, qty) for _, ask_order, _, price, qty in trades] == [
        (best_early["order_id"], 101, 5),
        (best_late["order_id"], 101, 5),
        (middle["order_id"], 102, 2),
    ]
    assert order_state(lob, taker["idNum"])["fulfilled"] == 12
    assert order_state(lob, far["idNum"])["fulfilled"] == 0
    assert resting(lob, "ask") == [
        dict(idNum=middle["idNum"], qty=5, fulfilled=2, price=102, available=3),
        dict(idNum=far["idNum"], qty=5, fulfilled=0, price=103, available=5),
    ]


def test_crossing_limit_order_does_not_match_the_same_trader(lob):
    place_limit(lob, "ask", 5, 101, tid=100)
    trades, _ = place_limit(lob, "bid", 5, 101, tid=100)

    assert trades == []
    assert trade_log(lob) == []


# --------------------------------------------------------------------------
# partial fills
# --------------------------------------------------------------------------


def test_incoming_order_larger_than_the_book_rests_its_remainder(lob):
    place_limit(lob, "ask", 5, 101, tid=100)
    trades, taker = place_limit(lob, "bid", 8, 101, tid=101)

    assert [qty for _, _, _, _, qty in trades] == [5]
    assert order_state(lob, taker["idNum"])["fulfilled"] == 5
    assert resting(lob, "ask") == []
    assert resting(lob, "bid") == [
        dict(idNum=taker["idNum"], qty=8, fulfilled=5, price=101, available=3)
    ]


def test_incoming_order_smaller_than_the_book_leaves_the_maker_available(lob):
    _, maker = place_limit(lob, "ask", 10, 101, tid=100)
    trades, taker = place_limit(lob, "bid", 4, 101, tid=101)

    assert [qty for _, _, _, _, qty in trades] == [4]
    assert order_state(lob, taker["idNum"])["fulfilled"] == 4
    assert resting(lob, "ask") == [
        dict(idNum=maker["idNum"], qty=10, fulfilled=4, price=101, available=6)
    ]


def test_partially_filled_remainder_is_filled_by_a_later_order(lob):
    place_limit(lob, "ask", 5, 101, tid=100)
    _, resting_bid = place_limit(lob, "bid", 8, 101, tid=101)

    trades, _ = place_limit(lob, "ask", 3, 101, tid=102)

    assert [qty for _, _, _, _, qty in trades] == [3]
    assert order_state(lob, resting_bid["idNum"])["fulfilled"] == 8
    assert resting(lob, "bid") == []
    assert resting(lob, "ask") == []


def test_a_partially_filled_order_keeps_its_quantity(lob):
    _, maker = place_limit(lob, "ask", 10, 101, tid=100)
    place_limit(lob, "bid", 4, 101, tid=101)

    state = order_state(lob, maker["idNum"])
    assert (state["qty"], state["fulfilled"]) == (10, 4)


# --------------------------------------------------------------------------
# market orders
# --------------------------------------------------------------------------


def test_market_order_fills_at_maker_prices_across_price_levels(lob):
    _, best = place_limit(lob, "bid", 3, 100, tid=100)
    _, worse = place_limit(lob, "bid", 4, 99, tid=101)

    trades, taker = place_market(lob, "ask", 5, tid=102)

    assert [(bid_order, price, qty) for bid_order, _, _, price, qty in trades] == [
        (best["order_id"], 100, 3),
        (worse["order_id"], 99, 2),
    ]
    assert order_state(lob, taker["idNum"])["fulfilled"] == 5
    assert order_state(lob, taker["idNum"])["price"] is None
    assert resting(lob, "bid") == [
        dict(idNum=worse["idNum"], qty=4, fulfilled=2, price=99, available=2)
    ]


def test_market_order_fills_only_the_liquidity_that_is_there(lob):
    place_limit(lob, "ask", 3, 101, tid=100)
    place_limit(lob, "ask", 2, 102, tid=101)

    trades, taker = place_market(lob, "bid", 8, tid=102)

    assert sum(qty for _, _, _, _, qty in trades) == 5
    assert order_state(lob, taker["idNum"])["fulfilled"] == 5
    assert resting(lob, "ask") == []


def test_market_order_on_an_empty_opposite_side_does_not_trade(lob):
    trades, taker = place_market(lob, "bid", 8, tid=102)

    assert trades == []
    assert trade_log(lob) == []
    assert order_state(lob, taker["idNum"])["fulfilled"] == 0
    assert resting(lob, "ask") == []


# --------------------------------------------------------------------------
# cancelling
# --------------------------------------------------------------------------


def test_cancel_removes_the_order_from_the_book(lob):
    _, first = place_limit(lob, "bid", 5, 99, tid=100)
    _, second = place_limit(lob, "bid", 5, 98, tid=101)

    cancel(lob, "bid", first["idNum"])

    assert order_state(lob, first["idNum"])["cancel"] == 1
    assert lob.getBestBid(INSTRUMENT) == 98
    assert [order["idNum"] for order in resting(lob, "bid")] == [second["idNum"]]


def test_cancel_targets_exactly_one_order(lob):
    _, first = place_limit(lob, "bid", 5, 99, tid=100)
    _, second = place_limit(lob, "bid", 5, 99, tid=101)

    cancel(lob, "bid", first["idNum"])

    assert order_state(lob, first["idNum"])["cancel"] == 1
    assert order_state(lob, second["idNum"])["cancel"] == 0


def test_a_cancelled_order_no_longer_matches(lob):
    _, maker = place_limit(lob, "bid", 5, 99, tid=100)
    cancel(lob, "bid", maker["idNum"])

    trades, _ = place_limit(lob, "ask", 5, 99, tid=101)

    assert trades == []
    assert trade_log(lob) == []
    assert order_state(lob, maker["idNum"])["fulfilled"] == 0


def test_cancel_keeps_the_fills_a_partially_filled_order_already_had(lob):
    _, maker = place_limit(lob, "ask", 10, 101, tid=100)
    place_limit(lob, "bid", 4, 101, tid=101)

    cancel(lob, "ask", maker["idNum"])

    state = order_state(lob, maker["idNum"])
    assert (state["cancel"], state["qty"], state["fulfilled"]) == (1, 10, 4)
    assert resting(lob, "ask") == []


# --------------------------------------------------------------------------
# modifying
# --------------------------------------------------------------------------


def test_modify_raises_quantity_without_trading(lob):
    _, order = place_limit(lob, "bid", 5, 99, tid=100)

    trades, _ = modify(lob, order["idNum"], "bid", qty=9, price=99, tid=100)

    assert trades == []
    state = order_state(lob, order["idNum"])
    assert (state["qty"], state["fulfilled"], state["price"]) == (9, 0, 99)


def test_modify_quantity_increase_moves_the_order_to_the_back_of_the_queue(lob):
    _, first = place_limit(lob, "bid", 5, 99, tid=100)
    _, second = place_limit(lob, "bid", 5, 99, tid=101)

    modify(lob, first["idNum"], "bid", qty=9, price=99, tid=100)

    assert [order["idNum"] for order in resting(lob, "bid")] == [
        second["idNum"],
        first["idNum"],
    ]

    # and the queue order is what the matcher actually uses
    trades, _ = place_limit(lob, "ask", 5, 99, tid=102)
    assert [(bid_order, qty) for bid_order, _, _, _, qty in trades] == [
        (second["order_id"], 5)
    ]


def test_modify_quantity_decrease_keeps_time_priority(lob):
    _, first = place_limit(lob, "bid", 5, 99, tid=100)
    _, second = place_limit(lob, "bid", 5, 99, tid=101)

    modify(lob, first["idNum"], "bid", qty=3, price=99, tid=100)

    assert order_state(lob, first["idNum"])["qty"] == 3
    assert [order["idNum"] for order in resting(lob, "bid")] == [
        first["idNum"],
        second["idNum"],
    ]

    trades, _ = place_limit(lob, "ask", 3, 99, tid=102)
    assert [(bid_order, qty) for bid_order, _, _, _, qty in trades] == [
        (first["order_id"], 3)
    ]


def test_modify_quantity_below_fulfilled_clamps_to_fulfilled(lob):
    _, maker = place_limit(lob, "ask", 10, 101, tid=100)
    place_limit(lob, "bid", 6, 101, tid=101)
    assert order_state(lob, maker["idNum"])["fulfilled"] == 6

    trades, _ = modify(lob, maker["idNum"], "ask", qty=4, price=101, tid=100)

    assert trades == []
    state = order_state(lob, maker["idNum"])
    assert (state["qty"], state["fulfilled"]) == (6, 6)
    # clamped to fulfilled means fully filled, so nothing is left to match
    assert resting(lob, "ask") == []


def test_modify_price_away_from_the_market_does_not_trade(lob):
    place_limit(lob, "ask", 5, 101, tid=100)
    _, order = place_limit(lob, "bid", 5, 99, tid=101)

    trades, _ = modify(lob, order["idNum"], "bid", qty=5, price=98, tid=101)

    assert trades == []
    assert trade_log(lob) == []
    assert order_state(lob, order["idNum"])["price"] == 98
    assert lob.getBestBid(INSTRUMENT) == 98


def test_modify_bid_price_into_the_market_matches_at_the_resting_price(lob):
    _, maker = place_limit(lob, "ask", 5, 101, tid=100)
    _, order = place_limit(lob, "bid", 5, 99, tid=101)

    trades, _ = modify(lob, order["idNum"], "bid", qty=5, price=102, tid=101)

    assert [
        (bid_order, ask_order, price, qty)
        for bid_order, ask_order, _, price, qty in trades
    ] == [(order["order_id"], maker["order_id"], 101, 5)]
    assert order_state(lob, order["idNum"])["fulfilled"] == 5
    assert order_state(lob, maker["idNum"])["fulfilled"] == 5
    assert resting(lob, "bid") == []
    assert resting(lob, "ask") == []


def test_modify_ask_price_into_the_market_matches_at_the_resting_price(lob):
    _, maker = place_limit(lob, "bid", 5, 100, tid=100)
    _, order = place_limit(lob, "ask", 5, 103, tid=101)

    trades, _ = modify(lob, order["idNum"], "ask", qty=5, price=99, tid=101)

    assert [
        (bid_order, ask_order, price, qty)
        for bid_order, ask_order, _, price, qty in trades
    ] == [(maker["order_id"], order["order_id"], 100, 5)]
    assert order_state(lob, order["idNum"])["fulfilled"] == 5
    assert resting(lob, "bid") == []
    assert resting(lob, "ask") == []


def test_modify_price_partially_matches_and_rests_the_remainder(lob):
    _, maker = place_limit(lob, "ask", 3, 101, tid=100)
    _, order = place_limit(lob, "bid", 8, 99, tid=101)

    trades, _ = modify(lob, order["idNum"], "bid", qty=8, price=101, tid=101)

    assert [qty for _, _, _, _, qty in trades] == [3]
    assert order_state(lob, maker["idNum"])["fulfilled"] == 3
    assert resting(lob, "bid") == [
        dict(idNum=order["idNum"], qty=8, fulfilled=3, price=101, available=5)
    ]
