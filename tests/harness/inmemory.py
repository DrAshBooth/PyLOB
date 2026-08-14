"""`PyLOB.engine.OrderBook` behind the engine-neutral surface.

ADR-0003 retired the second engine this used to sit beside (the SQL
`LegacyOrderBook`), and with it the registry that parameterized every
acceptance suite over both and the `engine_xfail` marker that recorded where
the two diverged. `build_inmemory` is the one builder left; a future second
engine wants its own module here, against `harness.surface`, and a params
list back over the two.
"""

from dataclasses import asdict

from PyLOB import replay
from PyLOB.engine import OrderBook
from PyLOB.sinks import ListSink
from PyLOB.sinks.sqlite import SQLiteSink, read_events

from .surface import (
    CURRENCY,
    DEFAULT_TICK,
    INSTRUMENT,
    NO_COMMISSION,
    TRADERS,
    UNSET,
    BookEntry,
    OrderRef,
    OrderState,
    Trade,
    as_commissions,
    self_matching_set,
)


class _Recorder:
    """A `ListSink` teed onto the session's real sink, for `recorded()`.

    An engine takes one sink, and the acceptance engines already spend theirs
    on the `SQLiteSink` that `reopen()` reloads from. A suite that has to read
    the stream back therefore needs the stream to go two places at once, and
    this is the whole of that: it forwards each event on, in order, and keeps
    a copy. It reads nothing and calls nothing back, which is the contract
    `events.EventSink` puts on a sink and the reason this is not simply a
    second `OrderBook` reference.

    Off by default -- `build_inmemory(capture=True)` -- so a suite that never
    asks for the stream pays nothing for it.
    """

    __slots__ = ("_sink", "_captured")

    def __init__(self, sink):
        self._sink = sink
        self._captured = ListSink()

    def consume(self, event, /):
        self._captured.consume(event)
        self._sink.consume(event)

    def close(self):
        self._sink.close()

    @property
    def events(self):
        return self._captured.events


