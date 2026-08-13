"""The sink's projections, checked against the engine that produced them.

`recording-sink` says the recorded session must answer "what is the state" --
order history, trade history, balances, commissions, last-trade price --
queryable by SQL after the session ends. `SQLiteSink` answers it with the
projection layer: the `orders`, `trade`, `balance` and `instrument` tables and
the `resting_order` and `trader_commission` views, folded out of the log as it
is written.

Until this module, **nothing in `tests/` issued a single query against any of
them** (`docs/engine-review-2026-08.md`, lob-6oj). The suite checked that the
log was complete (`test_sink_durability.py`), that re-folding the log
reproduced the projections (the same fold, so a bug in it reproduced
faithfully), and that a recorded run matched a sinkless one (which reads the
engine, never the database). Eleven injected mutations lived in the gap
between those: a currency leg dropped entirely, both commission signs
flipped, a fill projected onto the wrong side's order row, the status
thresholds off by one, the last price never recorded, and the `orders` write
stream re-sorted so that a modify landed after the fill that followed it.

The check that closes the gap is the obvious one, and had simply never been
written: run a session, `close()`, then ask the database and the engine the
same questions.

    balance            == `OrderBook.holdings()`, every (trader, symbol)
    orders             == each `Order`'s qty, fulfilled, value, commission,
                          price, priority, currency and status
    resting_order      == `OrderBook.snapshot()`, in matching priority order,
                          with `available` as the quantity still to trade
    trade              == the `Trade` list the engine returned
    instrument         == `getLastPrice()`
    trader_commission  == commission summed per trader per currency

Both workloads run at `buffer_size` 1 and 100,000 -- one event per
transaction, and the whole session in one -- because `SQLiteSink` claims those
produce the same database, and because one of the eleven is only reachable at
the large one. A single batch is where the `orders` stream's statements are
grouped into `executemany` runs, and regrouping them by SQL text rather than
leaving them in `seq` order applies every fill before every modify: an order
modified and then filled ends up carrying the modify's stale `fulfilled`. At
`buffer_size=1` there is nothing to regroup and the mutation is invisible.

The scripted session reaches every transition on purpose -- including the two
the projections compute rather than copy, an exact fill and a modify that
clamps quantity down to what is already filled. The randomised one is breadth:
it does not know which transitions it is hitting, which is the point.

The reviewer verified that none of this is currently wrong (zero discrepancies
over three seeds and three buffer sizes, before any of it was written). This
module is the barrier, not the bug report.
"""

from __future__ import annotations

import random
import sqlite3

import pytest
from PyLOB.engine import OrderBook
from PyLOB.events import Accepted, Cancelled, CancelReason, Filled, OrderType, Side
from PyLOB.sinks.sqlite import SQLiteSink, read_events

TICK = 0.01

#: Two instruments in two currencies, so an order booked under the wrong one
#: has somewhere wrong to be.
CURRENCIES = {"FAKE": "USD", "BOGUS": "EUR"}

#: `(tid, commission_min, commission_max_percnt, commission_per_unit)`. One
#: trader pays nothing; the rest pay enough that a sign flip on a commission
#: leg is many orders of magnitude larger than the comparison tolerance.
TRADERS = (
    (1, 2.5, 1.0, 0.01),
    (2, 0.0, 0.5, 0.02),
    (3, 0.0, 0.0, 0.0),
    (4, 1.0, 2.0, 0.05),
)

#: Balances and commissions are unquantized floating-point sums (`commissions`
#: forbids rounding them) and the sink accumulates its own copy in SQL rather
#: than copying the engine's running total, so they are compared within a
#: tolerance -- one far below the smallest charge any schedule above makes.
MONEY = dict(rel=1e-12, abs=1e-9)

BUFFER_SIZES = (1, 100_000)


def build(db_path, buffer_size):
    """A configured engine recording into `db_path`."""
    book = OrderBook(tick_size=TICK, sink=SQLiteSink(db_path, buffer_size=buffer_size))
    for symbol, currency in CURRENCIES.items():
        book.configure_instrument(symbol, currency)
    for tid, minimum, percnt, per_unit in TRADERS:
        book.configure_trader(
            tid,
            name="t%d" % tid,
            commission_min=minimum,
            commission_max_percnt=percnt,
            commission_per_unit=per_unit,
        )
    return book


