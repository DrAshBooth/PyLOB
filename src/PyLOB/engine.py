"""The in-memory matching engine: the book, the crossing loop, the ledgers.

ADR-0001 moved matching out of SQLite and into this module. Everything an
order's life touches is here -- the structures, the identifier rules, the
price grid, the crossing loop, cancel and modify, and the running balances --
because one code path owning eligibility, allocation and accounting is the
whole point of the move.

No SQL. This module must never import `sqlite3`: a sink is optional and lives
behind `events.EventSink`, so the engine has to be complete without one. It
follows that a caller with no sink still learns what happened: a submission
returns its `Trade` list, and `Filled` -- the recorder's view of the same
executions -- is built only when someone is recording.

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
but the heap now holds that price twice, and duplicates are the one thing
lazy deletion has to be careful about. There are exactly two ways to make
one: `add` re-creating a level whose stale entry has not surfaced yet, and
`_compact` rebuilding from the live levels while `match_levels` is holding a
price it popped, which the walk then pushes back on its way out.

Neither costs accuracy -- a duplicate is the same price value -- provided the
same *level* is never handed to one match walk twice, which is what the
`walked` set in `match_levels` guarantees by dropping a copy on sight. What
they cost is memory, and `_compact` is what bounds it: it rebuilds when
*either* heap outgrows the live level count, because `_worst` is read only by
the reporting queries and so sheds a stale entry almost never (lob-n3n: a
gate on `_best` alone leaves `_worst` growing one entry per level creation
for the life of the process).

Costs, in the number of price levels L on a side and orders N in a level:

    best / worst price          O(1) amortized (O(log L) when stale entries pop)
    insert into an old level    O(1)
    insert into a new level     O(log L)
    cancel                      O(1)
    fill the front of the book  O(1) per order, O(log L) per exhausted level
    step over k gated orders    O(k) per level, O(1) when the whole level is
                                the taker's own (lob-rp4)
    volume at price             O(L)   -- levels, not orders: `volume` is cached
    depth ladder                O(L log L) -- levels again; the sort is the cost
    snapshot                    O(L log L + N)

Two heaps rather than one sorted structure because matching only ever asks for
one end. `getWorstBid`/`getWorstAsk` are reporting queries (`book-queries`)
and deserve to be cheap, but they do not deserve to slow down insertion. A
sorted list of prices would answer both ends and range scans in one structure,
but pays an O(L) memmove on every new level; a red-black tree pays more code
than the research scale is worth (the inmemory-engine design.md, decision 1,
rejected the 2013 implementation's tree for the same reason).

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
that 0.05 is a twentieth and not 0.05000000000000000277, and what comes back
is the nearest double to the exact decimal multiple.

It is the price/tick *ratio* that has to fit the 40 significant digits the
quotient is carried at, so the usable range is "prices within forty digits of
the tick" and a wider ratio raises `InvalidOrder` naming it (`quantize_price`).

What a submission has to be
---------------------------

One gate, run before the clock moves or an identifier is allocated, because
`order-lifecycle` requires an invalid submission to raise a library exception
rather than half-apply. A price is a finite, strictly positive real number
(never a `bool`, which is an `int` in Python); a quantity is a positive `int`
no larger than `MAX_QTY`; `qty * price` has to be finite; and a market order
that carries a price is refused rather than quietly ignoring it.

Positivity and the `2**53` ceiling are decisions rather than readings of the
specs, and both cost a book to learn: `_check_price`, `_check_qty` and
`_check_notional` each say what their own rule costs to get wrong.
`create_order` is where the gate runs.

Matching
--------

`submit` is one submission end to end: accept, cross, then rest or cancel the
remainder. The crossing loop walks the opposite side best-price-first and each
level front-to-back, so (price, priority) is the entirety of the matching
order, and every execution prices at the **maker's** limit -- the resting
order named terms and the arriving one took them.

Two rules bend the walk, and both are book state rather than special cases:

- a market order carries no price, crosses every level, and is cancelled the
  moment liquidity runs out (`order-lifecycle`: immediate-or-cancel, never
  rests) -- `Order.resting` makes that structural rather than remembered;
- a resting order of the taker's own trader is **skipped, not consumed**,
  unless that trader has `allow_self_matching`. It keeps its place, its
  quantity and its `fulfilled`, and the walk carries on past it
  (`trader-balances`). `BookSide.match_levels` therefore hands levels out one
  at a time and puts back what the caller did not empty, so a level nobody
  was allowed to trade with does not stop the walk at the top of the book.

Stepping over a gated order is where the walk pays for the dict: `match` holds
*one* cursor over the level rather than re-indexing it, which is what keeps a
walk past k of the taker's own orders from costing O(k^2)
(`docs/engine-review-2026-08.md`, lob-rp4), and a level whose orders all belong
to one trader answers that case in a single comparison. A modify that changes
price re-enters the same loop as a taker, which is why every fill goes through
`BookSide.fill` on *both* sides: the taker may itself be in the book, and its
level's cached volume has to stay honest either way.

The ledgers
-----------

Commissions and balances are computed here, not in a sink (design.md decision
3): online PnL is a required feature and a sink is optional, so `order.
commission` and `balance()` cannot depend on one being attached.
`commission_for` is the one implementation of the formula and states it.

Balances track; they do not gate. No margin check, no sufficient-funds check,
negative balances permitted and recorded on both sides. That absence is a
requirement of `trader-balances`, not an omission: a well-meaning funds check
here would break short-selling research workloads. A trade's four movements
are `_settle`'s, and the arithmetic every consumer shares is stated once in
`PyLOB.events`.

What the stream records
-----------------------

`recording-sink` requires the persisted stream to be sufficient to reconstruct
the book *and the reporting values*, so every public call that changes engine
state has to leave something a replayer can re-issue. Two mechanisms keep that
true.

**Most operations emit.** `submit`, `cancelOrder`, `modifyOrder`,
`processOrder`, `configure_instrument` and `configure_trader` each emit the
event that describes what they did, and a replayer re-issues those events as
the same calls (`events`, "Replay"). `cancel` and `modify` -- the same two
operations addressed by identifier and keyword -- emit nothing of their own:
they delegate, so there is one behaviour under two names rather than two
behaviours to keep in step.

**A mutation no event can express is refused, not performed quietly.**
`configure_instrument(symbol, None)` used to withdraw an instrument's currency
and `setLastPrice` used to overwrite the last-trade price, both silently, both
leaving the engine and its own log describing different sessions; each raises
now and says why where it is defined. Shrinking the mutation set is the fix
available in this module, because growing the emission set instead means a new
event kind -- a change to the stream format *and* to every sink that folds it.

**What is left is the decomposition `submit` is built from** --
`create_order`, `rest`, `match`, `emit`, `next_priority`, `next_trade_id`,
`next_seq`. They are public, and used as a *partial* sequence they are not
replay-coherent: `create_order` emits `Accepted` and stops, while a replayer
turns an `Accepted` into a whole `submit`, so a session that accepted an order
without matching it replays as one that matched it -- inventing trades that
never happened. Each says so in its own docstring, and
`tests/test_emission_coverage.py` fails on any new public method that changes
state without emitting and without being listed there. `match` and `rest` go
further and refuse outright an `Order` this engine never accepted, that
failure having been live rather than merely a replay mismatch (`match`).

Identifiers of the form `lob-d6i` name findings in this repository's issue
tracker, which an installed copy of the package cannot open -- nothing here
depends on them, because every docstring carries the substance inline; the
citations that matter to a reader are the ADRs under `docs/adr/` and the two
2026-08 review documents under `docs/`.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException
from functools import lru_cache
from math import isfinite
from numbers import Real
from typing import Any, Final, NamedTuple

from .events import (
    Accepted,
    Cancelled,
    CancelReason,
    Event,
    EventSink,
    Filled,
    InstrumentConfigured,
    Modified,
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
    "commission_for",
    "Order",
    "Trade",
    "Trader",
    "PriceLevel",
    "BookSide",
    "InstrumentBook",
    "OrderBook",
    "DEFAULT_TICK_SIZE",
    "MAX_QTY",
]

DEFAULT_TICK_SIZE: Final = 0.0001

#: The largest quantity a submission may carry: 2**53, the largest integer a
#: float represents exactly. Past it an order's `value` and its trader's
#: balance stop being able to hold the number they are given, and `qty *
#: price` starts overflowing inside `_execute` rather than at the gate.
MAX_QTY: Final = 2**53

#: `MAX_QTY` at this price is still a finite float, so `qty * price` needs
#: checking only above it: 2**53 * 1e292 is 9.0e307 against a largest float of
#: 1.8e308. One comparison on the accept path, in place of a multiplication
#: that can only matter for a price no market has.
_NOTIONAL_GUARD: Final = 1e292

_INF: Final = float("inf")

#: How many quantized prices a book keeps (`OrderBook.quantize`). Big enough
#: to hold the grid of any book a research workload builds around a touch;
#: small enough that a book that walks a wide grid forever cannot grow one
#: entry per price it ever saw.
_GRID_MEMO_SIZE: Final = 8192


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
    """A submission, modification or configuration the engine will not accept.

    Also a `ValueError`, because that is what a caller who has never read
    these docs will already be catching around bad input.

    It covers two kinds of refusal: malformed input, and a call whose effect
    the event stream could not express (module docstring, "What the stream
    records").
    """


class DuplicateOrderID(InvalidOrder):
    """An externally supplied identifier that some order already carries.

    Rejected rather than accepted-and-disambiguated: `order-lifecycle` says
    operations addressing an identifier affect at most one order, and the only
    way to keep that promise is to never let a second order have the id.
    """


class UnknownOrder(PyLOBError, LookupError):
    """An operation named an order this engine does not have.

    Either by identifier -- cancel or modify against one no order carries --
    or by object: `match` and `rest` refuse an `Order` the engine never
    accepted, however well formed it is, because trading or resting one puts
    liquidity in the book that no event describes (lob-9fu).
    """


# --------------------------------------------------------------------------
# tick quantization
# --------------------------------------------------------------------------

#: Wide enough that the intermediate quotient never rounds: a price/tick ratio
#: needs 40 significant digits before this loses anything, and no market has a
#: grid that deep.
_CTX: Final = Context(prec=40, rounding=ROUND_HALF_EVEN)
_ONE: Final = Decimal(1)


def _positive_real(value: Any, what: str) -> float:
    """`value` as a positive finite float, or an `InvalidOrder` saying why not.

    The one shape test behind both the price gate and the tick gate: the two
    want exactly the same thing of a number, and while they asked separately a
    string tick raised `TypeError` and `True` raised
    `decimal.InvalidOperation` -- neither the library exception
    `order-lifecycle` promises. `bool` is refused before anything else, since
    `True` is an `int` in Python and would pass every numeric test below.
    Anything else that is a real number is taken -- a `numpy` scalar or a
    `Fraction` counts -- and converted once, so what the engine stores and
    quantizes is an ordinary float rather than whatever the caller's array
    library hands back from arithmetic.

    The refusal names int and float rather than the wider set it accepts,
    because the value that lands here is nearly always a `Decimal` -- which is
    a real number by any ordinary reading and is not a `numbers.Real`, so
    "must be a real number" reads as a contradiction of the thing in front of
    you. What the caller needs is the conversion, so the message is that.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Real)):
        raise InvalidOrder(
            "%s must be an int or float (pass `float(%s)`), got %r"
            % (what, what.replace(" ", "_"), value)
        )
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        # An int too large for a float: 10**400 is a real number and still
        # not one this engine can hold.
        raise InvalidOrder("%s is out of range: %r" % (what, value)) from exc
    if not isfinite(number):
        raise InvalidOrder("%s must be finite, got %r" % (what, value))
    if number <= 0:
        raise InvalidOrder("%s must be positive, got %r" % (what, value))
    return number


