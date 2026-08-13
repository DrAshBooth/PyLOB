"""Fixtures shared by the acceptance suites.

The acceptance suites encode the *frozen contracts* under
`openspec/specs/` -- target behaviour, not what the engine happens to do
today. What is engine-specific lives here rather than in the suites.

The seam is **the adapter**: one engine-neutral vocabulary for submitting
orders and observing the result.

operations
    `limit`, `market`, `cancel`, `modify`, `reopen`, `close`
observation
    `order_state`, `snapshot`, `trades`, `best`, `worst`, `volume_at`,
    `last_price`, `balance`

The suites never touch `processOrder`'s dict quotes or the engine's own
objects; they call `engine.limit(...)`, read `order.fulfilled`, ask for
`engine.snapshot("bid")`. That is what lets one test body describe a
*contract* rather than an implementation, and it is not hypothetical: the
spec-derived reference matcher in `tests/reference` speaks exactly this
surface, which is what makes the differential harness expressible.

ADR-0003 retired the second engine this file used to carry (the 2013 SQL
`LegacyOrderBook`), and with it the registry that parameterized every suite
over both and the `engine_xfail` marker that recorded where the two diverged.
`build_inmemory` is the one builder left; a future second engine wants its own
builder against this surface, and a params list back over the two.

Each book records to its own sqlite *file* under `tmp_path`, because
`reopen()` needs somewhere to reload from -- see `InMemoryAdapter`.
"""

from itertools import count
from typing import NamedTuple, Optional

import pytest
from PyLOB.engine import OrderBook
from PyLOB.sinks.sqlite import SQLiteSink, read_events

INSTRUMENT = "FAKE"
CURRENCY = "USD"
DEFAULT_TICK = 0.0001

# Low ids so a suite can use the trader numbers its scenario text uses
# ("trader 2 buys 5 @ 101 from trader 1"), plus example.py's block.
TRADERS = (1, 2, 3, 4, 5) + tuple(range(100, 112))

# Commission and balance are floating-point sums with no quantization step, so
# the commissions contract requires comparison within a tolerance rather than
# for bit equality. `approx_money` is the only sanctioned comparison.
MONEY_REL = 1e-9
MONEY_ABS = 1e-9


class Commissions(NamedTuple):
    """A trader's commission schedule: `min(max_pct * V / 100, max(min, per_unit * Q))`."""

    min: float = 0.0
    max_pct: float = 0.0
    per_unit: float = 0.0


NO_COMMISSION = Commissions()


class _Unset:
    def __repr__(self):
        return "<unset>"


#: "argument not supplied", distinct from an explicitly supplied ``None``
#: (`modify(order, price=None)` is a scenario in its own right).
UNSET = _Unset()


# --------------------------------------------------------------------------
# engine-neutral values
# --------------------------------------------------------------------------


class OrderState(NamedTuple):
    """An order as the engine currently holds it."""

    idNum: int
    side: str
    order_type: str
    price: Optional[float]
    qty: int
    fulfilled: int
    cancelled: bool
    resting: bool
    commission: float


class BookEntry(NamedTuple):
    """One resting order in a book snapshot; list position is priority position."""

    idNum: int
    price: Optional[float]
    available: int
    qty: int
    fulfilled: int


class Trade(NamedTuple):
    """One execution. `bid` and `ask` are the two orders' external identifiers."""

    bid: int
    ask: int
    price: float
    qty: int


class OrderRef:
    """Handle to a submitted order.

    `trades` are the executions that submission itself produced; every other
    attribute is read from the engine at the moment it is asked for, so a
    reference taken early still reports current state.
    """

    def __init__(self, engine, idNum, side, trades=()):
        self.engine = engine
        self.idNum = idNum
        self.side = side
        self.trades = tuple(trades)

    def __repr__(self):
        return "OrderRef(idNum=%r, side=%r)" % (self.idNum, self.side)

    @property
    def state(self):
        state = self.engine.order_state(self.idNum)
        assert state is not None, "no order with idNum=%r" % (self.idNum,)
        return state

    @property
    def qty(self):
        return self.state.qty

    @property
    def price(self):
        return self.state.price

    @property
    def fulfilled(self):
        return self.state.fulfilled

    @property
    def cancelled(self):
        return self.state.cancelled

    @property
    def resting(self):
        return self.state.resting

    @property
    def commission(self):
        return self.state.commission


# --------------------------------------------------------------------------
# the engine, behind the adapter surface
# --------------------------------------------------------------------------