# --------------------------------------------------------------------------
# the two workloads
# --------------------------------------------------------------------------


def scripted(book):
    """A session that reaches every projection transition deliberately.

    Each step is here for a reason, because a projection bug is invisible in
    the shape of a workload and shows only in the comparison afterwards:

    - an order filled *exactly*, so the fill's `>= qty` threshold is the one
      being read and not a strict `>`;
    - a modify that clamps quantity down to the fulfilled amount, which
      completes an order through the modify path rather than through a fill;
    - a modify followed by a fill of the same order, the pair whose relative
      order the `orders` write stream has to preserve;
    - a market order that fills in full, and one whose remainder is cancelled;
    - a cancel with quantity still on it, and a cancel with no fills at all;
    - resting orders left on both sides at the end, one of them partly filled,
      so `resting_order.available` is not merely `qty`.

    Returns the trades the engine reported, in the order it reported them.
    """
    trades = []

    def submit(*args, **kwargs):
        order, done = book.submit(*args, **kwargs)
        trades.extend(done)
        return order

    def modify(order, **update):
        update.setdefault("side", str(order.side))
        update.setdefault("qty", None)
        update.setdefault("price", None)
        done, _ = book.modifyOrder(order.idNum, update)
        trades.extend(done)
        return order

    # A resting ask, partly consumed twice. The taker of the first fill is
    # itself filled exactly, which is the threshold case for the projection's
    # `fulfilled >= qty`.
    ask = submit(1, "FAKE", "ask", "limit", 10, 100.00)
    submit(2, "FAKE", "bid", "limit", 4, 100.00)
    submit(2, "FAKE", "bid", "limit", 5, 100.00)
    assert ask.fulfilled == 9 and ask.resting

    # ... and then finished by a modify that clamps its quantity down to what
    # is already filled. `order-lifecycle` reports that as a modification, so
    # the projection recomputes the status instead of waiting for a fill.
    modify(ask, qty=9)
    assert ask.filled and not ask.resting

    # A quantity increase (which re-queues) with no crossing liquidity, then
    # the fill that finishes it.
    bid = submit(3, "FAKE", "bid", "limit", 5, 99.50)
    modify(bid, qty=8)
    submit(4, "FAKE", "ask", "limit", 8, 99.50)
    assert bid.filled

    # Accepted and cancelled without ever trading.
    idle = submit(4, "FAKE", "ask", "limit", 7, 105.00)
    book.cancelOrder("ask", idle.idNum)

    # The other instrument, in the other currency: a cancel with quantity
    # still on it, a market order that fills in full, and one whose remainder
    # is cancelled.
    submit(1, "BOGUS", "bid", "limit", 6, 50.00)
    partial = submit(3, "BOGUS", "ask", "limit", 10, 50.00)
    assert partial.fulfilled == 6 and partial.resting
    book.cancelOrder("ask", partial.idNum)

    submit(4, "BOGUS", "ask", "limit", 5, 51.00)
    filled_market = submit(2, "BOGUS", "bid", "market", 3)
    assert filled_market.filled and not filled_market.cancelled
    hungry = submit(1, "BOGUS", "bid", "market", 20)
    assert hungry.fulfilled == 2 and hungry.cancelled

    # A repricing modify that crosses, so one order row takes a modify and
    # then a fill. Their relative order is what the write stream owes the log.
    repriced = submit(3, "FAKE", "bid", "limit", 5, 98.00)
    submit(1, "FAKE", "ask", "limit", 5, 100.50)
    modify(repriced, price=100.50)
    assert repriced.filled

    # What is left resting at the end, including a partly filled order so the
    # view's `available` is not simply its `qty`.
    submit(2, "FAKE", "bid", "limit", 4, 99.00)
    submit(3, "FAKE", "bid", "limit", 6, 99.00)
    submit(1, "FAKE", "bid", "limit", 2, 98.50)
    submit(4, "FAKE", "ask", "limit", 3, 101.00)
    submit(2, "BOGUS", "ask", "limit", 9, 52.00)
    leftover = submit(3, "BOGUS", "bid", "limit", 10, 52.00)
    assert leftover.fulfilled == 9 and leftover.resting

    return trades


