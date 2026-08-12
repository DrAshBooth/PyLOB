"""The in-memory book: order store, price levels, and tick quantization.

ADR-0001 moved matching out of SQLite and into this module. Everything the
matching loop needs to be fast and deterministic lives here -- the structures,
the identifier rules, and the price grid -- and nothing here matches. The
crossing loop, cancel/modify, and the ledgers build on top of this file.

No SQL. This module must never import `sqlite3`: a sink is optional and lives
behind `events.EventSink`, so the engine has to be complete without one.

The structures
--------------

One `InstrumentBook` per symbol, two `BookSide`s each. A side is a dict of
price -> `PriceLevel`, plus two heaps of prices for the two ends of the book::

    BookSide
      _levels   {price: PriceLevel}          membership, O(1)
      _best     heap, best price on top      matching reads this
      _worst    heap, worst price on top     reporting reads this

`PriceLevel` holds `{idNum: Order}`. A dict is insertion-ordered, so it *is* a
FIFO queue, and unlike a `deque` it also removes by identifier in O(1) --
which is what cancel does, and cancel is the second most common operation in a
replay after submission. Re-inserting an order puts it at the back, which is
exactly the priority re-stamp a non-passive modify owes (`order-lifecycle`).

The heaps carry prices, not levels, and use lazy deletion: emptying a level
drops it from `_levels` and leaves a stale price behind, which the next peek
pops. A price that is re-created before its stale entry surfaces is *still
correct* -- the heap holds the price value, and the value has not changed --
so duplicates cost memory, not accuracy. `_compact` bounds that memory.

Costs, in the number of price levels L on a side and orders N in a level:

    best / worst price          O(1) amortized (O(log L) when stale entries pop)
    insert into an old level    O(1)
    insert into a new level     O(log L)
    cancel                      O(1)
    fill the front of the book  O(1) per order, O(log L) per exhausted level
    volume at price             O(L)   -- levels, not orders: `volume` is cached
    snapshot                    O(L log L + N)

Two heaps rather than one sorted structure because matching only ever asks for
one end. `getWorstBid`/`getWorstAsk` are reporting queries (`book-queries`)
and deserve to be cheap, but they do not deserve to slow down insertion. A
sorted list of prices would answer both ends and range scans in one structure,
but pays an O(L) memmove on every new level; a red-black tree pays more code
than the research scale is worth (ADR-0001 rejected the 2013 implementation's
tree for the same reason).

Identity
--------

`_orders` maps `idNum` -> `Order` and is never pruned: a filled or cancelled
order is still addressable, because the acceptance surface asks a finished
order for its `fulfilled` and `commission`. Retention is also what makes
identifiers unique *within the book's lifetime* rather than merely among
resting orders, which is what `order-lifecycle` requires.

Two rules, both enforced in `create_order`, and both bugs on the legacy engine
(lob-a17, lob-7e7):

- an externally supplied `idNum` already in `_orders` is rejected, so no two
  orders ever answer to one identifier;
- the auto-assignment counter is a high-water mark over every identifier the
  engine has seen, supplied or assigned. Replay re-issues each `Accepted` with
  its original identifier, so the counter re-seeds itself as the stream is
  read and an order submitted after a reload cannot collide with one from
  before it. There is no separate "restore the counter" step to forget.

Priority
--------

`priority` is a monotonic counter stamped at acceptance, and it is the only
tie-break in matching: orders sort by `(price, priority)`. `timestamp` is
recorded data and never a sort key -- two replayed orders may carry the same
one (lob-xqz; legacy tie-broke on SQLite's storage order, which no query
guarantees). `seq`, the position in the event stream, is a third counter and
matching must never read it: an engine with no sink must match identically to
one with four.

Tick quantization
-----------------

`order-lifecycle` requires the nearest multiple of the tick for *any* positive
tick, exact for decimal ticks. Legacy computed `round(price, log10(1/tick))`,
which is a digit count, not a grid: it treats 0.25, 1 and 5 as the same tick
and quantizes 100.03 to 100.0 on a 0.05 tick. Here the grid is real -- divide
by the tick, round to an integer, multiply back -- carried out in `decimal` so
that 0.05 is a twentieth and not 0.05000000000000000277. The float that comes
back is the nearest double to the exact decimal multiple.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Context, Decimal
from functools import lru_cache
from typing import Final

from .events import (
    Accepted,
    CancelReason,
    Event,
    EventSink,
    InstrumentConfigured,
    OrderType,
    SessionStarted,
    Side,
    TraderConfigured,
    close_sink,
)

__all__ = [
    "PyLOBError",
    "InvalidOrder",
    "DuplicateOrderID",
    "UnknownOrder",
    "quantize_price",
    "Order",
    "Trader",
    "PriceLevel",
    "BookSide",
    "InstrumentBook",
    "OrderBook",
    "DEFAULT_TICK_SIZE",
]

DEFAULT_TICK_SIZE: Final = 0.0001


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class PyLOBError(Exception):
    """Base of every error this library raises.

    `order-lifecycle` requires that a bad submission raise "a library
    exception" and that the API "never terminate the host process" -- the
    legacy engine called `sys.exit` on a qty of zero. Catching this class
    catches everything the engine refuses.
    """


class InvalidOrder(PyLOBError, ValueError):
    """A submission or modification the engine will not accept.

    Also a `ValueError`, because that is what a caller who has never read
    these docs will already be catching around bad input.
    """


class DuplicateOrderID(InvalidOrder):
    """An externally supplied identifier that some order already carries.

    Rejected rather than accepted-and-disambiguated: `order-lifecycle` says
    operations addressing an identifier affect at most one order, and the only
    way to keep that promise is to never let a second order have the id.
    """


class UnknownOrder(PyLOBError, LookupError):
    """Cancel or modify named an identifier no order has."""


# --------------------------------------------------------------------------
# tick quantization
# --------------------------------------------------------------------------

#: Wide enough that the intermediate quotient never rounds: a price/tick ratio
#: needs 40 significant digits before this loses anything, and no market has a
#: grid that deep.
_CTX: Final = Context(prec=40, rounding=ROUND_HALF_EVEN)
_ONE: Final = Decimal(1)


@lru_cache(maxsize=256)
def _tick_decimal(tick_size: float) -> Decimal:
    """The tick as an exact decimal, via `str` so 0.05 is a twentieth.

    `Decimal(0.05)` is 0.05000000000000000277..., the double; `Decimal("0.05")`
    is the number the caller meant. Cached because a book quantizes on every
    submission and the tick never changes.
    """
    if not tick_size > 0:
        raise InvalidOrder("tick size must be positive, got %r" % (tick_size,))
    tick = Decimal(str(tick_size))
    if not tick.is_finite() or tick <= 0:
        raise InvalidOrder("tick size must be positive and finite, got %r" % (tick,))
    return tick


def _quantize(price: float, tick: Decimal) -> float:
    """`price` snapped to the nearest multiple of an already-decimalized tick."""
    multiples = _CTX.divide(Decimal(str(price)), tick).quantize(
        _ONE, rounding=ROUND_HALF_EVEN, context=_CTX
    )
    # `+ 0.0` only to turn a -0.0 back into 0.0: they compare and hash alike,
    # so it is cosmetic, but a book should not report a negative zero price.
    return float(_CTX.multiply(multiples, tick)) + 0.0


def quantize_price(price: float, tick_size: float) -> float:
    """`price` snapped to the nearest multiple of `tick_size`.

    Works for any positive tick -- 0.05, 0.25, 1, 5, 0.0001 -- because the
    tick is a grid spacing here and not a count of decimal places. Exact for
    decimal ticks: on a 0.05 grid 100.03 quantizes to 100.05, and the result
    is the nearest double to that exact decimal.

    Both operands are read through `str`, so the value quantized is the number
    the caller wrote rather than the double they got: 100.03 means 100.03, not
    100.0299999999999994. Exact halves round to even, as Python's own `round`
    does, so the grid introduces no directional bias.
    """
    return _quantize(price, _tick_decimal(tick_size))


# --------------------------------------------------------------------------
# what the book holds
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Order:
    """One order, from acceptance until the end of the session.

    Mutable and identity-addressed: the book, the level, and `_orders` all
    hold the same object, so a fill updates one place. It outlives its stay in
    the book -- a filled or cancelled order stays in the store and keeps
    answering for its `fulfilled` and `commission`.

    `price` is the *quantized working price*, never the price as submitted,
    and `None` exactly for market orders. `value` and `commission` are
    cumulative over the order's life and are maintained by the ledgers, not
    here.
    """

    idNum: int
    tid: int
    instrument: str
    side: Side
    order_type: OrderType
    price: float | None
    qty: int
    timestamp: float
    #: Arrival stamp; matching sorts by (price, priority). Re-stamped by a
    #: non-passive modify. Never a timestamp, never an event `seq`.
    priority: int
    fulfilled: int = 0
    #: Cumulative traded value (sum of qty * price over this order's fills).
    value: float = 0.0
    #: Cumulative commission charged, recomputed from cumulative (Q, V).
    commission: float = 0.0
    cancelled: bool = False
    cancel_reason: CancelReason | None = None

    @property
    def remaining(self) -> int:
        """Quantity still available to trade."""
        return self.qty - self.fulfilled

    @property
    def filled(self) -> bool:
        """Is this order done by execution alone?"""
        return self.fulfilled >= self.qty

    @property
    def resting(self) -> bool:
        """Is this order in the book right now?

        Derived rather than stored, and false for a market order by
        construction rather than by the matching loop remembering to cancel
        the remainder: `order-lifecycle` says a market order never rests, and
        a derived flag cannot be caught mid-processing saying otherwise.
        """
        return (
            self.order_type is OrderType.LIMIT
            and not self.cancelled
            and self.fulfilled < self.qty
        )


@dataclass(slots=True)
class Trader:
    """A participant's standing configuration.

    Held here because matching consults `allow_self_matching` and the ledgers
    consult the schedule; the engine charges the commission itself (design.md
    decision 3), so the numbers cannot live only in a sink.
    """

    tid: int
    name: str = ""
    allow_self_matching: bool = False
    commission_min: float = 0.0
    commission_max_percnt: float = 0.0
    commission_per_unit: float = 0.0


class PriceLevel:
    """The FIFO queue of orders resting at one price.

    Backed by a dict keyed on `idNum`: insertion-ordered, so iteration is
    priority order, and O(1) to remove by identifier. `volume` is the sum of
    the members' `remaining`, maintained incrementally so that a volume query
    costs one addition per *level* rather than one per order.
    """

    __slots__ = ("price", "volume", "_orders")

    def __init__(self, price: float) -> None:
        self.price = price
        self.volume = 0
        self._orders: dict[int, Order] = {}

    def __len__(self) -> int:
        return len(self._orders)

    def __bool__(self) -> bool:
        return bool(self._orders)

    def __iter__(self) -> Iterator[Order]:
        """Members in queue order -- the order they will match in."""
        return iter(self._orders.values())

    def __contains__(self, order: Order) -> bool:
        return self._orders.get(order.idNum) is order

    def __repr__(self) -> str:
        return "PriceLevel(price=%r, orders=%d, volume=%d)" % (
            self.price,
            len(self._orders),
            self.volume,
        )

    def first(self) -> Order | None:
        """The order at the front of the queue, or None if the level is empty."""
        for order in self._orders.values():
            return order
        return None

    def append(self, order: Order) -> None:
        """Put `order` at the back of the queue."""
        if order.idNum in self._orders:
            raise DuplicateOrderID(
                "order %r is already resting at %r" % (order.idNum, self.price)
            )
        self._orders[order.idNum] = order
        self.volume += order.remaining

    def discard(self, order: Order) -> bool:
        """Take `order` out of the queue; False if it was not in it."""
        if self._orders.pop(order.idNum, None) is None:
            return False
        self.volume -= order.remaining
        return True


class BookSide:
    """One side of one instrument's book: its levels and its two ends.

    Every mutation of a resting order's quantity goes through `fill` or
    `resize` rather than through the `Order` directly, because the level's
    cached `volume` and the order's membership are derived from it. Reaching
    past these methods leaves the cache wrong and the book holding an order
    with nothing left to trade.

    Two fields are the book's index into itself and must not be changed while
    an order rests: `price` names its level and `priority` is its place in
    that level's queue. A modification that changes either is `remove`, then
    the change, then `add` -- which is also exactly the trip to the back of
    the queue that `order-lifecycle` requires of a non-passive modify.
    """

    __slots__ = ("side", "_levels", "_best", "_worst", "_best_sign")

    def __init__(self, side: Side) -> None:
        self.side = side
        self._levels: dict[float, PriceLevel] = {}
        # Prices, sign-flipped so `heapq`'s minimum is the end we want. A bid
        # is better the higher it is, an ask the lower.
        self._best_sign = -1.0 if side is Side.BID else 1.0
        self._best: list[float] = []
        self._worst: list[float] = []

    def __len__(self) -> int:
        """How many price levels are on this side."""
        return len(self._levels)

    def __bool__(self) -> bool:
        return bool(self._levels)

    def __iter__(self) -> Iterator[Order]:
        """Every resting order, best price first and FIFO within a price.

        This is matching priority order, which is also snapshot order
        (`book-queries`: a snapshot is "ordered by matching priority").
        """
        for level in self.levels():
            yield from level

    def __repr__(self) -> str:
        return "BookSide(side=%r, levels=%d, volume=%d)" % (
            str(self.side),
            len(self._levels),
            self.total_volume,
        )

    # -- reading -----------------------------------------------------------

    def best_price(self) -> float | None:
        """Highest bid / lowest ask, or None when the side is empty."""
        return self._peek(self._best, self._best_sign)

    def worst_price(self) -> float | None:
        """Lowest bid / highest ask, or None when the side is empty."""
        return self._peek(self._worst, -self._best_sign)

    def best_level(self) -> PriceLevel | None:
        """The level matching would consume first, or None when empty."""
        price = self.best_price()
        return None if price is None else self._levels[price]

    def level_at(self, price: float) -> PriceLevel | None:
        """The level at exactly `price` -- which must already be quantized."""
        return self._levels.get(price)

    def levels(self) -> list[PriceLevel]:
        """Every non-empty level, best price first."""
        return [
            self._levels[price]
            for price in sorted(self._levels, reverse=self.side is Side.BID)
        ]

    @property
    def total_volume(self) -> int:
        """Unfulfilled quantity resting on this side, across all prices."""
        return sum(level.volume for level in self._levels.values())

    def volume_at(self, price: float) -> int:
        """Unfulfilled quantity an opposite order priced at `price` could take.

        `book-queries` defines this as the marketable question, not the
        exact-price one: bids priced at or above `price` when this is the bid
        side, asks priced at or below it when this is the ask side, and 0 when
        nothing qualifies. `price` must already be quantized.
        """
        if self.side is Side.BID:
            return sum(
                level.volume for level in self._levels.values() if level.price >= price
            )
        return sum(
            level.volume for level in self._levels.values() if level.price <= price
        )

    # -- writing -----------------------------------------------------------

    def add(self, order: Order) -> PriceLevel:
        """Rest `order` at the back of its price level, creating the level if new.

        The caller has already stamped `order.priority`; appending is what
        makes the stamp the queue position. An order with nothing left to
        trade never reaches the book.
        """
        if order.price is None:
            raise InvalidOrder("a market order never rests")
        if order.side is not self.side:
            raise InvalidOrder(
                "order %r is a %s, not a %s" % (order.idNum, order.side, self.side)
            )
        if order.remaining <= 0:
            raise InvalidOrder("order %r has nothing left to rest" % (order.idNum,))

        level = self._levels.get(order.price)
        if level is None:
            level = self._levels[order.price] = PriceLevel(order.price)
            heapq.heappush(self._best, order.price * self._best_sign)
            heapq.heappush(self._worst, order.price * -self._best_sign)
        level.append(order)
        return level

    def remove(self, order: Order) -> bool:
        """Take `order` out of the book; False if it was not resting.

        Used by cancellation and by a modify that has to re-queue. It does not
        touch the order's own state -- whether it left because it was
        cancelled or because it is being put back is the caller's business.
        """
        if order.price is None:
            return False
        level = self._levels.get(order.price)
        if level is None or not level.discard(order):
            return False
        if not level:
            self._drop(order.price)
        return True

    def fill(self, order: Order, qty: int) -> None:
        """Record that `qty` of resting `order` traded.

        The one sanctioned way to reduce a resting order's remaining quantity:
        it advances `fulfilled`, keeps the level's cached volume honest, and
        takes the order out of the book once it has nothing left. Cumulative
        `value` and `commission` are the ledgers' business, not this one's.
        """
        if qty <= 0:
            raise InvalidOrder("fill quantity must be positive, got %r" % (qty,))
        if qty > order.remaining:
            raise InvalidOrder(
                "cannot fill %d of order %r: only %d remains"
                % (qty, order.idNum, order.remaining)
            )
        order.fulfilled += qty
        level = None if order.price is None else self._levels.get(order.price)
        if level is not None and order in level:
            level.volume -= qty
            if order.remaining <= 0:
                level.discard(order)
                if not level:
                    self._drop(order.price)

    def resize(self, order: Order, qty: int) -> None:
        """Change a resting order's quantity without moving it in the queue.

        The passive half of modify (`order-lifecycle`: a pure quantity
        decrease keeps its place). A decrease down to the already-fulfilled
        amount leaves the order with nothing to trade, so it leaves the book;
        the caller still reports it as modified, not cancelled.
        """
        if qty < order.fulfilled:
            raise InvalidOrder(
                "quantity %r is below order %r's fulfilled %d -- clamp first"
                % (qty, order.idNum, order.fulfilled)
            )
        level = None if order.price is None else self._levels.get(order.price)
        resting = level is not None and order in level
        delta = qty - order.qty
        order.qty = qty
        if not resting:
            return
        assert level is not None
        level.volume += delta
        if order.remaining <= 0:
            level.discard(order)
            if not level:
                self._drop(order.price)

    # -- internals ---------------------------------------------------------

    def _drop(self, price: float | None) -> None:
        """Forget an emptied level, leaving its prices in the heaps as stale."""
        if price is not None:
            del self._levels[price]
            self._compact()

    def _peek(self, heap: list[float], sign: float) -> float | None:
        """Top of `heap` after discarding prices whose level is gone."""
        levels = self._levels
        while heap:
            price = heap[0] * sign
            if price in levels:
                return price
            heapq.heappop(heap)
        return None

    def _compact(self) -> None:
        """Rebuild the heaps when stale prices come to outnumber live ones.

        Lazy deletion only pays for itself if the stale entries eventually
        surface, and a price that churns deep in the book never does. This
        bounds the heaps at a small multiple of the live level count for an
        O(L) rebuild that happens O(1/L) of the time.
        """
        live = len(self._levels)
        if len(self._best) <= 2 * live + 16:
            return
        self._best = [price * self._best_sign for price in self._levels]
        self._worst = [price * -self._best_sign for price in self._levels]
        heapq.heapify(self._best)
        heapq.heapify(self._worst)


@dataclass(slots=True)
class InstrumentBook:
    """One instrument: its two sides, its currency, and its last trade price.

    `last_price` is reporting state (`book-queries`: "reporting, not matching
    state"). Nothing in matching may read it -- IOC market orders price at the
    maker, so the book never needs a reference price to match against.
    """

    symbol: str
    currency: str | None = None
    last_price: float | None = None
    bids: BookSide = field(default_factory=lambda: BookSide(Side.BID))
    asks: BookSide = field(default_factory=lambda: BookSide(Side.ASK))

    def side(self, side: Side | str) -> BookSide:
        """The named side of this book."""
        return self.bids if _as_side(side) is Side.BID else self.asks

    def opposite(self, side: Side | str) -> BookSide:
        """The side an order of `side` matches against."""
        return self.asks if _as_side(side) is Side.BID else self.bids


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------


class OrderBook:
    """The in-memory book: instruments, traders, orders, and the counters.

    This class owns the state and the invariants; the matching loop, cancel
    and modify, and the ledgers are methods added on top of it. What is here
    is what they all need:

    configuration
        `configure_instrument`, `configure_trader`, `clipPrice`
    the store
        `create_order`, `order`, `orders`
    the book
        `book`, `rest`, `snapshot`, and the `get*` queries
    the counters
        `next_priority`, `next_trade_id`, `next_seq`
    the sink
        `recording`, `emit`, `close`

    Construction emits `SessionStarted`, so a stream always opens with the
    tick size a replay needs in order to quantize the same way.
    """

    valid_types: Final = tuple(str(member) for member in OrderType)
    valid_sides: Final = tuple(str(member) for member in Side)

    def __init__(
        self,
        tick_size: float = DEFAULT_TICK_SIZE,
        sink: EventSink | None = None,
        timestamp: float = 0.0,
    ) -> None:
        self._tick = _tick_decimal(tick_size)
        self.tick_size = tick_size
        #: The engine clock. Advanced by one per non-replay operation; set
        #: from the caller's value on the data-replay path. Recorded data.
        self.time: float = timestamp

        self._orders: dict[int, Order] = {}
        self._books: dict[str, InstrumentBook] = {}
        self._traders: dict[int, Trader] = {}

        self._next_idNum = 1
        self._next_priority = 1
        self._next_trade_id = 1
        self._next_seq = 0

        self._sink = sink
        self.emit(
            SessionStarted(
                seq=self.next_seq(), timestamp=self.time, tick_size=tick_size
            )
        )

    def __repr__(self) -> str:
        return "OrderBook(tick_size=%r, instruments=%d, orders=%d)" % (
            self.tick_size,
            len(self._books),
            len(self._orders),
        )

    # -- prices ------------------------------------------------------------

    def clipPrice(self, price: float) -> float:
        """This book's tick grid applied to `price` (legacy name, kept)."""
        return _quantize(price, self._tick)

    quantize = clipPrice

    # -- configuration -----------------------------------------------------

    def configure_instrument(self, symbol: str, currency: str | None = None) -> None:
        """Declare an instrument and the currency it settles in.

        A sink cannot book the cash leg of a trade without the currency, so
        this is a recorded event and not merely local state.
        """
        book = self.book(symbol)
        book.currency = currency
        if self.recording and currency is not None:
            self.emit(
                InstrumentConfigured(
                    seq=self.next_seq(),
                    timestamp=self.time,
                    symbol=symbol,
                    currency=currency,
                )
            )

    def configure_trader(
        self,
        tid: int,
        name: str = "",
        allow_self_matching: bool = False,
        commission_min: float = 0.0,
        commission_max_percnt: float = 0.0,
        commission_per_unit: float = 0.0,
    ) -> Trader:
        """Set a trader's commission schedule and self-matching flag.

        Re-callable: the last call before a given `seq` wins, which is what a
        replayer reconstructs from the stream.
        """
        trader = Trader(
            tid=tid,
            name=name or str(tid),
            allow_self_matching=allow_self_matching,
            commission_min=commission_min,
            commission_max_percnt=commission_max_percnt,
            commission_per_unit=commission_per_unit,
        )
        self._traders[tid] = trader
        if self.recording:
            self.emit(
                TraderConfigured(
                    seq=self.next_seq(),
                    timestamp=self.time,
                    tid=trader.tid,
                    name=trader.name,
                    allow_self_matching=trader.allow_self_matching,
                    commission_min=trader.commission_min,
                    commission_max_percnt=trader.commission_max_percnt,
                    commission_per_unit=trader.commission_per_unit,
                )
            )
        return trader

    def trader(self, tid: int) -> Trader:
        """`tid`'s configuration, defaulted (no commission, no self-matching).

        An unconfigured trader is not an error: the schedule is a policy the
        host sets, and its absence means zero, not "reject the order".
        """
        trader = self._traders.get(tid)
        if trader is None:
            trader = self._traders[tid] = Trader(tid=tid, name=str(tid))
        return trader

    # -- the store ---------------------------------------------------------

    def order(self, idNum: int) -> Order | None:
        """The order with `idNum`, resting or finished, or None if none has it."""
        return self._orders.get(idNum)

    def require_order(self, idNum: int) -> Order:
        """`order`, but an unknown identifier raises rather than returning None.

        `order-lifecycle`: cancel or modify against an identifier no order has
        raises a library exception and leaves the book unchanged.
        """
        order = self._orders.get(idNum)
        if order is None:
            raise UnknownOrder("no order with idNum %r" % (idNum,))
        return order

    def orders(self) -> Iterator[Order]:
        """Every order the engine has accepted, in acceptance order."""
        return iter(self._orders.values())

    def create_order(
        self,
        tid: int,
        instrument: str,
        side: Side | str,
        order_type: OrderType | str,
        qty: int,
        price: float | None = None,
        idNum: int | None = None,
        timestamp: float | None = None,
    ) -> Order:
        """Validate a submission, register it, and stamp its priority.

        This is acceptance and nothing more: the order is in the store and has
        its identifier, its quantized working price and its arrival stamp, but
        it is not in the book and has not matched. The caller matches it and
        rests whatever survives.

        `Accepted` is emitted here, as the last act, so that the event stream
        cannot show an order trading before it exists and a rejected
        submission cannot emit anything at all -- every raise below happens
        before any state changes.

        `timestamp` and `idNum` are the data-replay path: supplied, they are
        used as given; omitted, the engine assigns them. A supplied identifier
        that any order already carries is refused (`DuplicateOrderID`), and
        one above the counter pushes the counter past it, so identifiers stay
        unique across a reload without a separate restore step.
        """
        side = _as_side(side)
        order_type = _as_order_type(order_type)

        if not isinstance(qty, int) or isinstance(qty, bool):
            raise InvalidOrder("quantity must be an integer, got %r" % (qty,))
        if qty <= 0:
            raise InvalidOrder("quantity must be positive, got %r" % (qty,))

        if order_type is OrderType.LIMIT:
            if price is None:
                raise InvalidOrder("a limit order needs a price")
            working_price: float | None = self.clipPrice(price)
        else:
            working_price = None

        idNum = self._assign_idNum(idNum)
        if timestamp is None:
            self.time += 1
        else:
            self.time = timestamp

        order = Order(
            idNum=idNum,
            tid=tid,
            instrument=instrument,
            side=side,
            order_type=order_type,
            price=working_price,
            qty=qty,
            timestamp=self.time,
            priority=self.next_priority(),
        )
        self._orders[idNum] = order
        self.book(instrument)
        if self.recording:
            self.emit(
                Accepted(
                    seq=self.next_seq(),
                    timestamp=order.timestamp,
                    idNum=order.idNum,
                    tid=order.tid,
                    instrument=order.instrument,
                    side=order.side,
                    order_type=order.order_type,
                    price=order.price,
                    qty=order.qty,
                    priority=order.priority,
                )
            )
        return order

    # -- the book ----------------------------------------------------------

    def book(self, instrument: str) -> InstrumentBook:
        """The book for `instrument`, created empty on first mention."""
        book = self._books.get(instrument)
        if book is None:
            book = self._books[instrument] = InstrumentBook(symbol=instrument)
        return book

    def instruments(self) -> Iterator[str]:
        """Every instrument this engine has a book for."""
        return iter(self._books)

    def rest(self, order: Order) -> None:
        """Put `order` into the book at the back of its price level.

        Called with whatever a submission or a modification has left over. A
        market order never gets here (`order-lifecycle`: it is
        immediate-or-cancel), and neither does a fully filled one.
        """
        if order.order_type is not OrderType.LIMIT:
            raise InvalidOrder("order %r is not a limit order" % (order.idNum,))
        if order.cancelled:
            raise InvalidOrder("order %r is cancelled" % (order.idNum,))
        self.book(order.instrument).side(order.side).add(order)

    def snapshot(self, instrument: str, side: Side | str) -> tuple[Order, ...]:
        """Resting orders on `side`, in matching priority order.

        `book-queries` requires the snapshot to list every resting order
        exactly once and to agree with the price and volume queries taken at
        the same moment -- which it does by being a view of the same levels
        rather than a separate record.
        """
        return tuple(self.book(instrument).side(side))

    # -- the read side (`book-queries`) ------------------------------------

    def getBestBid(self, instrument: str) -> float | None:
        """The highest resting bid price, or None if no bid rests."""
        return self.book(instrument).bids.best_price()

    def getWorstBid(self, instrument: str) -> float | None:
        """The lowest resting bid price, or None if no bid rests."""
        return self.book(instrument).bids.worst_price()

    def getBestAsk(self, instrument: str) -> float | None:
        """The lowest resting ask price, or None if no ask rests."""
        return self.book(instrument).asks.best_price()

    def getWorstAsk(self, instrument: str) -> float | None:
        """The highest resting ask price, or None if no ask rests."""
        return self.book(instrument).asks.worst_price()

    def getVolumeAtPrice(self, instrument: str, side: Side | str, price: float) -> int:
        """How much of `side` an opposite order priced at `price` could take.

        The query price is quantized first: a price off the grid is not a
        price this book can hold, so asking about it means asking about the
        grid point it names.
        """
        return self.book(instrument).side(side).volume_at(self.clipPrice(price))

    def getLastPrice(self, instrument: str) -> float | None:
        """The most recent trade price, or None before the first trade."""
        return self.book(instrument).last_price

    def setLastPrice(self, instrument: str, price: float) -> None:
        """Record a trade price as the instrument's last (reporting only)."""
        self.book(instrument).last_price = price

    # -- counters ----------------------------------------------------------

    def next_priority(self) -> int:
        """The next arrival stamp. Advances whether or not a sink is attached."""
        priority = self._next_priority
        self._next_priority += 1
        return priority

    def next_trade_id(self) -> int:
        """The next trade identifier."""
        trade_id = self._next_trade_id
        self._next_trade_id += 1
        return trade_id

    def next_seq(self) -> int:
        """The next position in the event stream. Matching must never read it."""
        seq = self._next_seq
        self._next_seq += 1
        return seq

    # -- the sink ----------------------------------------------------------

    @property
    def recording(self) -> bool:
        """Is a sink attached?

        Test this before *building* an event: with no sink, an unrecorded
        engine should not pay to construct one, and nothing observes the gaps
        it leaves in `seq`.
        """
        return self._sink is not None

    def emit(self, event: Event) -> None:
        """Hand one event to the sink, or drop it when there is none."""
        if self._sink is not None:
            self._sink.consume(event)

    def close(self) -> None:
        """End the session, flushing the sink if it has anything buffered."""
        if self._sink is not None:
            close_sink(self._sink)

    # -- internals ---------------------------------------------------------

    def _assign_idNum(self, idNum: int | None) -> int:
        """Allocate or accept an identifier, keeping it unique for all time.

        The store is never pruned, so `in self._orders` is a lifetime test and
        not merely a test of what is resting. The high-water mark is what
        survives a reload: replay supplies every identifier it saw, each one
        pushes the counter past itself, and the first order submitted
        afterwards lands above all of them (lob-7e7).
        """
        if idNum is None:
            idNum = self._next_idNum
            self._next_idNum += 1
            return idNum
        if not isinstance(idNum, int) or isinstance(idNum, bool):
            raise InvalidOrder("idNum must be an integer, got %r" % (idNum,))
        if idNum in self._orders:
            raise DuplicateOrderID("idNum %r is already in use" % (idNum,))
        if idNum >= self._next_idNum:
            self._next_idNum = idNum + 1
        return idNum


def _as_side(side: Side | str) -> Side:
    """`side` as a `Side`, or an `InvalidOrder` naming what was allowed."""
    try:
        return Side(side)
    except ValueError:
        raise InvalidOrder(
            "side must be one of %r, got %r" % (OrderBook.valid_sides, side)
        ) from None


def _as_order_type(order_type: OrderType | str) -> OrderType:
    """`order_type` as an `OrderType`, or an `InvalidOrder`."""
    try:
        return OrderType(order_type)
    except ValueError:
        raise InvalidOrder(
            "order type must be one of %r, got %r" % (OrderBook.valid_types, order_type)
        ) from None
