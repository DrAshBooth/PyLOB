"""`ReferenceBook` behind the acceptance suites' engine-neutral surface.

`matcher.py` is the oracle and knows nothing about how the tests talk; this is
the translation layer, and it is deliberately thin. Everything it does is
either renaming (`book` -> `snapshot`) or repackaging (a `RefOrder` as a
`BookEntry`). No rule lives here -- if a decision about *behaviour* appears in
this file it is in the wrong file, because a rule with nowhere to cite a spec
clause is a rule nobody derived.

The surface is the one `tests/harness/` documents:

operations
    `limit`, `market`, `cancel`, `modify`, `reopen`, `close`
observation
    `order_state`, `snapshot`, `trades`, `best`, `worst`, `volume_at`,
    `last_price`, `balance`

Speaking exactly that surface is what lets `tests/test_differential.py` drive
the model and the engine through one code path and compare the two.
"""

from harness import (
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

from .matcher import ReferenceBook

__all__ = ["ReferenceAdapter", "build_reference"]


class ReferenceAdapter:
    """The spec-derived model, speaking the acceptance surface."""

    def __init__(self, book, db_path, instrument, currency, tick_size):
        self.book = book
        self.db_path = db_path
        self.instrument = instrument
        self.currency = currency
        self.tick_size = tick_size

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
        self.book.cancel(idNum, side)

    def modify(self, order, qty=UNSET, price=UNSET, side=UNSET, tid=UNSET):
        """Modify an order; return the executions the modification produced.

        Anything not passed keeps the order's current value; `price=None` is
        passed through as an explicit None, because "leave the price alone" is
        a scenario in its own right and not a default. This is the same
        defaulting the engine's adapter does, and it has to be, or the two
        sides of the comparison would not be sent the same request.
        """
        idNum, side = self._target(order, side)
        state = self.order_state(idNum)
        if qty is UNSET:
            qty = state.qty if state else None
        if price is UNSET:
            price = state.price if state else None
        return self._as_trades(self.book.modify(idNum, qty=qty, price=price, side=side))

    def reopen(self):
        """Reload persisted state; return self.

        The model persists nothing, so there is nothing to reload and this is
        a no-op -- which is exactly what makes it useful. The engine rebuilds
        itself from its event stream here; the model simply carries on, and
        the harness then compares the two. Any way in which a reloaded engine
        differs from the session it replaced shows up as a divergence from a
        book that never went away.
        """
        return self

    def close(self):
        """Nothing to close: no file, no connection, no buffer."""

    # -- observation -------------------------------------------------------

    def order_state(self, idNum):
        """`OrderState` for `idNum`, or None if no order has that identifier."""
        order = self.book.order(idNum)
        if order is None:
            return None
        return OrderState(
            idNum=order.idNum,
            side=order.side,
            order_type=order.order_type,
            price=order.price,
            qty=order.qty,
            fulfilled=order.fulfilled,
            cancelled=order.cancelled,
            resting=order.resting,
            commission=order.commission,
        )

    def snapshot(self, side, instrument=None):
        """Resting orders on `side`, in matching priority order."""
        return tuple(
            BookEntry(
                idNum=order.idNum,
                price=order.price,
                available=order.remaining,
                qty=order.qty,
                fulfilled=order.fulfilled,
            )
            for order in self.book.book(instrument or self.instrument, side)
        )

    def trades(self, instrument=None):
        """Every execution in the instrument, oldest first."""
        return self._as_trades(self.book.trades(instrument or self.instrument))

    def best(self, side, instrument=None):
        return self.book.best(instrument or self.instrument, side)

    def worst(self, side, instrument=None):
        return self.book.worst(instrument or self.instrument, side)

    def volume_at(self, side, price, instrument=None):
        return self.book.volume_at(instrument or self.instrument, side, price)

    def last_price(self, instrument=None):
        return self.book.last_price(instrument or self.instrument)

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
            # What the engine's adapter does with a bare idNum, kept so both
            # sides see the same replayed clock.
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
        return OrderRef(self, order.idNum, side, self._as_trades(trades))

    def _as_trades(self, trades):
        """`RefTrade`s in the surface's vocabulary."""
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


def build_reference(
    db_path,
    traders=TRADERS,
    commissions=NO_COMMISSION,
    self_matching=(),
    instrument=INSTRUMENT,
    currency=CURRENCY,
    tick_size=DEFAULT_TICK,
):
    """Build the reference model, configured like any other engine under test.

    Same signature as `build_inmemory`, so a caller can build the two the same
    way and hold them side by side. `db_path` is accepted and ignored: the
    model has no persistence, and taking the argument is what keeps the two
    builders interchangeable.
    """
    schedule = as_commissions(commissions)
    allowed = self_matching_set(self_matching, traders)

    book = ReferenceBook(tick_size=tick_size)
    book.configure_instrument(instrument, currency)
    for tid in traders:
        book.configure_trader(
            tid,
            allow_self_matching=tid in allowed,
            commission_min=schedule.min,
            commission_max_percnt=schedule.max_pct,
            commission_per_unit=schedule.per_unit,
        )

    return ReferenceAdapter(
        book,
        db_path=db_path,
        instrument=instrument,
        currency=currency,
        tick_size=tick_size,
    )