@lru_cache(maxsize=256)
def _tick_cached(tick_size: float) -> Decimal:
    """`_tick_decimal`'s memo, over an already-validated float."""
    return Decimal(str(tick_size))


def _tick_decimal(tick_size: float) -> Decimal:
    """The tick as an exact decimal, via `str` so 0.05 is a twentieth.

    `Decimal(0.05)` is 0.05000000000000000277..., the double; `Decimal("0.05")`
    is the number the caller meant. Cached because a book quantizes on every
    submission and the tick never changes -- and validated *outside* the cache,
    because `lru_cache` raises `TypeError` on an unhashable argument before the
    body it is wrapping ever gets to reject it.
    """
    return _tick_cached(_positive_real(tick_size, "tick size"))


def _quantize(price: float, tick: Decimal) -> float:
    """`price` snapped to the nearest multiple of an already-decimalized tick.

    Three `decimal` operations and two conversions, which makes it the most
    expensive thing on the accept path; `OrderBook.quantize` keeps the
    answers, and this is what a miss costs. A ratio too wide for `_CTX` raises
    `InvalidOrder` rather than leaking `decimal.InvalidOperation`, which is
    not an error a caller catching this library's own would catch.
    """
    try:
        multiples = _CTX.divide(Decimal(str(price)), tick).quantize(
            _ONE, rounding=ROUND_HALF_EVEN, context=_CTX
        )
        # `+ 0.0` only to turn a -0.0 back into 0.0: they compare and hash
        # alike, so it is cosmetic, but a book should not report a negative
        # zero price.
        return float(_CTX.multiply(multiples, tick)) + 0.0
    except DecimalException as exc:
        raise InvalidOrder(
            "price %r does not quantize on a tick of %s: the price/tick ratio "
            "needs more than the %d significant digits the grid carries"
            % (price, tick, _CTX.prec)
        ) from exc


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

    Usable range: the price/tick *ratio* is carried at 40 significant digits,
    so 1e-40 is a legal tick that no ordinary price will quantize against.
    A ratio wider than that raises `InvalidOrder`.
    """
    return _quantize(price, _tick_decimal(tick_size))


# --------------------------------------------------------------------------
# what the book holds
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Order:
    """One order, from acceptance until the end of the session.

    **Yours to read, not to write.** This object is the engine's live record,
    not a copy: `order.qty = 5` on an order the book is holding leaves its
    price level's cached `volume` describing the old quantity, and emits no
    event, so the book, the queries and the log disagree from then on.

    `price` and `priority` are worse than that, because they are not merely
    stored on the order -- they are the book's index into itself, and assigning
    to either desynchronizes it permanently (`BookSide`). An order whose
    `price` no longer names the level it is filed under is looked up in the
    wrong place by everything that follows. `BookSide.remove` finds nothing,
    which makes `cancelOrder` set the flag and return while leaving the order
    resting and its quantity counted -- a cancelled order contributing volume
    for the rest of the session. `BookSide.fill` finds no level either, so the
    order trades down to nothing without leaving the queue, and the next taker
    to reach it asks for a fill of zero: `InvalidOrder` out of `_execute`,
    after the taker's own `Accepted` and the fills before it are already in
    the stream and already settled. Nothing rejects the assignment, and no
    later operation repairs it.

    Change a resting order through `modifyOrder` or `cancelOrder`, which is
    the same request routed through `BookSide` and recorded -- and which is
    `remove`, then the change, then `add`, the one sequence that leaves the
    index and the order agreeing.

    Mutable and identity-addressed: the book, the level, and `_orders` all
    hold the same object, so a fill updates one place. It outlives its stay in
    the book -- a filled or cancelled order stays in the store and keeps
    answering for its `fulfilled` and `commission`.

    Freezing it is therefore not a tidy-up. It is a public type, and every
    write path in this module assigns to it (`BookSide.fill` advances
    `fulfilled`, `_charge` accumulates `value` and `commission`, a reprice
    assigns `price`, `qty` and `priority` together), so a frozen `Order` is a
    change to the public surface and to the accept path ADR-0002 measures.
    Recorded here rather than done, as `OrderBook.book` records its own.

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
    #: Quantity filled so far -- a count, not the `filled` flag beside it.
    #: `fulfilled` is how much has traded; `filled` is whether all of it has.
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
        """Is this order eligible to rest: limit, not cancelled, not filled?

        Derived rather than stored, and false for a market order by
        construction rather than by the matching loop remembering to cancel
        the remainder: `order-lifecycle` says a market order never rests, and
        a derived flag cannot be caught mid-processing saying otherwise.

        Eligibility and membership coincide between operations, and only
        between them. A limit order reads `resting` from the moment
        `create_order` returns, and `submit` does not put it in a level until
        it has finished crossing, so in between it is in no level and says it
        rests. Every submission passes through that state, and the only
        observer who can be there is a sink, which `emit` calls from inside
        the operation (`OrderBook`) -- and which can leave the order there for
        good: a sink that raises out of the walk aborts the submission with
        its fills settled, its `Accepted` emitted, and the taker answering
        `resting` while no level holds it. `snapshot` is the membership
        question.
        """
        return (
            self.order_type is OrderType.LIMIT
            and not self.cancelled
            and self.fulfilled < self.qty
        )


class Trade(NamedTuple):
    """One execution, as reported to whoever caused it.

    The engine's answer to "what did my order just do", returned from `submit`
    and `modifyOrder` and built whether or not a sink is attached. `Filled` is
    the recorder's view of the same execution and carries the accounting a
    sink needs; this carries what a caller needs to see a trade happen, and
    the orders it names answer for the rest.

    `price` is the maker's limit and `qty` this execution alone -- neither is
    a cumulative total.

    A `NamedTuple` and not the frozen dataclass it used to be (ADR-0004, for
    the measurement and the rejected alternatives). What that changed for a
    caller is the type's width, not its guarantees: a `Trade` is still
    immutable and hashable, and it is now also a sequence -- it unpacks,
    indexes and compares equal to a plain tuple of its fields, while
    `dataclasses.asdict` and `replace` no longer apply to it (`_asdict` and
    `_replace` do). Field *order* is therefore part of the public surface in a
    way it was not before.
    """

    trade_id: int
    timestamp: float
    instrument: str
    price: float
    qty: int
    #: Which side was the aggressor; the other side was resting.
    taker_side: Side
    bid_idNum: int
    bid_tid: int
    ask_idNum: int
    ask_tid: int

    @property
    def taker_idNum(self) -> int:
        """The aggressing order's identifier."""
        return self.bid_idNum if self.taker_side is Side.BID else self.ask_idNum

    @property
    def maker_idNum(self) -> int:
        """The resting order's identifier -- the one that set the price."""
        return self.ask_idNum if self.taker_side is Side.BID else self.bid_idNum


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
    #: `percnt`, sic: frozen in the stream format (`TraderConfigured`), so
    #: renaming it would invalidate every recorded session.
    commission_max_percnt: float = 0.0
    commission_per_unit: float = 0.0


