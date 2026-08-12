"""PyLOB: a limit order book for simulation research.

`OrderBook` is the in-memory matching engine (`PyLOB.engine`): price-time
priority in one layer, no I/O, no database. It is the engine ADR-0001 moved
matching into, and as of this release it is what `from PyLOB import OrderBook`
gives you.

`LegacyOrderBook` is the 2013 SQL engine (`PyLOB.orderbook`), which matches by
executing queries against a SQLite connection you hand it. It stays in the
tree as the cross-check oracle the differential suite runs against, and it is
the class the old import used to name -- code that passed `OrderBook(db=conn)`
wants this one.

Also exported: the exceptions a caller catches (`PyLOBError` and its
subclasses), the objects the public API hands back (`Order`, `Trade`,
`Trader`), the two vocabularies it accepts (`Side`, `OrderType` -- plain
strings work everywhere they do), and `EventSink`, the protocol a recorder
implements.

`SQLiteSink` is deliberately *not* here. Persistence is optional and off the
hot path (ADR-0001, ADR-0002), and importing it eagerly would make every
`import PyLOB` import `sqlite3` for a feature most callers never attach::

    from PyLOB.sinks.sqlite import SQLiteSink

    book = OrderBook(tick_size=0.01, sink=SQLiteSink("session.db"))
"""

from .engine import (
    DEFAULT_TICK_SIZE,
    DuplicateOrderID,
    InvalidOrder,
    Order,
    OrderBook,
    PyLOBError,
    Trade,
    Trader,
    UnknownOrder,
)
from .events import EventSink, OrderType, Side
from .orderbook import OrderBook as LegacyOrderBook

__all__ = [
    "OrderBook",
    "LegacyOrderBook",
    "PyLOBError",
    "InvalidOrder",
    "DuplicateOrderID",
    "UnknownOrder",
    "Order",
    "Trade",
    "Trader",
    "Side",
    "OrderType",
    "EventSink",
    "DEFAULT_TICK_SIZE",
]