class InMemoryAdapter:
    """`PyLOB.engine.OrderBook` behind the engine-neutral acceptance surface.

    Observation reads the engine's own objects -- an `Order` for order state,
    a book snapshot for the queue, the in-core ledger for a balance -- and the
    trade log is the executions the engine returned from each operation.

    A `SQLiteSink` records the session to `db_path`, because `reopen()` needs
    somewhere to reload *from*: this engine's persisted state is its event
    stream, not a book on disk.
    """

    def __init__(self, book, db_path, instrument, currency, tick_size):
        self.book = book
        self.db_path = db_path
        self.instrument = instrument
        self.currency = currency
        self.tick_size = tick_size
        self._trades = []
        self._reloads = 0

    # -- operations --------------------------------------------------------

    def limit(self, side, qty, price, tid, **kwargs):
        """Submit a limit order; return an `OrderRef`."""
        return self._submit("limit", side, qty, tid, price=price, **kwargs)

    def market(self, side, qty, tid, **kwargs):
        """Submit a market order; return an `OrderRef`."""
        return self._submit("market", side, qty, tid, price=None, **kwargs)

    def cancel(self, order, side=UNSET):
        """Cancel an order, addressed by `OrderRef` or by raw identifier."""
        idNum, side = self._target(order, side)
        self.book.cancelOrder(side, idNum)

    def modify(self, order, qty=UNSET, price=UNSET, side=UNSET, tid=UNSET):
        """Modify an order; return the executions the modification produced."""
        idNum, side = self._target(order, side)
        state = self.order_state(idNum)
        if qty is UNSET:
            qty = state.qty if state else None
        if price is UNSET:
            price = state.price if state else None
        if tid is UNSET:
            tid = self._owner(idNum)

        trades, _ = self.book.modifyOrder(
            idNum, dict(side=side, qty=qty, price=price, tid=tid)
        )
        return self._record(trades)

    def reopen(self):
        """Rebuild the engine from persisted state; return self.

        For this engine the persisted state is the event stream. The sink is
        flushed and closed, the *replayable* events are read back -- the
        configuration and the commands a caller made, never the fills, which
        would double-book -- and re-issued into a fresh book that derives
        every trade for itself. Nothing carries over in memory: the trade log
        here is cleared and refilled by the replay, so what it holds
        afterwards is what the new engine produced, not what the old one did.

        The reloaded session records to a new file, since the log's `seq` is
        its primary key and a second session cannot append to the first.
        """
        self.book.close()

        events = list(read_events(self.db_path, replayable_only=True))
        self._reloads += 1
        self.db_path = self.db_path.with_name(
            "%s.reload%d.db" % (self.db_path.stem, self._reloads)
        )
        self._trades = []

        book = None
        for event in events:
            # `KIND` is the wire name and is stable across renames of the
            # event class, so dispatching on it needs no imports.
            if event.KIND == "session_started":
                book = OrderBook(
                    tick_size=event.tick_size, sink=SQLiteSink(self.db_path)
                )
            elif event.KIND == "instrument_configured":
                book.configure_instrument(event.symbol, event.currency)
            elif event.KIND == "trader_configured":
                book.configure_trader(
                    event.tid,
                    name=event.name,
                    allow_self_matching=event.allow_self_matching,
                    commission_min=event.commission_min,
                    commission_max_percnt=event.commission_max_percnt,
                    commission_per_unit=event.commission_per_unit,
                )
            elif event.KIND == "accepted":
                _, trades = book.submit(
                    tid=event.tid,
                    instrument=event.instrument,
                    side=event.side,
                    order_type=event.order_type,
                    qty=event.qty,
                    price=event.price,
                    idNum=event.idNum,
                    timestamp=event.timestamp,
                )
                self._trades.extend(trades)
            elif event.KIND == "modified":
                trades, _ = book.modifyOrder(
                    event.idNum,
                    dict(
                        side=event.side,
                        qty=event.qty,
                        price=event.price,
                        tid=event.tid,
                    ),
                    time=event.timestamp,
                )
                self._trades.extend(trades)
            elif event.KIND == "cancelled":
                book.cancelOrder(event.side, event.idNum, time=event.timestamp)

        self.book = book
        return self

    def close(self):
        self.book.close()

    # -- observation -------------------------------------------------------

    def order_state(self, idNum):
        """`OrderState` for `idNum`, or None if no order has that identifier."""
        order = self.book.order(idNum)
        if order is None:
            return None
        return OrderState(
            idNum=order.idNum,
            side=str(order.side),
            order_type=str(order.order_type),
            price=order.price,
            qty=order.qty,
            fulfilled=order.fulfilled,
            cancelled=order.cancelled,
            resting=order.resting,
            commission=order.commission,
        )

    def snapshot(self, side, instrument=None):
        """Resting orders on `side`, in the engine's own matching priority order."""
        return tuple(
            BookEntry(
                idNum=order.idNum,
                price=order.price,
                available=order.remaining,
                qty=order.qty,
                fulfilled=order.fulfilled,
            )
            for order in self.book.snapshot(instrument or self.instrument, side)
        )

    def trades(self, instrument=None):
        """Every execution in the instrument, oldest first."""
        instrument = instrument or self.instrument
        return tuple(
            Trade(
                bid=trade.bid_idNum,
                ask=trade.ask_idNum,
                price=trade.price,
                qty=trade.qty,
            )
            for trade in self._trades
            if trade.instrument == instrument
        )

    def best(self, side, instrument=None):
        instrument = instrument or self.instrument
        if side == "bid":
            return self.book.getBestBid(instrument)
        return self.book.getBestAsk(instrument)

    def worst(self, side, instrument=None):
        instrument = instrument or self.instrument
        if side == "bid":
            return self.book.getWorstBid(instrument)
        return self.book.getWorstAsk(instrument)

    def volume_at(self, side, price, instrument=None):
        return self.book.getVolumeAtPrice(instrument or self.instrument, side, price)

    def last_price(self, instrument=None):
        return self.book.getLastPrice(instrument or self.instrument)

    def balance(self, tid, instrument):
        """A trader's balance in an instrument or currency; 0 before any movement."""
        return self.book.balance(tid, instrument)

    # -- internals ---------------------------------------------------------

    def _submit(
        self,
        order_type,
        side,
        qty,
        tid,
        price,
        instrument=None,
        idNum=None,
        timestamp=None,
    ):
        if timestamp is not None and idNum is None:
            raise ValueError("the replay path needs an explicit idNum")
        if idNum is not None and timestamp is None:
            # What the legacy adapter does with a bare idNum, kept so the two
            # engines see the same replayed clock.
            timestamp = idNum

        order, trades = self.book.submit(
            tid=tid,
            instrument=instrument or self.instrument,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            idNum=idNum,
            timestamp=timestamp,
        )
        return OrderRef(self, order.idNum, side, self._record(trades))

    def _record(self, trades):
        """Add executions to the log; return them in the neutral vocabulary."""
        self._trades.extend(trades)
        return tuple(
            Trade(
                bid=trade.bid_idNum,
                ask=trade.ask_idNum,
                price=trade.price,
                qty=trade.qty,
            )
            for trade in trades
        )

    def _target(self, order, side):
        """Resolve (identifier, side) from an `OrderRef` or a raw identifier."""
        if isinstance(order, OrderRef):
            return order.idNum, order.side if side is UNSET else side
        if side is UNSET:
            state = self.order_state(order)
            side = state.side if state else None
        return order, side

    def _owner(self, idNum):
        order = self.book.order(idNum)
        return order.tid if order else None