def randomised(book, rng, n_ops=240):
    """Limits, markets, cancels and modifies, chosen by `rng`.

    Breadth rather than intent: the scripted session knows which transitions
    it is reaching and this one does not, which is why both are here. Targets
    for cancels and modifies are drawn from the orders that are still resting,
    so the workload follows the book rather than a fixed script.
    """
    trades = []
    resting = []
    tids = [tid for tid, *_ in TRADERS]
    for _ in range(n_ops):
        resting = [order for order in resting if order.resting]
        symbol = rng.choice(list(CURRENCIES))
        mid = 100.0 if symbol == "FAKE" else 50.0
        roll = rng.random()
        if roll < 0.58 or not resting:
            order, done = book.submit(
                tid=rng.choice(tids),
                instrument=symbol,
                side=rng.choice(("bid", "ask")),
                order_type="limit",
                qty=rng.randint(1, 8),
                price=round(mid + rng.randint(-40, 40) * TICK, 2),
            )
            resting.append(order)
        elif roll < 0.70:
            _, done = book.submit(
                tid=rng.choice(tids),
                instrument=symbol,
                side=rng.choice(("bid", "ask")),
                order_type="market",
                qty=rng.randint(1, 6),
            )
        elif roll < 0.85:
            order = resting.pop(rng.randrange(len(resting)))
            book.cancelOrder(order.side, order.idNum)
            done = []
        else:
            order = resting[rng.randrange(len(resting))]
            done, _ = book.modifyOrder(
                order.idNum,
                dict(
                    side=str(order.side),
                    qty=rng.randint(1, 12),
                    price=round(order.price + rng.randint(-20, 20) * TICK, 2),
                ),
            )
        trades.extend(done)
    return trades


# --------------------------------------------------------------------------
# what the engine says, and what the database says
# --------------------------------------------------------------------------


def expected_status(order):
    """The `orders.status` this order should have been projected to.

    Cancellation is read first because it is sticky in the projection too: an
    IOC remainder is cancelled with fills against it, and "cancelled" is what
    a reader is owed.
    """
    if order.cancelled:
        return "cancelled"
    return "filled" if order.filled else "open"


