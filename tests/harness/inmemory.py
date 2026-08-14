"""`PyLOB.engine.OrderBook` behind the engine-neutral surface.

ADR-0003 retired the second engine this used to sit beside (the SQL
`LegacyOrderBook`), and with it the registry that parameterized every
acceptance suite over both and the `engine_xfail` marker that recorded where
the two diverged. `build_inmemory` is the one builder left; a future second
engine wants its own module here, against `harness.surface`, and a params
list back over the two.
"""

from PyLOB import replay
from PyLOB.engine import OrderBook
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
        its primary key and a second session cannot append to the first.
        """
        self.book.close()

        events = list(read_events(self.db_path, replayable_only=True))
        self._reloads += 1
        self.db_path = self.db_path.with_name(
            "%s.reload%d.db" % (self.db_path.stem, self._reloads)
        )

        self.book, trades = replay(events, sink=SQLiteSink(self.db_path))
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
    schedule = as_commissions(commissions)
    allowed = self_matching_set(self_matching, traders)

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