class InMemoryAdapter:
    """`PyLOB.engine.OrderBook` behind the engine-neutral acceptance surface.

    Observation reads the engine's own objects -- an `Order` for order state,
    a book snapshot for the queue, the in-core ledger for a balance -- and the
    trade log is the executions the engine returned from each operation.

    A `SQLiteSink` records the session to `db_path`, because `reopen()` needs
    somewhere to reload *from*: this engine's persisted state is its event
    stream, not a book on disk.
    """

    def __init__(self, book, db_path, instrument, currency, tick_size, recorder=None):
        self.book = book
        self.db_path = db_path
        self.instrument = instrument
        self.currency = currency
        self.tick_size = tick_size
        self._trades = []
        self._reloads = 0
        self._recorder = recorder

    # -- operations --------------------------------------------------------

    def limit(self, side, qty, price, tid, **kwargs):
        """Submit a limit order; return an `OrderRef`."""
        return self._submit("limit", side, qty, tid, price=price, **kwargs)

    def market(self, side, qty, tid, **kwargs):
        """Submit a market order; return an `OrderRef`."""
        return self._submit("market", side, qty, tid, price=None, **kwargs)

    def cancel(self, order, side=UNSET, timestamp=None, keyword=False):
        """Cancel an order, addressed by `OrderRef` or by raw identifier.

        `side=None` names no side, so the order is addressed by identifier
        alone; leaving it unsupplied takes the side from the reference.

        `keyword=True` reaches the library's keyword-addressed spelling rather
        than its positional one -- the two operations `order-lifecycle`
        requires to be indistinguishable in the recorded stream. Which
        spelling is which, and what each names its clock, is engine knowledge
        and lives here rather than in a suite: an engine that renamed the
        keyword surface's `timestamp` would break this line, which is the
        point of writing it out.
        """
        idNum, side = self._target(order, side)
        if keyword:
            self.book.cancel(idNum, side=side, timestamp=timestamp)
        else:
            self.book.cancelOrder(side, idNum, timestamp)

    def modify(
        self,
        order,
        qty=UNSET,
        price=UNSET,
        side=UNSET,
        tid=UNSET,
        timestamp=None,
        keyword=False,
    ):
        """Modify an order; return the executions the modification produced.

        `timestamp` and `keyword` mean what they mean for `cancel`. The
        keyword spelling reads an unsupplied `qty` or `price` as "leave that
        one alone" itself, so this passes on only what it was given; the
        positional one requires all three fields, so this fills the missing
        ones in from the order's current state. Both are "leave it alone", and
        the two produce the same event -- which is a claim a suite tests
        rather than one this docstring settles.
        """
        idNum, side = self._target(order, side)
        if keyword:
            named = {}
            if qty is not UNSET:
                named["qty"] = qty
            if price is not UNSET:
                named["price"] = price
            _, trades = self.book.modify(idNum, side=side, timestamp=timestamp, **named)
            return self._record(trades)

        state = self.order_state(idNum)
        if qty is UNSET:
            qty = state.qty if state else None
        if price is UNSET:
            price = state.price if state else None
        if tid is UNSET:
            tid = self._owner(idNum)

        trades, _ = self.book.modifyOrder(
            idNum, dict(side=side, qty=qty, price=price, tid=tid), timestamp
        )
        return self._record(trades)

    def reopen(self):
        """Rebuild the engine from persisted state; return self.

        For this engine the persisted state is the event stream, and
        `PyLOB.replay` is what re-issues one -- the same shipped function
        `tests/test_replay.py` compares end states with, so this surface
        reloads the way a researcher does. The sink is flushed and closed, the
        events are read back, and the commands among them are re-issued into a
        fresh book that derives every trade for itself. Nothing carries over
        in memory: the trade log here is replaced by the replay's, so what it
        holds afterwards is what the new engine produced, not what the old one
        did.

        The reloaded session records to a new file, since the log's `seq` is
        its primary key and a second session cannot append to the first. A
        capturing adapter gets a fresh capture with it: `recorded()` means what
        *this* session recorded, and after a reload that is the replay's stream
        and not the stream it was replayed from.
        """
        self.book.close()

        events = list(read_events(self.db_path, replayable_only=True))
        self._reloads += 1
        self.db_path = self.db_path.with_name(
            "%s.reload%d.db" % (self.db_path.stem, self._reloads)
        )

        sink = SQLiteSink(self.db_path)
        if self._recorder is not None:
            self._recorder = _Recorder(sink)
            sink = self._recorder
        self.book, trades = replay(events, sink=sink)
        self._trades = list(trades)
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

    def depth(self, side, levels=None, instrument=None):
        """The aggregated price ladder for `side`: (price, volume), best first.

        `levels` bounds it to the best N and is passed through untouched, so a
        suite can hand it a bound the contract says has to be refused.
        """
        return self.book.depth(instrument or self.instrument, side, levels)

    def recorded(self):
        """Every event this session recorded, as plain dicts, in stream order.

        `kind` plus the event's own fields, so a suite can compare two recorded
        streams, or read the stamp an operation put on its event, without
        importing an engine's event classes. Needs `capture=True` at build
        time, because the stream has to be teed to get here.
        """
        assert self._recorder is not None, (
            "this engine is not capturing its stream: build it with "
            "engine_factory(capture=True)"
        )
        return tuple(
            dict(kind=type(event).KIND, **asdict(event))
            for event in self._recorder.events
        )

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
    instruments=(),
    tick_size=DEFAULT_TICK,
    capture=False,
):
    """Build the in-memory engine (ADR-0001, epic lob-5rt), recording to `db_path`.

    `instrument`/`currency` are the *default* instrument -- the one every
    surface call means when it names none. `instruments` are further
    `(symbol, currency)` pairs to declare beside it, for a suite that has to
    say what is scoped to one instrument and what is not; each gets its own
    settlement currency, so a second instrument may settle in a second
    currency.

    Configuration goes through the engine's own calls rather than through
    seeded rows, so the commission schedule and the self-matching flags reach
    the recorded stream as `TraderConfigured` events, and each instrument as
    an `InstrumentConfigured` -- which is what makes `reopen()` able to
    rebuild every book, charging the same commissions and settling in the
    same currencies.

    `capture` tees the recorded stream into memory as well, which is what
    `InMemoryAdapter.recorded` reads. Off by default: a suite that never asks
    for the stream should not pay a list append per event for it.
    """
    schedule = as_commissions(commissions)
    allowed = self_matching_set(self_matching, traders)

    sink = SQLiteSink(db_path)
    recorder = _Recorder(sink) if capture else None
    book = OrderBook(tick_size=tick_size, sink=recorder or sink)
    book.configure_instrument(instrument, currency)
    for symbol, settles_in in instruments:
        book.configure_instrument(symbol, settles_in)
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
        recorder=recorder,
    )