def rows(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


def assert_balances_match(book, conn):
    """`balance` is the engine's ledger: both legs, both sides, every trader."""
    engine = {(tid, symbol): amount for tid, symbol, amount in book.holdings()}
    sink = {
        (tid, symbol): amount
        for tid, symbol, amount in rows(conn, "SELECT tid, symbol, amount FROM balance")
    }
    assert set(engine) == set(sink), (
        "balance rows differ: engine-only %r, sink-only %r"
        % (sorted(set(engine) - set(sink)), sorted(set(sink) - set(engine)))
    )
    for key, amount in engine.items():
        assert sink[key] == pytest.approx(amount, **MONEY), (
            "balance%r: engine %r, sink %r"
            % (
                key,
                amount,
                sink[key],
            )
        )


def assert_orders_match(book, conn):
    """`orders` carries every order's terms and its cumulative accounting."""
    sink = {
        row[0]: row
        for row in rows(
            conn,
            "SELECT idNum, tid, instrument, currency, side, order_type, price, "
            "qty, fulfilled, priority, status FROM orders",
        )
    }
    engine_ids = {order.idNum for order in book.orders()}
    assert engine_ids == set(sink), (
        "order rows differ: engine-only %r, sink-only %r"
        % (sorted(engine_ids - set(sink)), sorted(set(sink) - engine_ids))
    )
    for order in book.orders():
        assert sink[order.idNum][1:] == (
            order.tid,
            order.instrument,
            CURRENCIES[order.instrument],
            str(order.side),
            str(order.order_type),
            order.price,
            order.qty,
            order.fulfilled,
            order.priority,
            expected_status(order),
        ), "order %d projected wrongly: %r" % (order.idNum, sink[order.idNum])

    # Value and commission are money and are compared as money. They are also
    # the pair one mutation swapped into each other's columns, which is why
    # they are read out separately rather than folded into the tuple above.
    for idNum, value, commission in rows(
        conn, "SELECT idNum, value, commission FROM orders"
    ):
        order = book.order(idNum)
        assert value == pytest.approx(order.value, **MONEY), (
            "order %d value: engine %r, sink %r" % (idNum, order.value, value)
        )
        assert commission == pytest.approx(order.commission, **MONEY), (
            "order %d commission: engine %r, sink %r"
            % (idNum, order.commission, commission)
        )


def assert_resting_orders_match(book, conn):
    """`resting_order` is `snapshot()`: same orders, same order, same quantity.

    The view has no notion of which end of a book is the good end, so matching
    priority order is reconstructed from its own columns -- best price first,
    then arrival stamp -- and compared against the snapshot, whose tuple
    position *is* that order (`book-queries`).
    """
    for symbol in CURRENCIES:
        for side in ("bid", "ask"):
            snapshot = book.snapshot(symbol, side)
            view = sorted(
                rows(
                    conn,
                    "SELECT idNum, price, available, qty, fulfilled, priority "
                    "FROM resting_order WHERE instrument = ? AND side = ?",
                    symbol,
                    side,
                ),
                key=lambda row: (-row[1] if side == "bid" else row[1], row[5]),
            )
            assert [row[0] for row in view] == [order.idNum for order in snapshot], (
                "resting_order[%s, %s] is not the snapshot" % (symbol, side)
            )
            for row, order in zip(view, snapshot):
                assert row[1:] == (
                    order.price,
                    order.remaining,
                    order.qty,
                    order.fulfilled,
                    order.priority,
                ), "resting_order row %r disagrees with order %d" % (row, order.idNum)


def assert_trades_match(book, conn, trades):
    """`trade` is the executions the engine reported to whoever caused them."""
    recorded = rows(
        conn,
        "SELECT trade_id, instrument, price, qty, taker_side, bid_idNum, "
        "bid_tid, ask_idNum, ask_tid FROM trade ORDER BY trade_id",
    )
    assert len(recorded) == len(trades), "%d trades recorded, engine made %d" % (
        len(recorded),
        len(trades),
    )
    assert recorded == [
        (
            trade.trade_id,
            trade.instrument,
            trade.price,
            trade.qty,
            str(trade.taker_side),
            trade.bid_idNum,
            trade.bid_tid,
            trade.ask_idNum,
            trade.ask_tid,
        )
        for trade in trades
    ]


def assert_last_prices_match(book, conn):
    """`instrument.last_price` is `getLastPrice`, the value `book-queries` names."""
    recorded = dict(rows(conn, "SELECT symbol, last_price FROM instrument"))
    assert set(recorded) == set(CURRENCIES)
    for symbol in CURRENCIES:
        assert recorded[symbol] == book.getLastPrice(symbol), (
            "last_price[%s]: engine %r, sink %r"
            % (symbol, book.getLastPrice(symbol), recorded[symbol])
        )


def assert_commission_view_matches(book, conn):
    """`trader_commission` sums each order's own commission in its own currency.

    Per the `commissions` contract the charge is a function of an order's
    cumulative fills, so the sum is over orders and never over trades.
    """
    expected = {}
    for order in book.orders():
        key = (order.tid, CURRENCIES[order.instrument])
        expected[key] = expected.get(key, 0.0) + order.commission
    view = {
        (tid, currency): total
        for tid, currency, total in rows(
            conn, "SELECT tid, currency, commission FROM trader_commission"
        )
    }
    assert set(view) == set(expected), (
        "trader_commission keys differ: view %r, engine %r"
        % (sorted(view), sorted(expected))
    )
    for key, total in expected.items():
        assert view[key] == pytest.approx(total, **MONEY), (
            "trader_commission%r: engine %r, view %r" % (key, total, view[key])
        )


def assert_projections_match(book, db_path, trades):
    """Every projection, against the engine that wrote it."""
    conn = sqlite3.connect(db_path)
    try:
        assert_balances_match(book, conn)
        assert_orders_match(book, conn)
        assert_resting_orders_match(book, conn)
        assert_trades_match(book, conn, trades)
        assert_last_prices_match(book, conn)
        assert_commission_view_matches(book, conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# the projections
# --------------------------------------------------------------------------


@pytest.mark.parametrize("buffer_size", BUFFER_SIZES)
def test_the_projections_are_the_engines_own_state(tmp_path, buffer_size):
    """The scripted session, read back out of SQL and compared field by field."""
    db_path = tmp_path / ("scripted-%d.db" % buffer_size)
    book = build(db_path, buffer_size)
    trades = scripted(book)
    book.close()

    assert trades, "the scripted session must trade"
    assert_projections_match(book, db_path, trades)


@pytest.mark.parametrize("buffer_size", BUFFER_SIZES)
def test_the_projections_hold_over_a_randomised_session(tmp_path, buffer_size):
    """The same comparison over a workload that chose its own transitions."""
    db_path = tmp_path / ("random-%d.db" % buffer_size)
    book = build(db_path, buffer_size)
    trades = randomised(book, random.Random(20260813))
    book.close()

    assert len(trades) > 40, "the workload made only %d trades" % len(trades)
    assert_projections_match(book, db_path, trades)


def test_the_two_buffer_sizes_project_identically(tmp_path):
    """`buffer_size` is a throughput knob, asserted here on the projections.

    `test_sink_durability.py` makes the claim about the whole database; this
    is the same claim narrowed to the tables above, and it is worth restating
    because the projections are what gets grouped into `executemany` runs and
    so what a regrouping would reorder.
    """
    dumps = []
    for buffer_size in BUFFER_SIZES:
        db_path = tmp_path / ("both-%d.db" % buffer_size)
        book = build(db_path, buffer_size)
        scripted(book)
        book.close()
        conn = sqlite3.connect(db_path)
        try:
            dumps.append(
                {
                    table: rows(conn, "SELECT * FROM %s ORDER BY %s" % (table, key))
                    for table, key in (
                        ("orders", "idNum"),
                        ("trade", "trade_id"),
                        ("balance", "tid, symbol"),
                        ("instrument", "symbol"),
                    )
                }
            )
        finally:
            conn.close()
    assert dumps[0] == dumps[1]


def test_an_instrument_with_no_currency_books_no_currency_leg(tmp_path):
    """The sink says so rather than inventing one, and the engine agrees.

    `_settle` moves the instrument leg of an unconfigured instrument and
    leaves the cash leg recoverable from the fill; the sink does the same and
    warns. The two ledgers agreeing *about the absence* is the part worth
    pinning: either side guessing a currency here would diverge from the other
    silently, which is the failure `configure_instrument(sym, None)` used to
    cause from the engine's end.
    """
    db_path = tmp_path / "nocurrency.db"
    book = OrderBook(tick_size=TICK, sink=SQLiteSink(db_path, buffer_size=1))
    book.configure_trader(1, "1", commission_min=2.0)
    book.configure_trader(2, "2", commission_min=2.0)
    book.submit(1, "NAKED", "ask", "limit", 5, 100.0)
    book.submit(2, "NAKED", "bid", "limit", 5, 100.0)
    book.close()

    assert sorted(book.holdings()) == [(1, "NAKED", -5.0), (2, "NAKED", 5.0)]
    conn = sqlite3.connect(db_path)
    try:
        assert rows(conn, "SELECT tid, symbol, amount FROM balance ORDER BY tid") == [
            (1, "NAKED", -5.0),
            (2, "NAKED", 5.0),
        ]
    finally:
        conn.close()


def test_a_projection_query_is_what_a_deep_dive_actually_runs(tmp_path):
    """The reason the layer exists: the book in SQL, without folding the log.

    `SQLiteSink`'s docstring sells the projections as turning "reconstruct the
    book from 200k JSON rows" into `SELECT * FROM resting_order`. This is that
    query -- depth by price -- with the engine's own snapshot and volume query
    as the expected answer.
    """
    db_path = tmp_path / "deepdive.db"
    book = build(db_path, buffer_size=64)
    scripted(book)
    book.close()

    conn = sqlite3.connect(db_path)
    try:
        depth = conn.execute(
            "SELECT price, SUM(available) FROM resting_order "
            "WHERE instrument = 'FAKE' AND side = 'bid' "
            "GROUP BY price ORDER BY price DESC"
        ).fetchall()
    finally:
        conn.close()

    expected = {}
    for order in book.snapshot("FAKE", "bid"):
        expected[order.price] = expected.get(order.price, 0) + order.remaining
    assert depth, "the scripted session leaves bids resting"
    assert depth == sorted(expected.items(), reverse=True)

    # The same rows, read the way the query above is actually used: cumulative
    # depth down the book is what an opposite order at that price could take.
    running = 0
    for price, volume in depth:
        running += volume
        assert running == book.getVolumeAtPrice("FAKE", "bid", price)


# --------------------------------------------------------------------------
# when the writes happen
# --------------------------------------------------------------------------


def test_the_buffer_flushes_at_its_size_not_one_event_later(tmp_path):
    """`buffer_size` events are on disk once the `buffer_size`-th is consumed.

    Read through the sink's own connection, which is the documented way to
    query a session that is still running: what it can see is what has been
    flushed. A threshold that fires one event late leaves the last event of
    every batch unwritten until the next one arrives -- invisible after
    `close`, and the difference between a live query seeing the trade that
    just happened and seeing the one before it.
    """
    sink = SQLiteSink(tmp_path / "buffered.db", buffer_size=4)
    book = OrderBook(tick_size=TICK, sink=sink)  # 1: SessionStarted
    assert sink.buffered == 1

    book.configure_instrument("FAKE", "USD")  # 2
    book.configure_trader(1, "1")  # 3
    assert sink.buffered == 3
    assert sink.connection.execute("SELECT COUNT(*) FROM event").fetchone() == (0,)

    book.submit(1, "FAKE", "bid", "limit", 5, 100.0)  # 4: Accepted
    assert sink.buffered == 0
    assert sink.connection.execute("SELECT COUNT(*) FROM event").fetchone() == (4,)
    assert sink.connection.execute("SELECT COUNT(*) FROM orders").fetchone() == (1,)

    sink.close()


# --------------------------------------------------------------------------
# reading the log back
# --------------------------------------------------------------------------


def test_a_decoded_event_carries_enum_members_not_bare_strings(tmp_path):
    """`decode_event` puts the `StrEnum` members back.

    JSON stores them as the strings they already are, so `event.side == "bid"`
    holds either way and every equality comparison in the suite passes on the
    raw string. What does not survive is identity -- `is Side.BID`, and the
    `match` statements a replayer and every sink dispatch on -- so a decoded
    stream without the restoration compares equal and behaves differently.
    """
    db_path = tmp_path / "decode.db"
    book = build(db_path, buffer_size=1)
    book.submit(1, "FAKE", "ask", "limit", 5, 100.0)
    book.submit(2, "FAKE", "bid", "limit", 5, 100.0)
    starved, _ = book.submit(3, "FAKE", "bid", "market", 4)
    assert starved.cancel_reason is CancelReason.IOC_REMAINDER
    book.close()

    seen = set()
    for event in read_events(db_path):
        match event:
            case Accepted():
                assert type(event.side) is Side
                assert type(event.order_type) is OrderType
                seen.add("accepted")
            case Filled():
                assert type(event.taker_side) is Side
                seen.add("filled")
            case Cancelled():
                assert type(event.side) is Side
                assert type(event.reason) is CancelReason
                seen.add("cancelled")
    assert seen == {"accepted", "filled", "cancelled"}


def test_a_second_session_cannot_merge_into_a_finished_recording(tmp_path):
    """One database per session, as `SQLiteSink`'s "Scope" says and nothing checked.

    `seq` is the log's primary key, so the intruder's first batch collides,
    every event of it collides again on salvage, and the session is recorded
    as lost rather than half-merged into somebody else's history. The
    projections of the first session are left exactly as they were.
    """
    db_path = tmp_path / "twice.db"
    book = build(db_path, buffer_size=1)
    book.submit(1, "FAKE", "ask", "limit", 5, 100.0)
    book.close()
    conn = sqlite3.connect(db_path)
    try:
        before = rows(conn, "SELECT * FROM orders ORDER BY idNum")
        events_before = conn.execute("SELECT COUNT(*) FROM event").fetchone()
    finally:
        conn.close()

    intruder_sink = SQLiteSink(db_path, buffer_size=1)
    intruder = OrderBook(tick_size=TICK, sink=intruder_sink)
    intruder.configure_instrument("FAKE", "USD")
    with pytest.raises(sqlite3.IntegrityError):
        intruder_sink.close()

    conn = sqlite3.connect(db_path)
    try:
        assert rows(conn, "SELECT * FROM orders ORDER BY idNum") == before
        assert conn.execute("SELECT COUNT(*) FROM event").fetchone() == events_before
        assert conn.execute("SELECT COUNT(*) FROM event_loss").fetchone()[0] > 0
    finally:
        conn.close()