def commission_for(trader: Trader, qty: int, value: float) -> float:
    """`min(max_pct * V / 100, max(min_commission, per_unit * Q))`, exactly.

    The `commissions` contract, in one place, over an order's *cumulative*
    filled quantity `qty` and fill value `value` -- never over a single fill.
    An order with no fills owes nothing, whatever the floor says.

    The percentage cap binds ahead of the floor: where the cap comes out below
    `min_commission` the commission is the cap, because `min_commission` is a
    floor on the per-unit charge and not on the order's commission. That is
    the interactive-brokers-style schedule this models, and it is the reason
    the `max` is inside the `min` rather than wrapped around it.

    No rounding, no currency quantization: the contract is the exact value of
    the floating-point formula, and a `round(x, 2)` here would diverge two
    engines while every acceptance test still passed.
    """
    if qty <= 0:
        return 0.0
    per_unit = max(trader.commission_min, trader.commission_per_unit * qty)
    capped = trader.commission_max_percnt * value / 100.0
    return min(capped, per_unit)


#: `PriceLevel.sole_tid` when the level's orders do not all belong to one
#: trader. A sentinel rather than `None` because a caller's `tid` is whatever
#: they say it is, and no sentinel that could also be a `tid` is safe.
_MIXED: Final = object()


class PriceLevel:
    """The FIFO queue of orders resting at one price.

    Backed by a dict keyed on `idNum`: insertion-ordered, so iteration is
    priority order, and O(1) to remove by identifier. `volume` is the sum of
    the members' `remaining`, maintained incrementally so that a volume query
    costs one addition per *level* rather than one per order.

    `sole_tid` is the same trick applied to ownership: the tid every order
    here belongs to, or `_MIXED`. Matching reads it to answer "is this whole
    level the taker's own?" in one comparison, and it is maintained only where
    it is cheap, on `append`. It is therefore conservative in one direction: a
    level that *became* single-owner by losing its other trader's orders reads
    `_MIXED` until it empties. Never the other way, which is the direction
    that would matter.
    """

    __slots__ = ("price", "volume", "sole_tid", "_orders")

    def __init__(self, price: float) -> None:
        self.price = price
        self.volume = 0
        self.sole_tid: Any = _MIXED
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

    def append(self, order: Order) -> None:
        """Put `order` at the back of the queue."""
        orders = self._orders
        if order.idNum in orders:
            raise DuplicateOrderID(
                "order %r is already resting at %r" % (order.idNum, self.price)
            )
        if not orders:
            self.sole_tid = order.tid
        elif self.sole_tid != order.tid:
            self.sole_tid = _MIXED
        orders[order.idNum] = order
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
            sum(level.volume for level in self._levels.values()),
        )

    # -- reading -----------------------------------------------------------

    def best_price(self) -> float | None:
        """Highest bid / lowest ask, or None when the side is empty."""
        return self._peek(self._best, self._best_sign)

    def worst_price(self) -> float | None:
        """Lowest bid / highest ask, or None when the side is empty."""
        return self._peek(self._worst, -self._best_sign)

    def level_at(self, price: float) -> PriceLevel | None:
        """The level at exactly `price` -- which must already be quantized."""
        return self._levels.get(price)

    def levels(self) -> list[PriceLevel]:
        """Every non-empty level, best price first."""
        return [
            self._levels[price]
            for price in sorted(self._levels, reverse=self.side is Side.BID)
        ]

    def crosses(self, level_price: float, price: float | None) -> bool:
        """Would an opposite order priced at `price` trade at `level_price`?

        `None` is a market order's price and crosses everything: it has named
        no terms, so there are none to fail.
        """
        if price is None:
            return True
        if self.side is Side.BID:
            return level_price >= price
        return level_price <= price

    def match_levels(self, price: float | None) -> Iterator[PriceLevel]:
        """Levels an opposite order priced at `price` may trade with, best first.

        The walk lifts each price off the best-price heap before handing over
        its level and puts back the ones whose level survived. That is what
        lets the caller *not* consume a level -- every order in it skipped by
        the self-matching gate -- and still reach the next one: without it the
        same untouchable price would be on top forever.

        Only the caller's own filling removes a level. This yields; it does
        not delete.

        A price already handed out is dropped rather than yielded again. The
        heap can hold a price twice (module docstring, "The structures"), and
        a duplicate is harmless there, where a price is only a price, but not
        *here*: handing the same level over twice restarts the caller's cursor
        into it from the front. So `walked` is the set of prices this walk has
        yielded, and a second copy is popped and forgotten -- which is also
        how the heaps shed the duplicates they accumulate.

        Close the iterator -- a `try`/`finally` around the walk, or exhaust it
        -- or the prices it walked past stay out of the heap until it is
        collected. An open walk is not a paused one: while it is held, the
        levels it has handed out are live in `_levels` and absent from
        `_best`, so `best_price` names the best level *behind* the walk, or
        `None`, which is exactly what a corrupted heap looks like from the
        outside. Nothing distinguishes the two, and only the `finally` above
        repairs it.

        Public by name and internal by contract: `OrderBook.match` is the only
        caller, and it is written around this requirement (its `try`/`finally`
        is the reason it takes no `contextlib.closing`).
        """
        walked: set[float] = set()
        try:
            while True:
                best = self._peek(self._best, self._best_sign)
                if best is None or not self.crosses(best, price):
                    return
                heapq.heappop(self._best)
                if best in walked:
                    continue
                walked.add(best)
                yield self._levels[best]
        finally:
            for level_price in walked:
                if level_price in self._levels:
                    heapq.heappush(self._best, level_price * self._best_sign)

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

        The gate reads whichever heap is longer, and it has to: `_best` is
        drained by every match, so gating on it alone meant the gate never
        opened, and `_worst` -- peeked only by the reporting queries -- grew
        one entry per level creation for the life of the process, until a
        first `getWorst*` call cost hundreds of milliseconds
        (`docs/engine-review-2026-08.md`, lob-n3n).
        """
        live = len(self._levels)
        if max(len(self._best), len(self._worst)) <= 2 * live + 16:
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
    """The engine: instruments, traders, orders, matching, and the ledgers.

    configuration
        `configure_instrument`, `configure_trader`, `quantize`
    operations
        `submit`, `cancelOrder`, `modifyOrder`, and `processOrder` -- the
        legacy dict-quote shape, kept because the public API is a standing
        constraint -- and `cancel`/`modify`, the last two addressed by
        identifier and keyword, which delegate to them
    the store
        `create_order`, `order`, `orders`
    the book
        `book`, `rest`, `match`, `snapshot`, `depth`, the `get*` queries, and
        `print`
    the ledgers
        `balance`, `holdings`
    the counters
        `next_priority`, `next_trade_id`, `next_seq`
    the sink
        `recording`, `emit`, `close`

    The configuration calls and the operations emit, and a replayer re-issues
    what they emitted as the same calls. The store, the book and the counters
    are the decomposition `submit` is built from: each one that changes state
    without emitting says so in its own docstring, under "Not replay-coherent"
    (module docstring, "What the stream records").

    Single-threaded and synchronous throughout. An operation is finished --
    matched, rested or cancelled, balances moved, events emitted -- before it
    returns, so a caller holding a return value is holding settled state.

    **A sink is not that caller.** `emit` hands each event to `consume`
    synchronously, from inside the operation that caused it, so a sink is the
    one observer positioned to see the engine mid-update -- and mid-update the
    book contradicts itself. A match walk lifts the level it is working off
    the best-price heap and leaves it in the level dict, so `getBestAsk`
    answers `None` for a side that `getWorstAsk`, `snapshot` and
    `getVolumeAtPrice` all report as occupied: the disagreement `book-queries`
    requires never to happen. A sink that *writes* is worse. Cancelling a
    maker the walk has stepped over shortens the prefix the skip cursor is
    walked back over, so the walk resumes past a maker the taker was entitled
    to and the taker rests against it -- a book left crossed between two
    traders, which no later operation unwinds. Neither is detected, and
    neither is a state any public call can reach. That a sink reads nothing
    and calls nothing back is therefore a load-bearing contract, stated where
    a sink author reads it (`events.EventSink`) and enforced by nothing.

    One book is one session and there is no `reset()`: an episode is a fresh
    `OrderBook`, which is the intended pattern and the measured-faster one
    (ADR-0006). `close()` flushes the sink and clears nothing.
    """

    valid_types: Final = tuple(str(member) for member in OrderType)
    valid_sides: Final = tuple(str(member) for member in Side)

    def __init__(
        self,
        tick_size: float = DEFAULT_TICK_SIZE,
        sink: EventSink | None = None,
        timestamp: float = 0.0,
    ) -> None:
        """Build an engine. It takes orders as soon as this returns.

        `tick_size` is the price grid: every submitted price is snapped to the
        nearest multiple of it, so an order at 100.03 rests at 100.03 on the
        default 0.0001 grid and at 100.05 on a 0.05 one. It is fixed for this
        book's life -- every resting price and the `quantize` memo assume it
        -- so a different grid means a different `OrderBook`, not a setter.

        `sink` is optional and `None` by default. With no sink the engine
        constructs no events at all (ADR-0002) and still hands every caller its
        `Trade` list; pass one -- a `SQLiteSink`, or any object with a
        `consume(event)` method -- to persist the session. Construction emits
        `SessionStarted`, so a recorded stream always opens with the tick size
        a replay needs in order to quantize the same way.

        `timestamp` seeds the engine clock. The clock starts there and
        advances by one per operation, unless the caller supplies a time of
        their own (`submit(..., timestamp=t)`, `cancelOrder(..., time=t)`,
        `modifyOrder(..., time=t)`), in which case it is set to that value:
        stepping simulated time is simply passing it in, and every event an
        operation emits carries the operation's own stamp. The clock is
        recorded data and **never a sort key** -- matching sorts by (price,
        priority), and two orders may legitimately carry the same timestamp.

        Construction is cheap and reserves nothing, so a fresh engine per
        episode is the intended way to reset state; there is no `reset()`.
        """
        self._tick = _tick_decimal(tick_size)
        self.tick_size = tick_size
        #: `quantize`'s memo: submitted price -> price on this book's grid.
        self._grid: dict[float, float] = {}
        #: The engine clock. Advanced by one per non-replay operation; set
        #: from the caller's value on the data-replay path. Recorded data.
        self.time: float = timestamp

        self._orders: dict[int, Order] = {}
        self._books: dict[str, InstrumentBook] = {}
        self._traders: dict[int, Trader] = {}
        #: (tid, symbol) -> amount, where a symbol is an instrument or a
        #: currency: both are things a trader holds, and holding -5 of either
        #: is a position the ledger records rather than a state it refuses.
        self._balances: dict[tuple[int, str], float] = {}

        self._next_idNum = 1
        self._next_priority = 1
        self._next_trade_id = 1
        self._next_seq = 0

        self._sink = sink
        # Gated like every other emission, so "a sinkless engine constructs no
        # event" (ADR-0002) holds without an exception to remember. One event
        # per session costs nothing measurable; an invariant with an asterisk
        # on it does, the first time someone relies on the plain statement.
        if self.recording:
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

    def quantize(self, price: float) -> float:
        """This book's tick grid applied to `price`.

        Answers are kept, because `_quantize` is the most expensive thing on
        the accept path and a book asks it about the same handful of prices
        all session: a level is a price, and a workload that quotes around a
        touch offers the same ones over and over.

        A plain dict and not an LRU, because at this size the cost that
        matters is the garbage collector's rather than the lookup's: a dict of
        float keys and float values holds nothing for it to trace, where an
        LRU's link nodes are one traced object per entry. Full means cleared,
        not evicted one by one -- the price of a policy cheap enough not to eat
        what it saves. `_grid` is per book because the tick is fixed at
        construction, so a price maps to one grid point for this book's whole
        life and nothing has to invalidate it. Only exact floats are memoized,
        so the memo never has to answer for a key type `dict` would refuse;
        `_check_price` has already made every submitted price one.
        """
        if type(price) is float:
            value = self._grid.get(price)
            if value is None:
                if len(self._grid) >= _GRID_MEMO_SIZE:
                    self._grid.clear()
                value = self._grid[price] = _quantize(price, self._tick)
            return value
        return _quantize(price, self._tick)

    #: The 2013 engine's name for `quantize`, and the same function object, so
    #: the two share one docstring. Kept because removing a public name needs
    #: an ADR (`config.yaml`); `quantize` is the one to write in new code.
    clipPrice = quantize

    # -- configuration -----------------------------------------------------

    def configure_instrument(self, symbol: str, currency: str) -> None:
        """Declare an instrument and the currency it settles in.

        **Call this before trading the instrument.** Until it is called, a
        trade moves only the instrument leg: no cash changes hands, and
        `balance(tid, "USD")` stays 0.0 for every trader for the whole
        session. Nothing raises and nothing warns, so a skipped call reads as
        a strategy that made no money rather than as a book that was never
        told what money is. Trading an unconfigured instrument is a supported
        state, not an error -- the fills are real and the cash leg is
        recoverable from them -- but PnL cannot be read off `balance` until
        the currency is declared.

        A sink cannot book the cash leg without the currency either, so this
        is a recorded event and not merely local state.

        Re-callable: naming a different currency re-denominates the legs of
        every *later* trade, and the event says so, so a replay re-denominates
        at the same point in the stream.

        There is no way to take a currency back. `currency=None` used to be
        accepted and to withdraw the declaration while emitting nothing --
        `InstrumentConfigured` requires a currency, and the emission was
        skipped rather than the assignment -- after which the engine settled
        the instrument leg alone and a sink, still holding the currency it was
        last told about, went on booking the cash leg for every trade. Two
        ledgers, no event between them, and a replay that reproduced the
        sink's (lob-9fu). Refusing it is the fix this module can make on its
        own: the alternative, an `InstrumentConfigured` carrying no currency,
        is a change to the stream format and to every sink that folds it, and
        a library that converts nothing between currencies (`config.yaml`: no
        FX, no cross-instrument netting) has nothing to spend that on.

        **An instrument may not be named after its own currency.** Balances
        are keyed `(tid, symbol)` and a symbol is an instrument *or* a
        currency, one namespace for both (`balance`), so an instrument called
        `"USD"` settling in `"USD"` would post both legs of every trade to the
        same key. A buy of 5 @ 100 would credit 5 and debit 500 and leave a
        single number, -495, that is neither a position nor a cash balance and
        cannot be split back into the two. Refused here, where the collision
        becomes knowable, rather than netted in silence twelve balance
        movements later: this is `config.yaml`'s "an instrument must not be
        named the same as a currency it settles against", enforced.

        The wider collision is still the caller's to avoid, because no single
        call can see it: an instrument named `"USD"` settling in `"EUR"`
        shares the namespace with any *other* instrument that settles in USD,
        and their traders' cash and position net together the same way. Name
        instruments and currencies out of disjoint sets and neither can
        happen.
        """
        if not isinstance(currency, str) or not currency:
            raise InvalidOrder(
                "instrument %r needs a non-empty currency, got %r: a currency "
                "cannot be withdrawn, because no event says it was" % (symbol, currency)
            )
        if symbol == currency:
            raise InvalidOrder(
                "instrument %r cannot settle in itself: balances are keyed "
                "(trader, symbol) over instruments and currencies alike, so "
                "both legs of every trade would post to the same key and net "
                "to a number that is neither a position nor cash" % (symbol,)
            )
        book = self.book(symbol)
        book.currency = currency
        if self.recording:
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

        The input gate is here and nowhere else on the submission path, and
        every check it makes runs before the clock moves (module docstring,
        "What a submission has to be").

        Not replay-coherent on its own. A replayer turns an `Accepted` into a
        whole `submit` -- accept, cross, then rest or cancel -- so a caller
        that accepts an order and stops records a session it did not run: a
        bid left unmatched over a crossing ask replays as a bid that took it,
        trades and all. Use `submit` unless you are going on to `match` and
        `rest` yourself.
        """
        side = _as_side(side)
        order_type = _as_order_type(order_type)

        _check_qty(qty)

        if order_type is OrderType.LIMIT:
            if price is None:
                raise InvalidOrder("a limit order needs a price")
            working_price: float | None = self.quantize(_check_price(price))
            _check_working_price(price, working_price, qty, self.tick_size)
        else:
            if price is not None:
                # Silently ignoring it is how a caller ends up believing a
                # market order was capped -- the same failure the
                # `modifyOrder(price=None)` fix refused to leave standing.
                raise InvalidOrder(
                    "a market order takes no price, got %r: it crosses every "
                    "level and prices at the maker" % (price,)
                )
            working_price = None

        idNum = self._assign_idNum(idNum)
        self._advance(timestamp)

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

    # -- operations --------------------------------------------------------

    def submit(
        self,
        tid: int,
        instrument: str,
        side: Side | str,
        order_type: OrderType | str,
        qty: int,
        price: float | None = None,
        idNum: int | None = None,
        timestamp: float | None = None,
    ) -> tuple[Order, list[Trade]]:
        """One submission end to end: accept, cross, then rest or cancel.

        Returns the order and the executions it caused, in match order. The
        order goes on answering for itself afterwards -- `fulfilled`,
        `commission`, `resting` -- so a caller needs nothing else to see what
        became of it. It is yours to read, not to write: assigning to the
        returned order's `qty` or `price` desynchronizes its level's cached
        volume and emits nothing, so change it through `modifyOrder` or
        `cancelOrder` (`Order`).

        What is left over rests only if it may: a limit order joins the back
        of its price level, a market order's remainder is cancelled, because
        `order-lifecycle` makes market orders immediate-or-cancel.

        Emission order is `Accepted`, then one `Filled` per execution, then
        the `Cancelled` of an IOC remainder -- the order the transitions
        happened in.
        """
        order = self.create_order(
            tid=tid,
            instrument=instrument,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            idNum=idNum,
            timestamp=timestamp,
        )
        trades = self.match(order)
        if order.remaining > 0:
            if order.order_type is OrderType.MARKET:
                self._cancel(order, CancelReason.IOC_REMAINDER)
            else:
                self.rest(order)
        return order, trades

    def processOrder(
        self, quote: dict[str, Any], fromData: bool = False, verbose: bool = False
    ) -> tuple[list[Trade], dict[str, Any]]:
        """`submit` in the legacy dict-quote shape: quote in, `(trades, quote)` out.

        Kept because the public API is a standing constraint. The quote is
        updated in place with the identifier, timestamp and quantized working
        price the engine assigned -- callers read those back out of it.

        `tid`, `instrument`, `side`, `type` and `qty` are required, and a
        missing one is an `InvalidOrder` naming it, never a `KeyError` from
        inside the engine -- the same rule, for the same reason, as
        `modifyOrder`'s (`_quote_field`). Nothing has moved when it raises:
        the quote is read before `submit` is called, and `submit`'s own gate
        runs before the clock does.

        `fromData` is the replay path: the quote's own `idNum` and `timestamp`
        are used as given instead of being assigned, and the quote has to
        carry both. Either one missing -- absent, or present as `None` -- is
        an `InvalidOrder` naming it (`_replay_field`): a replay path that
        invents the identity it was handed the flag to reproduce yields a run
        that no longer traces back to its data, and yields it in silence. The
        legacy engine required both here (lob-0mv).

        `fromData=False` is how a quote gets an engine-assigned identifier and
        stamp, and the flag is per call, so a feed of rows that carry their own
        identity and rows that do not is two calls rather than one.

        The differences a caller porting from the retired SQL engine meets --
        this return shape, the keys that are no longer written back, and the
        refusals that used to be silent no-ops -- are enumerated in
        `docs/migrating-from-the-legacy-engine.md` (ADR-0003).
        """
        order, trades = self.submit(
            tid=_quote_field(quote, "tid"),
            instrument=_quote_field(quote, "instrument"),
            side=_quote_field(quote, "side"),
            order_type=_quote_field(quote, "type"),
            qty=_quote_field(quote, "qty"),
            price=quote.get("price"),
            idNum=_replay_field(quote, "idNum") if fromData else None,
            timestamp=_replay_field(quote, "timestamp") if fromData else None,
        )
        quote["idNum"] = order.idNum
        quote["timestamp"] = order.timestamp
        quote["price"] = order.price
        if verbose:
            _report(trades, order.tid)
        return trades, quote

    def cancelOrder(
        self, side: Side | str | None, idNum: int, time: float | None = None
    ) -> Order:
        """Take one order out of the book at its owner's request.

        Every way of naming an order that does not exist raises rather than
        no-ops (`order-lifecycle`; lob-0rb, where the legacy engine's silent
        no-op is how a caller loses an order it believes it cancelled): an
        unknown identifier, a `side` that is not the order's, an order already
        cancelled, and an order with nothing left to cancel. Nothing changes
        before the last of those checks passes -- not even the clock.

        `side` may be `None` to address the order by identifier alone.

        `cancel` is this operation spelled by keyword -- `cancel(idNum,
        side=..., timestamp=...)` -- and delegates here, so the refusals above
        are the only copy of themselves and either spelling records the same
        `Cancelled`.

        Commission already charged on the filled part stays charged
        (`commissions`: cancelling does not refund), which is why `Cancelled`
        carries no commission field: nothing moves.
        """
        order = self.require_order(idNum)
        if side is not None:
            named = _as_side(side)
            if named is not order.side:
                raise InvalidOrder(
                    "order %r is a %s, not a %s" % (idNum, order.side, named)
                )
        if order.cancelled:
            raise InvalidOrder("order %r is already cancelled" % (idNum,))
        if order.filled:
            raise InvalidOrder("order %r is fully filled, nothing to cancel" % (idNum,))
        self._advance(time)
        self._cancel(order, CancelReason.REQUESTED)
        return order

    def modifyOrder(
        self,
        idNum: int,
        orderUpdate: dict[str, Any],
        time: float | None = None,
        verbose: bool = False,
    ) -> tuple[list[Trade], dict[str, Any]]:
        """Change a resting order's price or quantity (legacy dict shape, kept).

        `orderUpdate` states `side`, `qty` and `price`. All three keys are
        required -- a missing one is an `InvalidOrder` naming it, never a
        `KeyError` from inside the engine. `qty` or `price` given as `None`
        means *leave that one alone*, and emphatically not "become a market
        order": the legacy engine read `price=None` that way and matched a
        limit order at 99 against an ask at 105 while keeping 99 as its stored
        price (lob-crf). An order that named a limit keeps it until someone
        names a different one.

        The rules, all from `order-lifecycle`:

        - a `side` that is not the order's raises, and the order is unchanged;
        - a fully filled order raises, exactly as `cancelOrder` refuses one
          (lob-8r6, where the two disagreed: modify accepted `qty=25` on an
          order of 10 with 10 fulfilled and put the finished order back in the
          book with 15 available and a fresh priority stamp). Wanting quantity
          after an order finishes means wanting a *new* order;
        - a quantity below what is already fulfilled clamps up to it, which
          finishes the order -- reported as `Modified` with the clamped
          quantity, not as a cancellation, and finished by that route is as
          finished as by trading: the next modify raises;
        - a price change or a quantity increase costs time priority: the order
          goes to the back of its (possibly new) level with a fresh stamp. A
          pure quantity decrease keeps its place and its stamp.

        `Modified` is emitted after the change and before any fills the new
        price causes, so a sink applying events in order never sees a fill
        against a stale price.

        `modify` is this operation spelled by keyword -- `modify(idNum,
        qty=..., price=..., timestamp=...)`, returning `(order, trades)` as
        `submit` does -- and delegates here, so every rule above is the only
        copy of itself and either spelling records the same stream.
        """
        order = self.require_order(idNum)
        side = _required(orderUpdate, "side", idNum)
        qty = _required(orderUpdate, "qty", idNum)
        price = _required(orderUpdate, "price", idNum)

        named = _as_side(side)
        if named is not order.side:
            raise InvalidOrder(
                "order %r is a %s, not a %s" % (idNum, order.side, named)
            )
        if order.cancelled:
            raise InvalidOrder("order %r is cancelled" % (idNum,))
        if order.order_type is not OrderType.LIMIT:
            raise InvalidOrder("order %r is a market order and never rested" % (idNum,))
        if order.filled:
            raise InvalidOrder(
                "order %r is fully filled, nothing left to modify: raising its "
                "quantity would return a finished order to the book with a fresh "
                "priority stamp, carrying the fills and the timestamp of the "
                "trade that finished it. Submit a new order for the quantity you "
                "want" % (idNum,)
            )
        if qty is not None:
            _check_qty(qty)

        prev_price, prev_qty = order.price, order.qty
        # A modification is a submission of the same order at new terms, so
        # it goes through the same gate: a NaN here would corrupt the book
        # exactly as one on the submission path would (lob-d6i).
        new_price = prev_price if price is None else self.quantize(_check_price(price))
        new_qty = max(prev_qty if qty is None else qty, order.fulfilled)
        if new_price is not None:
            _check_working_price(price, new_price, new_qty, self.tick_size)
        reprioritized = new_price != prev_price or new_qty > prev_qty

        self._advance(time)
        side_book = self.book(order.instrument).side(order.side)
        if reprioritized:
            if order.resting:
                side_book.remove(order)
            order.price = new_price
            order.qty = new_qty
            order.priority = self.next_priority()
        else:
            # The passive half: `resize` is the one path that changes a
            # resting order's quantity without moving it in the queue.
            side_book.resize(order, new_qty)

        if self.recording:
            self.emit(
                Modified(
                    seq=self.next_seq(),
                    timestamp=self.time,
                    idNum=order.idNum,
                    tid=order.tid,
                    instrument=order.instrument,
                    side=order.side,
                    price=order.price,
                    qty=order.qty,
                    fulfilled=order.fulfilled,
                    prev_price=prev_price,
                    prev_qty=prev_qty,
                    priority=order.priority,
                    reprioritized=reprioritized,
                )
            )

        trades: list[Trade] = []
        if reprioritized:
            # The order is out of the book, so it crosses as a taker like any
            # arriving order -- and, like one, only what survives goes back in.
            trades = self.match(order)
            if order.remaining > 0:
                self.rest(order)
        if verbose:
            _report(trades, order.tid)
        return trades, orderUpdate

    def cancel(
        self,
        idNum: int,
        *,
        side: Side | str | None = None,
        timestamp: float | None = None,
    ) -> Order:
        """`cancelOrder` addressed by identifier and keyword. Returns the order.

        The identifier comes first and everything else is keyword-only, so the
        misbinding this spelling exists to remove -- an identifier bound to the
        `side` that `cancelOrder(side, idNum)` takes first -- is not
        expressible. The clock is `timestamp=`, spelled as `submit` spells it.

        `side` is optional and omitting it addresses the order by identifier
        alone; naming one the order does not have raises, as it does through
        the other spelling.

        It delegates and does nothing else. Every refusal, the state change and
        the `Cancelled` all stay in `cancelOrder`, so `order-lifecycle` has one
        behaviour under two names and a stream cannot say which was called.
        """
        return self.cancelOrder(side, idNum, timestamp)

    def modify(
        self,
        idNum: int,
        *,
        qty: int | None = None,
        price: float | None = None,
        side: Side | str | None = None,
        timestamp: float | None = None,
    ) -> tuple[Order, list[Trade]]:
        """`modifyOrder` addressed by identifier and keyword. Returns (order, trades).

        The return shape is `submit`'s rather than `modifyOrder`'s `(trades,
        orderUpdate)`: the two things a caller wants after changing an order --
        the order, and what the change traded -- come back in the shape and the
        order a submission already hands them back. The clock is `timestamp=`,
        spelled as `submit` spells it. `side` is optional and omitting it
        addresses the order by identifier alone.

        An omitted `qty` or `price` leaves that one alone, which is what
        `modifyOrder` reads a `None` as. **Naming neither raises.** This
        spelling cannot tell an omitted argument from one passed as `None`, so
        the alternative is a `modify(idNum)` that changes nothing, advances the
        clock and writes a `Modified` into every recorded log -- and refusing is
        the same call the dict form makes when an update is malformed, in the
        stricter direction. `modifyOrder({side, qty: None, price: None})` stays
        legal and stays a no-op modification: this is a rule of the new
        spelling, not a change to the old one.

        Beyond that refusal it delegates and does nothing else. The clamp rule,
        the reprioritization rule and the order the validations run in stay in
        `modifyOrder`, so a session driven through either spelling records the
        same stream.
        """
        if qty is None and price is None:
            raise InvalidOrder(
                "modifying order %r needs a qty or a price: naming neither "
                "would record a modification that changes nothing" % (idNum,)
            )
        order = self.require_order(idNum)
        trades, _ = self.modifyOrder(
            idNum,
            {
                "side": order.side if side is None else side,
                "qty": qty,
                "price": price,
            },
            timestamp,
        )
        return order, trades

    # -- matching ----------------------------------------------------------

    def match(self, taker: Order) -> list[Trade]:
        """Trade `taker` against the opposite side as far as it is entitled to.

        Levels best-price-first, orders within a level front-to-back: (price,
        priority) and nothing else. Each execution prices at the maker's
        limit, and `taker.remaining` bounds the whole walk, so an order with
        fills already against it trades only what it has left
        (`order-matching`).

        Resting orders of the taker's own trader are stepped over unless that
        trader allows self-matching -- untouched, still in the book, still at
        the front of their level.

        `taker` may itself be resting: that is a repriced modify, and it is
        why the taker's own side is filled through `BookSide.fill` rather than
        by touching `fulfilled` directly.

        Not replay-coherent on its own: this is half of `submit`, and what it
        leaves unmatched neither rests nor is cancelled until a caller says
        so. The trades it does execute are emitted as they happen, so it
        cannot leave a fill unrecorded -- but only if the taker is an order
        the engine accepted, which is why the first thing it does is check.
        A hand-built `Order` used to trade here against real resting
        liquidity, moving four balances and draining the book for an order
        `order()` returned `None` for and no `Accepted` ever described
        (lob-9fu).
        """
        if self._orders.get(taker.idNum) is not taker:
            raise UnknownOrder(
                "order %r is not this engine's: matching an order it never "
                "accepted trades real liquidity for an order no event "
                "describes" % (taker.idNum,)
            )
        trades: list[Trade] = []
        book = self.book(taker.instrument)
        # Both are whole `BookSide`s, not collections of counterparties: the
        # maker book is the side being walked, the taker book the side the
        # taker would rest on -- and does rest on already when this is a
        # repriced modify, which is why its fills go through it.
        maker_book = book.opposite(taker.side)
        if taker.cancelled or taker.remaining <= 0 or not maker_book:
            return trades
        taker_book = book.side(taker.side)
        gated = not self.trader(taker.tid).allow_self_matching
        # `try`/`finally` rather than `contextlib.closing`: the walk has to be
        # closed on every exit (that is what puts the popped prices back), and
        # the context manager charges an object, an `__enter__` and an
        # `__exit__` per submission for the same guarantee. Nothing may come
        # between the two lines below: an exception there would leave the walk
        # holding prices it popped off the heap.
        levels = maker_book.match_levels(taker.price)
        try:
            for level in levels:
                if gated and level.sole_tid == taker.tid:
                    # Every order here is the taker's own, so the gate would
                    # step over all of them and trade nothing. One comparison
                    # instead of one per order -- the single-agent case.
                    continue
                # Orders skipped by the gate stay at the front of the level
                # and stay put, so `skipped` is the length of a prefix that
                # cannot change under us: it is what a replacement cursor has
                # to be walked back over, and only a fill needs one.
                skipped = 0
                cursor: Iterator[Order] | None = None
                while taker.remaining > 0:
                    if cursor is None:
                        cursor = iter(level)
                        for _ in range(skipped):
                            next(cursor, None)
                    maker = next(cursor, None)
                    if maker is None:
                        break
                    if gated and maker.tid == taker.tid:
                        skipped += 1
                        continue
                    trades.append(
                        self._execute(
                            book, taker, taker_book, maker, maker_book, level.price
                        )
                    )
                    # A fill that did not exhaust the taker exhausted its
                    # maker, which leaves the level's dict a member shorter
                    # and the cursor over it unusable.
                    cursor = None
                if taker.remaining <= 0:
                    break
        finally:
            levels.close()
        return trades

    def _execute(
        self,
        book: InstrumentBook,
        taker: Order,
        taker_book: BookSide,
        maker: Order,
        maker_book: BookSide,
        price: float,
    ) -> Trade:
        """One execution: fill both orders, charge both, move four balances.

        The arithmetic comes first and the fills second, so that an execution
        is all-or-nothing with respect to its own numbers: computed between
        the fills and the settlement, an `OverflowError` out of `qty * price`
        left both orders recording a fill that no balance had moved for and no
        `Filled` described. The input gate makes that overflow unreachable;
        the ordering makes it harmless.
        """
        qty = min(taker.remaining, maker.remaining)
        value = qty * price

        maker_book.fill(maker, qty)
        taker_book.fill(taker, qty)

        taker_delta = self._charge(taker, value)
        maker_delta = self._charge(maker, value)
        book.last_price = price

        if taker.side is Side.BID:
            bid, ask = taker, maker
            bid_delta, ask_delta = taker_delta, maker_delta
        else:
            bid, ask = maker, taker
            bid_delta, ask_delta = maker_delta, taker_delta
        self._settle(book, bid, ask, qty, value, bid_delta, ask_delta)

        trade_id = self.next_trade_id()
        if self.recording:
            self.emit(
                Filled(
                    seq=self.next_seq(),
                    timestamp=self.time,
                    instrument=book.symbol,
                    trade_id=trade_id,
                    price=price,
                    qty=qty,
                    taker_side=taker.side,
                    bid_idNum=bid.idNum,
                    bid_tid=bid.tid,
                    bid_fulfilled=bid.fulfilled,
                    bid_value=bid.value,
                    bid_commission=bid.commission,
                    bid_commission_delta=bid_delta,
                    ask_idNum=ask.idNum,
                    ask_tid=ask.tid,
                    ask_fulfilled=ask.fulfilled,
                    ask_value=ask.value,
                    ask_commission=ask.commission,
                    ask_commission_delta=ask_delta,
                )
            )
        return Trade(
            trade_id=trade_id,
            timestamp=self.time,
            instrument=book.symbol,
            price=price,
            qty=qty,
            taker_side=taker.side,
            bid_idNum=bid.idNum,
            bid_tid=bid.tid,
            ask_idNum=ask.idNum,
            ask_tid=ask.tid,
        )

    def _cancel(self, order: Order, reason: CancelReason) -> None:
        """Take `order` out of the book and record why it left.

        Shared by the caller's cancel and by the engine's own cancellation of
        an IOC remainder; `reason` is what tells a replayer which of the two
        it is re-issuing and which it re-derives.
        """
        if order.resting:
            self.book(order.instrument).side(order.side).remove(order)
        remaining = order.remaining
        order.cancelled = True
        order.cancel_reason = reason
        if self.recording:
            self.emit(
                Cancelled(
                    seq=self.next_seq(),
                    timestamp=self.time,
                    idNum=order.idNum,
                    tid=order.tid,
                    instrument=order.instrument,
                    side=order.side,
                    price=order.price,
                    fulfilled=order.fulfilled,
                    remaining=remaining,
                    reason=reason,
                )
            )

    # -- the ledgers -------------------------------------------------------

    def balance(self, tid: int, symbol: str) -> float:
        """`tid`'s holding of `symbol` -- an instrument or a currency.

        Zero before any movement, and freely negative afterwards: balances
        record what happened and gate nothing (`trader-balances`).

        One namespace covers both kinds, so a symbol that is an instrument
        *and* somebody's settlement currency answers for the two of them
        added together, with no way back to either. `configure_instrument`
        refuses the case it can see (an instrument settling in itself) and
        names the case it cannot.
        """
        return self._balances.get((tid, symbol), 0.0)

    def holdings(self) -> Iterator[tuple[int, str, float]]:
        """Every `(tid, symbol, amount)` the ledger has moved."""
        for (tid, symbol), amount in self._balances.items():
            yield tid, symbol, amount

    def _charge(self, order: Order, value: float) -> float:
        """Add one fill to `order`'s totals; return the commission increment.

        Cumulative recompute, not per-fill accrual: the formula is applied to
        the order's whole (Q, V) and only the difference is charged. Two fills
        of 3 then 2 @ 100 under min=2.5 charge 2.5 in total, not 5.0 -- the
        second fill's increment is zero.
        """
        order.value += value
        charged = commission_for(self.trader(order.tid), order.fulfilled, order.value)
        delta = charged - order.commission
        order.commission = charged
        return delta

    def _settle(
        self,
        book: InstrumentBook,
        bid: Order,
        ask: Order,
        qty: int,
        value: float,
        bid_delta: float,
        ask_delta: float,
    ) -> None:
        """The four movements of one trade, and the two commission debits.

        The rule stated once in `PyLOB.events` and applied here and in every
        sink. Commission settles in the instrument's currency, so an
        instrument with no declared currency moves its own leg and nothing
        else -- the fill is still recorded and the currency leg is recoverable
        from it.
        """
        self._credit(bid.tid, book.symbol, float(qty))
        self._credit(ask.tid, book.symbol, -float(qty))
        if book.currency is None:
            return
        self._credit(bid.tid, book.currency, -(value + bid_delta))
        self._credit(ask.tid, book.currency, value - ask_delta)

    def _credit(self, tid: int, symbol: str, amount: float) -> None:
        """Move one balance. No check of any kind: this records, it does not vet."""
        key = (tid, symbol)
        self._balances[key] = self._balances.get(key, 0.0) + amount

    # -- the book ----------------------------------------------------------

    def book(self, instrument: str) -> InstrumentBook:
        """The book for `instrument`, created empty on first mention.

        **A read creates too, and there is no undeclared symbol.** Every query
        that names an instrument -- `getBestBid`, `getWorstAsk`,
        `getVolumeAtPrice`, `getLastPrice`, `snapshot`, `print` -- reaches the
        store through here, so `getBestBid("TYPO")` answers `None` *and* leaves
        `TYPO` in `instruments()` for the rest of the session. Nothing warns,
        because nothing here can tell a typo from an instrument about to be
        traded: an empty book is what an instrument's first mention looks like
        either way.

        Two consequences worth knowing before they surprise you. A survey that
        walks `instruments()` after polling a list of candidate symbols
        surveys the candidates, not the traded set -- take the symbols from
        your own configuration instead. And a process that queries an
        unbounded stream of distinct symbols grows one `InstrumentBook` per
        symbol it has ever seen, since nothing prunes them; a fixed symbol set,
        which is every simulation this library was written for, costs one
        empty book per typo and nothing more.

        Creation on first mention is the standing scope constraint
        (`openspec/config.yaml`: "a book springs into being on first mention"),
        so making the read queries non-creating is a contract change and an
        OpenSpec proposal, not a tidy-up. It is recorded here rather than done.
        """
        book = self._books.get(instrument)
        if book is None:
            book = self._books[instrument] = InstrumentBook(symbol=instrument)
        return book

    def instruments(self) -> Iterator[str]:
        """Every instrument this engine has a book for.

        Every symbol *mentioned*, that is, which is not the same as every
        symbol traded or configured: a book springs into being on first
        mention and a read query mentions one (`book`). An empty entry here is
        as likely to be a mistyped query as an instrument awaiting its first
        order.
        """
        return iter(self._books)

    def rest(self, order: Order) -> None:
        """Put `order` into the book at the back of its price level.

        Called with whatever a submission or a modification has left over. A
        market order never gets here (`order-lifecycle`: it is
        immediate-or-cancel), and neither does a fully filled one.

        Not replay-coherent on its own, and it emits nothing: resting is the
        *absence* of a transition, and what records it is the `Accepted` (or
        `Modified`) of the order that got here, which a replayer re-issues as
        a whole `submit`. Calling it on an order the engine did not accept is
        refused for the reason `match` is: an order in the book that no event
        describes is liquidity a replay cannot rebuild.
        """
        if self._orders.get(order.idNum) is not order:
            raise UnknownOrder(
                "order %r is not this engine's: resting an order it never "
                "accepted puts liquidity in the book that no event describes"
                % (order.idNum,)
            )
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
    #
    # Every one of these names an instrument, and naming one creates its book
    # if it does not have one: an unknown symbol reads as an empty book and
    # stays in `instruments()` afterwards. See `book`, which says what that
    # costs and why it is not fixed here.

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
        return self.book(instrument).side(side).volume_at(self.quantize(price))

    def depth(
        self, instrument: str, side: Side | str, levels: int | None = None
    ) -> tuple[tuple[float, int], ...]:
        """The aggregated price ladder for one side: (price, volume), best first.

        One pair per distinct price at which orders rest -- the price, and the
        total unfulfilled quantity resting at exactly that price. The order is
        `snapshot`'s order and matching's order, best price first, because all
        three read the same levels: one rule, three queries.

        `levels` bounds the answer to the best N; `None` is the whole ladder. A
        bound that is not a positive whole number raises rather than answering,
        because "the best zero levels" and "the whole ladder" are the two things
        `0` could have meant and neither is worth guessing at -- ask with `None`
        for the second.

        A side with nothing resting answers an empty ladder and does not raise:
        an empty book is a book, and every other read-side query says so too.

        The volume is the level's own running total (`PriceLevel`), so a ladder
        costs the sort over prices and nothing per order. Levels aggregate by
        exact price and every resting price is already on the tick grid, so no
        level can be split in two by a rounding difference.

        `book-queries` requires this to agree with the queries taken beside it,
        and it agrees by construction rather than by arrangement: the ends of
        the ladder are `getBest*` and `getWorst*`, its volumes accumulated from
        the best level down are `getVolumeAtPrice` at each of those prices, and
        its pairs are `snapshot`'s remainders aggregated by price.
        """
        if levels is not None:
            if not isinstance(levels, int) or isinstance(levels, bool):
                raise InvalidOrder(
                    "levels must be an integer, or None for the whole ladder, "
                    "got %r" % (levels,)
                )
            if levels < 1:
                raise InvalidOrder(
                    "levels must be at least 1, got %r: pass None for the whole "
                    "ladder" % (levels,)
                )
        ladder = self.book(instrument).side(side).levels()
        if levels is not None:
            ladder = ladder[:levels]
        return tuple((level.price, level.volume) for level in ladder)

    def getLastPrice(self, instrument: str) -> float | None:
        """The most recent trade price, or None before the first trade."""
        return self.book(instrument).last_price

    def setLastPrice(self, instrument: str, price: float) -> None:
        """Refused: the last-trade price is output, and cannot be dictated.

        `book-queries` defines the value this would overwrite as "the price of
        the most recent trade" and requires that after a reload it equal the
        last trade in the persisted record. An assignment satisfies neither
        clause: it names a price no trade made and emits nothing, so the engine
        and its own log report different last prices and a replay lands on the
        log's (module docstring, "What the stream records"). `_execute` sets
        the value where it is defined, on every execution.

        Seeding an opening price before the first trade is the use this
        refuses; it would need an event of its own, which is a stream-format
        change and a maintainer's call. Raises `InvalidOrder` always, and is
        kept as a name that says why because removing a public name needs an
        ADR (`config.yaml`).
        """
        raise InvalidOrder(
            "the last-trade price of %r is engine output, set by executions "
            "(%r): assigning it reports a price no trade made and no event "
            "records" % (instrument, price)
        )

    def print(self, instrument: str) -> str:
        """The book as text, in the legacy engine's shape. Returns what it prints.

        Eyeball output and nothing else: nothing parses this and no test
        asserts on it, so the shape is kept only to spare `example.py`'s output
        a gratuitous diff (design.md, risks).

        Both sides list best price first and FIFO within a price -- the same
        matching-priority order `snapshot` gives -- as `id)qty-fulfilled @
        price t=timestamp`. The two `volume ...` probes are legacy's, fixed
        prices and all: they mean something for the example's book and very
        little for anyone else's, and `getVolumeAtPrice` is the query to ask
        directly.

        The trades section differs, and not by omission: this engine keeps no
        trade log. Retention is not the objection -- `_orders` keeps every
        order the book has ever seen, because that store is the
        identifier-uniqueness test `order-lifecycle` requires. A trade log is
        required by nothing, so it would be a second unbounded structure,
        grown on every run to serve a debug print, and a sink already records
        those executions for the runs that want them. `last_price` is what
        the engine does retain, so that is what the section shows; a full
        history is `select * from trade` against a `SQLiteSink` database.
        """
        book = self.book(instrument)
        lines = ["------ Bids -------"]
        lines += [_book_line(order) for order in book.bids]
        lines += ["", "------ Asks -------"]
        lines += [_book_line(order) for order in book.asks]
        lines += ["", "------ Trades ------", "last price: %s" % (book.last_price,), ""]
        lines += [
            "volume bid if i ask 98: %d"
            % (self.getVolumeAtPrice(instrument, "bid", 98),),
            "volume ask if i bid 101: %d"
            % (self.getVolumeAtPrice(instrument, "ask", 101),),
            "best bid: %s" % (self.getBestBid(instrument),),
            "worst bid: %s" % (self.getWorstBid(instrument),),
            "best ask: %s" % (self.getBestAsk(instrument),),
            "worst ask: %s" % (self.getWorstAsk(instrument),),
        ]
        value = "\n".join(lines) + "\n"
        # The builtin: a method named `print` shadows it in the class body,
        # never inside a method body.
        print(value)
        return value

    # -- counters ----------------------------------------------------------

    # Each of the three hands out a number and moves on; nothing re-issues a
    # number, and no event says one was taken. Called from outside the
    # operation that needs it, they are silent state changes -- which is what
    # the list in `tests/test_emission_coverage.py` records them as.

    def next_priority(self) -> int:
        """Allocate the next arrival stamp (consumes it).

        Advances whether or not a sink is attached.

        Not replay-coherent: a stamp taken outside `create_order` or a
        repricing `modifyOrder` belongs to no order and is recorded nowhere,
        so every later order's `priority` is one higher here than in the
        replay of this session. Priority is the matching tie-break, so that is
        a divergence in matching order, not merely in a reported number.
        """
        priority = self._next_priority
        self._next_priority += 1
        return priority

    def next_trade_id(self) -> int:
        """Allocate the next trade identifier (consumes it).

        Not replay-coherent: as with `next_priority`, an identifier taken
        outside `_execute` names no trade, and every later trade is numbered
        one higher than the replay numbers it.
        """
        trade_id = self._next_trade_id
        self._next_trade_id += 1
        return trade_id

    def next_seq(self) -> int:
        """Allocate the next position in the event stream (consumes it).

        Matching must never read it.

        Not replay-coherent: a `seq` taken without an event to carry it
        leaves a hole in the stream, and a sink reading the log is entitled to
        treat a non-contiguous `seq` range as a lost event (`sinks.sqlite`,
        `check_log`) -- a session that called this would be read as a damaged
        one.
        """
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
        """Hand one event to the sink, or drop it when there is none.

        The call is synchronous and unguarded: `consume` runs on this thread,
        inside the operation that built the event, and returns before the
        operation does. What a sink may do from that position is
        `events.EventSink`'s contract, and nothing here checks it -- a sink
        that reads the engine is answered, one that calls back into it is
        obeyed, and one that raises takes the operation down with it.

        Not replay-coherent: it records, it does not act. An event pushed in
        from outside describes a transition the engine did not make, and the
        replay of that stream makes it -- the one direction of divergence the
        others do not have.
        """
        if self._sink is not None:
            self._sink.consume(event)

    def close(self) -> None:
        """End the session, flushing the sink if it has anything buffered."""
        if self._sink is not None:
            close_sink(self._sink)

    # -- internals ---------------------------------------------------------

    def _advance(self, timestamp: float | None) -> float:
        """Move the clock on by one, or to the caller's value on the replay path.

        Every operation stamps its events with `self.time`, so this is what
        makes the fills and the IOC cancellation of a submission carry that
        submission's timestamp. Recorded data, never a sort key.
        """
        if timestamp is None:
            self.time += 1
        else:
            self.time = timestamp
        return self.time

    def _assign_idNum(self, idNum: int | None) -> int:
        """Allocate or accept an identifier, keeping it unique for all time.

        The store is never pruned, so `in self._orders` is a lifetime test and
        not merely a test of what is resting. The high-water mark is what
        survives a reload: replay supplies every identifier it saw, each one
        pushes the counter past itself, and the first order submitted
        afterwards lands above all of them.
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


def _check_qty(qty: int) -> None:
    """A quantity the engine will work with, or an `InvalidOrder` saying why not.

    `order-lifecycle`: an invalid submission raises a library exception and
    the API never terminates the host process -- the legacy engine called
    `sys.exit` here. `bool` is excluded because `True` is an `int` and an
    order for `True` units is a mistake, not a quantity.

    Python integers have no ceiling and floats do, so the range is bounded at
    the top as well: `MAX_QTY` is where `qty * price` stops being arithmetic
    and starts being an `OverflowError` raised from the middle of an
    execution.
    """
    if not isinstance(qty, int) or isinstance(qty, bool):
        raise InvalidOrder("quantity must be an integer, got %r" % (qty,))
    if qty <= 0:
        raise InvalidOrder("quantity must be positive, got %r" % (qty,))
    if qty > MAX_QTY:
        raise InvalidOrder(
            "quantity must be at most %d (2**53), got %r" % (MAX_QTY, qty)
        )


def _check_price(price: Any) -> float:
    """A price the engine will hold, or an `InvalidOrder` saying why not.

    An ordinary float is answered in two comparisons and everything else is
    handed to `_positive_real`, because this runs on every submission and the
    accept path is the one ADR-0002 measures.

    Finite, real, and strictly positive. Positivity is a decision rather than
    a reading of the specs (module docstring, "What a submission has to be"):
    a negative price makes `commission_for` return a negative charge and
    `_settle` credits it, so the ledger would pay a trader to trade. Zero goes
    with it, as the price at which every fill is worth nothing.

    Non-finite is the one that cost a book: `float("nan")` compares false
    against everything, so a NaN price that rests is never the minimum of its
    heap, never evicted, and buries every better price underneath it -- a book
    left crossed between two traders, permanently and silently, and faithfully
    reproduced by replay. That same property is what the fast path leans on:
    `0.0 < price` is already false for a NaN and for `-inf`, so the bounded
    comparison below is the whole finiteness test.
    """
    if type(price) is float and 0.0 < price < _INF:
        return price
    return _positive_real(price, "price")


def _check_notional(qty: int, price: float) -> None:
    """`qty * price` has to be a number, not an infinity.

    The product is what `_execute` charges commission on and settles in cash.
    Two individually legal operands can have an illegal product, and floats
    overflow quietly: 10**300 units at 1e30 came to `value = inf`, took the
    ledger to +/-inf, and made the percentage commission cap stop capping,
    since `min(inf, x)` is `x` -- with nothing raised anywhere
    (`docs/engine-review-2026-08.md`, lob-d6i). `MAX_QTY` refuses that
    quantity now, leaving only a quantity at the ceiling against a price above
    `_NOTIONAL_GUARD` for the caller to ask about.
    """
    if not isfinite(qty * price):
        raise InvalidOrder(
            "quantity %r at price %r overflows: qty * price must be finite"
            % (qty, price)
        )


def _check_working_price(
    submitted: Any, working: float, qty: int, tick_size: float
) -> None:
    """What a price still has to survive *after* the grid has been applied.

    Both places a price enters the book run this: `create_order` on a
    submission and `modifyOrder` on a reprice, which is the same gate because a
    modification is a submission of the same order at new terms. It was written
    out twice and the copies were verbatim, so the only thing keeping them
    equal was that nobody had edited one.

    `submitted` is the price as the caller wrote it, quoted back at them
    because `working` is a number they never typed.
    """
    if working <= 0:
        raise InvalidOrder(
            "price %r quantizes to %r on a tick of %r: under half a tick is "
            "not a price this book can hold" % (submitted, working, tick_size)
        )
    if working > _NOTIONAL_GUARD:
        _check_notional(qty, working)


def _required(update: dict[str, Any], field_name: str, idNum: int) -> Any:
    """One field of a modification, or an `InvalidOrder` naming what is missing.

    A modification states what the order should now be, so the fields it can
    change are named even when they do not change -- `None` says "leave this
    one alone". Absence is a malformed update and is refused as one, rather
    than defaulting to something the caller did not ask for (lob-crf).
    """
    try:
        return update[field_name]
    except KeyError:
        raise InvalidOrder(
            "modifying order %r needs a %r (None leaves it unchanged)"
            % (idNum, field_name)
        ) from None


def _quote_field(quote: dict[str, Any], field_name: str) -> Any:
    """One field of a dict quote, or an `InvalidOrder` naming what is missing.

    `_required`'s counterpart on the submission side. A quote states an order
    the caller wants; a quote that omits part of it is malformed input, and
    `order-lifecycle` has malformed input raise a library exception rather
    than something from inside the engine. Indexing the dict directly raised
    `KeyError`, which is neither a `PyLOBError` nor a `ValueError`, so a
    caller who had wrapped the call in the two exceptions this library
    documents saw it go past them (lob-49r).

    `price` is not read through this: it is absent for every market order, and
    a limit order without one is refused by `create_order` where the rule that
    requires it lives.
    """
    try:
        return quote[field_name]
    except KeyError:
        raise InvalidOrder(
            "a quote needs a %r: processOrder reads tid, instrument, side, "
            "type and qty off it, and price for a limit order" % (field_name,)
        ) from None


def _replay_field(quote: dict[str, Any], field_name: str) -> Any:
    """One field `fromData` promised to replay, or an `InvalidOrder` naming it.

    `_quote_field`'s rule applied to the two fields the flag itself makes
    required. `processOrder(quote, fromData=True)` says the quote's identity
    comes from the data; a quote that does not carry it has not got one, and
    assigning it here would hand back a run whose orders are numbered and
    stamped by the engine rather than by the source it claims to replay -- the
    failure that shows up as a session nobody can reproduce rather than as an
    error (lob-crf, lob-49r, lob-0mv). The legacy engine required both.

    `None` is refused alongside absence, and not read as "leave it to the
    engine" the way `_required` reads it as "leave it alone": `None` is exactly
    what `submit` takes as "assign one", so admitting it here would be the
    silent assignment under another spelling.
    """
    value = quote.get(field_name)
    if value is None:
        raise InvalidOrder(
            "a fromData quote needs its own %r: fromData replays the identity "
            "the data carries, and assigning one here records a session that "
            "cannot be traced back to it. Pass fromData=False for a quote the "
            "engine should stamp" % (field_name,)
        )
    return value


def _book_line(order: Order) -> str:
    """One resting order as `OrderBook.print` writes it (legacy's format)."""
    return "%s)%s-%s @ %s t=%s" % (
        order.idNum,
        order.qty,
        order.fulfilled,
        order.price,
        order.timestamp,
    )


def _report(trades: list[Trade], tid: int) -> None:
    """Print one line per trade, in the legacy engine's format.

    `verbose` output is read by eyeballs and by nothing else; keeping the
    shape keeps example output free of gratuitous diff noise.
    """
    for trade in trades:
        counterparty = trade.ask_tid if trade.taker_side is Side.BID else trade.bid_tid
        print(
            ">>> TRADE \nt=%s $%f n=%d p1=%d p2=%d"
            % (trade.timestamp, trade.price, trade.qty, counterparty, tid)
        )


def _as_side(side: Side | str) -> Side:
    """`side` as a `Side`, or an `InvalidOrder` naming what was allowed.

    The identity test first because the engine calls this on itself more often
    than on a caller's string -- every `book.side(order.side)` and
    `book.opposite(order.side)` passes a `Side` that is already one, and
    `Side(member)` is a full enum lookup to hand back what it was given. An
    enum with members cannot be subclassed, so `type(side) is Side` is the
    whole membership test and not merely a common case of it.
    """
    if type(side) is Side:
        return side
    try:
        return Side(side)
    except ValueError:
        raise InvalidOrder(
            "side must be one of %r, got %r" % (OrderBook.valid_sides, side)
        ) from None


def _as_order_type(order_type: OrderType | str) -> OrderType:
    """`order_type` as an `OrderType`, or an `InvalidOrder`."""
    if type(order_type) is OrderType:
        return order_type
    try:
        return OrderType(order_type)
    except ValueError:
        raise InvalidOrder(
            "order type must be one of %r, got %r" % (OrderBook.valid_types, order_type)
        ) from None
