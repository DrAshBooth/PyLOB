"""
Created on Apr 11, 2013

@author: Ash Booth

For a full walkthrough of PyLOB functionality and usage,
see the wiki @ https://github.com/ab24v07/PyLOB/wiki

"""

import sqlite3
import tempfile
from pathlib import Path

from PyLOB import OrderBook

INSTRUMENT = "FAKE"
CURRENCY = "USD"
# The traders this walkthrough submits for.
TRADERS = range(100, 112)


def configure(lob):
    """Declare the instrument and give every trader a commission schedule.

    Neither is required -- an unconfigured trader pays nothing and an
    instrument springs into being on first mention -- but a book that never
    hears the currency cannot move the cash leg of a trade, only the
    instrument leg.
    """
    lob.configure_instrument(INSTRUMENT, CURRENCY)
    for tid in TRADERS:
        lob.configure_trader(
            tid,
            name=str(tid),
            commission_min=2.5,
            commission_max_percnt=1,
            commission_per_unit=0.01,
        )


def walkthrough(lob):
    """Limit orders, crossing, market orders, cancel and modify."""
    configure(lob)

    ########### Limit Orders #############

    # A limit order that crosses nothing rests in the book at its price.
    # `submit` returns the order itself and the executions it caused, so
    # `order.idNum`, `order.fulfilled` and `order.remaining` are all the
    # bookkeeping a caller needs.
    lob.submit(100, INSTRUMENT, "ask", "limit", 5, 101)
    lob.submit(101, INSTRUMENT, "ask", "limit", 5, 103)
    lob.submit(102, INSTRUMENT, "ask", "limit", 5, 101)
    lob.submit(103, INSTRUMENT, "ask", "limit", 5, 101)
    bid_at_99, _ = lob.submit(100, INSTRUMENT, "bid", "limit", 5, 99)
    lob.submit(101, INSTRUMENT, "bid", "limit", 5, 98)
    lob.submit(102, INSTRUMENT, "bid", "limit", 5, 99)
    bid_at_97, _ = lob.submit(103, INSTRUMENT, "bid", "limit", 5, 97)

    # The current book may be viewed using a print
    lob.print(INSTRUMENT)

    # Submitting a limit order that crosses the opposing best price will
    # result in a trade -- priced at the *resting* order's limit, because the
    # order already in the book named the terms.
    _, trades = lob.submit(109, INSTRUMENT, "bid", "limit", 2, 102)
    print("bid 2 @ 102 crosses the best ask:", [(t.qty, t.price) for t in trades])
    lob.print(INSTRUMENT)

    # If a limit order crosses but is only partially matched, the remaining
    # volume is placed in the book as an outstanding order.
    order, trades = lob.submit(110, INSTRUMENT, "bid", "limit", 50, 102)
    print(
        "bid 50 @ 102 fills %d in %d trades and rests %d @ %s"
        % (order.fulfilled, len(trades), order.remaining, order.price)
    )
    lob.print(INSTRUMENT)

    ############# Market Orders ##############

    # A market order names no price: it takes the specified volume from the
    # inside of the book, whatever that costs. It is immediate-or-cancel and
    # never rests, so whatever the book could not fill is cancelled.
    order, _ = lob.submit(111, INSTRUMENT, "ask", "market", 40)
    print("market ask 40 fills %d, cancels %d" % (order.fulfilled, order.remaining))
    lob.print(INSTRUMENT)

    ############ Cancelling Orders #############

    # An order is cancelled by identifier; the side is optional and checked
    # against the order when given.
    print("cancelling bid for 5 @ 97..")
    lob.cancelOrder("bid", bid_at_97.idNum)
    lob.print(INSTRUMENT)

    ########### Modifying Orders #############

    # A resting order is modified by identifier. Asking for more quantity
    # costs time priority: the order goes to the back of its price level.
    lob.modifyOrder(bid_at_99.idNum, dict(side="bid", qty=14, price=99))
    print("book after increase amount. will be put as end of queue")
    lob.print(INSTRUMENT)

    # Changing the price costs time priority too -- and a price that crosses
    # matches immediately, exactly as an arriving order would.
    lob.modifyOrder(bid_at_99.idNum, dict(side="bid", qty=14, price=103.2))
    print("book after improve bid price. will process the order")
    lob.print(INSTRUMENT)

    ######### The Legacy Quote API ###########

    # `processOrder` takes the 2013 dict quote and returns `(trades, quote)`,
    # writing the identifier, timestamp and quantized price back into the
    # quote it was given. It is `submit` in the older clothes.
    quote = dict(type="market", side="ask", instrument=INSTRUMENT, qty=40, tid=111)
    trades, quote = lob.processOrder(quote, False, False)
    print("market ask 40 through processOrder ->", quote)
    lob.print(INSTRUMENT)


def recorded(db_path):
    """The optional second act: the same engine, with a recorder attached.

    Sinkless is the default and the fast path: ADR-0002 measures the
    throughput target with no sink attached, and an engine without one builds
    no event at all. Attaching a `SQLiteSink` costs roughly 8x throughput and
    buys the whole session back as queryable history -- worth it for the runs
    you mean to inspect afterwards, wasted on the ones you do not.
    """
    from PyLOB.sinks.sqlite import SQLiteSink

    lob = OrderBook(tick_size=0.01, sink=SQLiteSink(db_path))
    lob.configure_instrument(INSTRUMENT, CURRENCY)
    lob.configure_trader(100)
    lob.configure_trader(101)
    lob.submit(100, INSTRUMENT, "ask", "limit", 5, 101)
    lob.submit(101, INSTRUMENT, "bid", "limit", 5, 101)
    # Closing flushes the buffered tail; nothing is guaranteed on disk before.
    lob.close()

    conn = sqlite3.connect(db_path)
    try:
        trades = conn.execute("select trade_id, price, qty from trade").fetchall()
        events = conn.execute("select count(*) from event").fetchone()[0]
    finally:
        conn.close()
    print("recorded %d events at %s; trades: %r" % (events, db_path, trades))


def main():
    # No sink is the default: no I/O, nothing persisted, nothing constructed.
    walkthrough(OrderBook(tick_size=0.01))

    # The database is temporary only because this is an example -- point the
    # sink anywhere you want the session kept.
    with tempfile.TemporaryDirectory() as tmp:
        recorded(Path(tmp) / "session.db")


if __name__ == "__main__":
    main()
