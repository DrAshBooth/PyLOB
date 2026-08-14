"""A walkthrough of PyLOB, in the order README.md introduces it.

Run it from a clone with `uv run python src/example.py`. It prints a running
commentary and writes nothing outside a temporary directory it creates and
removes.

Three acts:

1. `walkthrough` -- one book, no sink. Limit orders that rest, a limit order
   that crosses, one that crosses only partially, a market order, a cancel, a
   modify, and one last submission through the legacy dict-quote API.
2. `recorded` -- the same engine with a `SQLiteSink` attached, and the SQL
   that reads the session back afterwards.
3. `replayed` -- that recording fed back into a fresh engine, which reaches
   the same book by matching again rather than by restoring anything.

`./verify` runs this file end to end, so it cannot drift from the library.

Written by Ash Booth in April 2013 and kept in step with the code since.
README.md is the short guide; the wiki carries the long-form walkthrough and a
guide to reading a recorded session back.
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

    Sinkless is the default and the fast path: an engine with no sink builds
    no event at all, and the throughput floor is measured in that
    configuration (ADR-0005, superseding ADR-0002). Attaching a `SQLiteSink`
    costs roughly 8x throughput and buys the whole session back as queryable
    history -- worth it for the runs you mean to inspect afterwards, wasted on
    the ones you do not.
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


def replayed(db_path):
    """The third act: the recording fed back into a fresh engine.

    What a session persists is its event log, so `read_events` decodes the log
    and `replay` re-issues the *commands* in it -- the configuration, the
    submissions, the modifications, the cancellations someone asked for. The
    fills are not fed back: this engine matches again and derives them itself,
    which is why an identical book is evidence of determinism and not of a
    restore.

    `replay` takes events rather than a path, so a session kept in memory (a
    `PyLOB.sinks.ListSink`) replays exactly the same way -- and `import PyLOB`
    still costs no `sqlite3`.
    """
    from PyLOB import replay
    from PyLOB.sinks.sqlite import read_events

    lob, trades = replay(read_events(db_path))
    print(
        "replayed: %d trade(s) re-derived, last price %s, best ask %s"
        % (len(trades), lob.getLastPrice(INSTRUMENT), lob.getBestAsk(INSTRUMENT))
    )


def main():
    # One book is one session, and the two acts below are two of them: there
    # is no `reset()` because constructing a fresh `OrderBook` is the reset,
    # and at 0.8 microseconds it is the pattern a per-episode gym or sweep
    # should use rather than avoid (README, "Sessions and episodes").
    #
    # No sink is the default: no I/O, nothing persisted, nothing constructed.
    walkthrough(OrderBook(tick_size=0.01))

    # The database is temporary only because this is an example -- point the
    # sink anywhere you want the session kept.
    with tempfile.TemporaryDirectory() as tmp:
        session = Path(tmp) / "session.db"
        recorded(session)
        replayed(session)


if __name__ == "__main__":
    main()
