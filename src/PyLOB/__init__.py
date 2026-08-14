"""PyLOB: a limit order book for simulation research.

`OrderBook` is the matching engine (`PyLOB.engine`): price-time priority in
one layer, no I/O, no database. It is the engine ADR-0001 moved matching
into, and since ADR-0003 retired the SQL engine PR #7 built it is the only one --
`OrderBook(db=conn)` no longer means anything, and there is no
`LegacyOrderBook` to reach for. The public API that engine defined is kept
(`processOrder`, `cancelOrder`, `modifyOrder`, `getVolumeAtPrice`,
`getBest*`/`getWorst*`, `print`); only the implementation behind it is gone.

Also exported: the exceptions a caller catches (`PyLOBError` and its
subclasses), the objects the public API hands back (`Order`, `Trade`,
`Trader`), the two vocabularies it accepts (`Side`, `OrderType` -- plain
strings work everywhere they do), `EventSink`, the protocol a recorder
implements, and `__version__`, which a recorded session should note alongside
its results.

`SQLiteSink` is deliberately *not* here. Persistence is optional and off the
hot path (ADR-0001, ADR-0002), and importing it eagerly would make every
`import PyLOB` import `sqlite3` for a feature most callers never attach::

    from PyLOB.sinks.sqlite import SQLiteSink

    book = OrderBook(tick_size=0.01, sink=SQLiteSink("session.db"))
"""

from typing import Final

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

#: This library's version, as a literal rather than a lookup: the package is
#: installable from a git URL at a commit that `importlib.metadata` would
#: report by whatever version string the wheel was built with, and the
#: constraint that PyLOB "reads nothing off disk" (`config.yaml`) applies to
#: its own metadata too.
#:
#: **This line is the version.** `pyproject.toml` declares `version` dynamic
#: and hatchling reads this literal out of the file's text at build time, so
#: the wheel and the sdist cannot disagree with it -- edit here, and nowhere
#: else. Nothing in the library reads it; it is here for researchers recording
#: which version produced a session. `events.STREAM_VERSION` is the number
#: that governs whether a recorded session can be replayed -- this one does
#: not.
__version__: Final = "0.1.0"

__all__ = [
    "__version__",
    "OrderBook",
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