def build_inmemory(
    db_path,
    traders=TRADERS,
    commissions=NO_COMMISSION,
    self_matching=(),
    instrument=INSTRUMENT,
    currency=CURRENCY,
    tick_size=DEFAULT_TICK,
):
    """Build the in-memory engine (ADR-0001, epic lob-5rt), recording to `db_path`.

    Configuration goes through the engine's own calls rather than through
    seeded rows, so the commission schedule and the self-matching flags reach
    the recorded stream as `TraderConfigured` events -- which is what makes
    `reopen()` able to rebuild a book that charges the same commissions.
    """
    schedule = _as_commissions(commissions)
    allowed = _self_matching_set(self_matching, traders)

    book = OrderBook(tick_size=tick_size, sink=SQLiteSink(db_path))
    book.configure_instrument(instrument, currency)
    for tid in traders:
        book.configure_trader(
            tid,
            name=str(tid),
            allow_self_matching=tid in allowed,
            commission_min=schedule.min,
            commission_max_percnt=schedule.max_pct,
            commission_per_unit=schedule.per_unit,
        )

    return InMemoryAdapter(
        book,
        db_path=db_path,
        instrument=instrument,
        currency=currency,
        tick_size=tick_size,
    )


def _as_commissions(commissions):
    if isinstance(commissions, Commissions):
        return commissions
    return Commissions(**commissions)


def _self_matching_set(self_matching, traders):
    if self_matching is True:
        return set(traders)
    if self_matching is False:
        return set()
    return set(self_matching)


# --------------------------------------------------------------------------
# the fixtures every suite is written against
# --------------------------------------------------------------------------


@pytest.fixture
def engine_factory(tmp_path):
    """Factory building independent engines, each recording to its own file.

    Call it as `engine_factory(commissions=dict(min=2.5, max_pct=1,
    per_unit=0.01), self_matching=(1,), tick_size=0.05)`; every call returns a
    fresh, empty book. Accepted options:

    `traders`
        the trader ids to seed (default: 1-5 and 100-111)
    `commissions`
        the commission schedule every seeded trader gets, as
        `dict(min=..., max_pct=..., per_unit=...)` (default: all zero)
    `self_matching`
        the trader ids allowed to match their own resting orders -- an
        iterable of ids, or True for all (default: none)
    `tick_size`, `instrument`, `currency`

    All engines are closed at teardown.
    """
    built = []
    counter = count(1)

    def factory(**options):
        adapter = build_inmemory(
            tmp_path / ("acceptance%d.db" % next(counter)), **options
        )
        built.append(adapter)
        return adapter

    yield factory

    for adapter in built:
        adapter.close()


@pytest.fixture
def engine(engine_factory):
    """An empty book, with commissions at zero."""
    return engine_factory()


@pytest.fixture
def approx_money():
    """Compare a commission or balance within a floating-point tolerance.

    The commissions contract is explicit that the value is the exact result of
    a floating-point formula with no currency quantization, and that tests
    compare within a tolerance rather than for bit equality::

        assert order.commission == approx_money(2.5)
    """

    def approx(expected):
        return pytest.approx(expected, rel=MONEY_REL, abs=MONEY_ABS)

    return approx
