"""A matching engine written from the frozen specs, to be an oracle.

This is the differential harness's second implementation. It exists to
disagree with `PyLOB.engine` when one of them is wrong, so its only real
requirement is that it be arrived at *independently*: from
`openspec/specs/*/spec.md`, not from the engine's source, and sharing no code
with it.

Three rules hold this module together.

**It imports nothing from `PyLOB`.** Not the engine, not `Side`, not
`quantize_price`. An oracle that borrows the code under test agrees with it by
construction: the day someone "fixes a divergence" by importing the engine's
quantizer, this file stops being a check and starts being a mirror.
`tests/test_differential.py::test_the_reference_imports_nothing_from_the_engine`
reads this file's import statements and asserts it.

**Every rule cites the clause it implements.** The citations are of the form
`order-lifecycle: "Market orders are immediate-or-cancel"` and name a
requirement heading in `openspec/specs/<capability>/spec.md`. A rule with no
citation is a rule someone made up, and the point of the oracle is that
nobody did.

**It is written to be obviously right, not to be fast.** Resting orders are
one flat list; matching re-sorts it by `(price, priority)` every time. That is
O(n log n) where the engine is O(1), and it is the whole idea: the engine's
heaps, level dicts and cached volumes are exactly the machinery a differential
test exists to check, so the model must not have any of it.

Where the model is *weaker* than an independent implementation
--------------------------------------------------------------

ADR-0003 accepts this openly, and these are the specific places:

* **The tie rule in quantization.** `order-lifecycle` requires the nearest
  multiple of the tick and says nothing about a price exactly half a tick from
  two multiples. Both implementations round half to even. That agreement is an
  assumption they share, not a result the oracle checks; an acceptance
  scenario is what pins it.
* **Refusals the specs do not name.** The specs name four invalid submissions
  and the unknown/duplicate identifier cases. Where the engine refuses
  something else -- a market order carrying a price, a cancel of an order
  already cancelled -- the model follows and says so at the rule.
* **Floating-point association.** The model moves a trade's cash leg and its
  commission leg separately, as `trader-balances` and `commissions` describe
  them; the engine debits the sum in one step. `a - v - c` and `a - (v + c)`
  are not the same double, which is why money is compared within a tolerance
  and never for bit equality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

__all__ = [
    "ASK",
    "BID",
    "LIMIT",
    "MARKET",
    "ReferenceBook",
    "ReferenceError",
    "ReferenceInvalid",
    "ReferenceUnknown",
    "RefOrder",
    "RefTrade",
    "RefTrader",
    "commission_for",
    "quantize",
]

#: Sides and order types, spelled as the acceptance surface spells them --
#: plain strings, so nothing here depends on the engine's enums.
BID = "bid"
ASK = "ask"
LIMIT = "limit"
MARKET = "market"

SIDES = (BID, ASK)
ORDER_TYPES = (LIMIT, MARKET)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class ReferenceError(Exception):
    """Base for everything this model refuses.

    `order-lifecycle: "Invalid submissions raise library exceptions"` requires
    a library exception and forbids terminating the process. One base class is
    how a caller catches the model's refusals without catching the engine's.
    """


class ReferenceInvalid(ReferenceError, ValueError):
    """The request is not one the model can carry out."""


class ReferenceUnknown(ReferenceError, LookupError):
    """No order carries that identifier."""


# --------------------------------------------------------------------------
# the tick grid
# --------------------------------------------------------------------------


def quantize(price, tick_size):
    """`price` snapped to the nearest multiple of `tick_size`.

    `order-lifecycle: "Prices are quantized to the tick"` -- "the nearest
    multiple of the configured tick size, for any positive tick size",
    "exact for decimal ticks and correct (nearest multiple) for non-decimal
    ticks".

    Exactness is the whole difficulty, and `Fraction` is how this model gets
    it: `0.05` as a double is 0.05000000000000000277..., so dividing by it in
    floating point puts 100.03 on the wrong side of the 100.05 grid point.
    `Fraction(str(x))` reads the number the caller *wrote* -- the shortest
    decimal that round-trips the double -- and the division and multiplication
    are then exact rationals with no precision to run out of.

    Ties go to the even multiple (`Fraction.__round__` is round-half-to-even).
    The spec does not name a tie rule; see this module's docstring.
    """
    tick = _positive(tick_size, "tick size")
    ratio = _positive(price, "price") / tick
    return float(round(ratio) * tick)


def _positive(value, what):
    """`value` as an exact positive `Fraction`, or a refusal saying why not.

    `bool` is refused before anything else, because `True` is an `int` in
    Python and a price of `True` is a mistake rather than a price.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReferenceInvalid("%s must be a real number, got %r" % (what, value))
    try:
        exact = Fraction(str(value))
    except (ArithmeticError, ValueError, OverflowError) as exc:
        raise ReferenceInvalid("%s is not a usable number: %r" % (what, value)) from exc
    if exact <= 0:
        raise ReferenceInvalid("%s must be positive, got %r" % (what, value))
    return exact


# --------------------------------------------------------------------------
# what the model holds
# --------------------------------------------------------------------------


@dataclass(eq=False)
class RefOrder:
    """One order, from acceptance until the end of the session.

    `eq=False` on purpose: the model tests membership of the resting list with
    `in`, and a field-wise `__eq__` would make two orders that happen to carry
    the same numbers the same order.

    `price` is the quantized working price and is `None` exactly for market
    orders. `value` and `commission` are cumulative over the order's life
    (`commissions: "Commission is recomputed on cumulative fills"`).
    """

    idNum: int
    tid: int
    instrument: str
    side: str
    order_type: str
    price: float | None
    qty: int
    #: Arrival sequence number -- a total order, assigned by the model, never
    #: the caller's timestamp. `order-lifecycle: "Price-time priority is
    #: deterministic"`: "using a total order (arrival sequence number) so that
    #: equal timestamps cannot produce nondeterministic matching".
    priority: int
    timestamp: float = 0.0
    fulfilled: int = 0
    value: float = 0.0
    commission: float = 0.0
    cancelled: bool = False

    @property
    def remaining(self):
        """What this order may still trade.

        `order-matching: "An order never trades beyond its unfulfilled
        remainder"`: "its stated quantity minus what it has already
        fulfilled".
        """
        return self.qty - self.fulfilled

    @property
    def resting(self):
        """Is this order in the book?

        Derived, not stored. `book-queries: "Best and worst prices reflect
        resting limit orders"` says cancelled and fully-filled orders do not
        contribute, and `order-lifecycle: "Market orders are
        immediate-or-cancel"` says a market order never rests at all.

        Deriving it rather than reading the resting list is deliberate: if the
        two ever disagree the book snapshot says so, which is a comparison the
        harness makes on every operation.
        """
        return (
            self.order_type == LIMIT
            and not self.cancelled
            and self.fulfilled < self.qty
        )


@dataclass(frozen=True)
class RefTrade:
    """One execution.

    `order-lifecycle: "Market orders are immediate-or-cancel"` -- "each fill
    prices at the resting order's limit price" -- so `price` is the maker's
    limit and never the taker's.
    """

    instrument: str
    price: float
    qty: int
    taker_side: str
    bid_idNum: int
    bid_tid: int
    ask_idNum: int
    ask_tid: int


@dataclass
class RefTrader:
    """A participant's standing configuration.

    The commission schedule is `commissions: "Commission follows the capped
    per-unit-with-floor formula"`; the flag is `trader-balances:
    "Self-matching is gated per trader"`.
    """

    allow_self_matching: bool = False
    commission_min: float = 0.0
    commission_max_percnt: float = 0.0
    commission_per_unit: float = 0.0


def commission_for(trader, qty, value):
    """`min(max_pct * V / 100, max(min, per_unit * Q))` over cumulative (Q, V).

    `commissions: "Commission follows the capped per-unit-with-floor
    formula"`, quoted whole: "the commission SHALL equal
    `min(max_pct x V / 100, max(min_commission, per_unit x Q))`", "An order
    with no fills SHALL carry zero commission", and "The percentage cap SHALL
    bind ahead of the floor" -- which is why the `max` is inside the `min`.

    No rounding: "Commission SHALL be computed in floating-point with no
    rounding or currency quantization step".
    """
    if qty <= 0:
        return 0.0
    per_unit = max(trader.commission_min, trader.commission_per_unit * qty)
    capped = trader.commission_max_percnt * value / 100.0
    return min(capped, per_unit)


# --------------------------------------------------------------------------
# the book
# --------------------------------------------------------------------------


@dataclass
class ReferenceBook:
    """Every resting order in one flat list, re-sorted whenever anything matches.

    There is no price level, no heap, no cached volume and no index of any
    kind. Every query is a scan and every match is a sort. That is what makes
    this a check on the engine's bookkeeping rather than a second copy of it.

    The bulk collections are `repr=False`: a failing differential run prints
    its own report, and a `ReferenceBook` that dumped every order it has ever
    seen into a traceback would bury it.
    """

    tick_size: float
    #: symbol -> currency (`commissions: "Commission settles in the
    #: instrument's currency"`).
    instruments: dict = field(default_factory=dict)
    traders: dict = field(default_factory=dict, repr=False)
    #: Every order ever accepted, by identifier, kept for the book's lifetime.
    #: `order-lifecycle: "Order identifiers are unique and stable"` makes an
    #: identifier unique "within the book's lifetime", so nothing is pruned.
    orders: dict = field(default_factory=dict, repr=False)
    #: The book itself.
    _resting: list = field(default_factory=list, repr=False)
    _trades: list = field(default_factory=list, repr=False)
    _balances: dict = field(default_factory=dict, repr=False)
    _last_price: dict = field(default_factory=dict)
    _next_priority: int = 1
    _next_idNum: int = 1

    # -- configuration -----------------------------------------------------

    def configure_instrument(self, symbol, currency):
        """Declare an instrument and the currency it settles in."""
        self.instruments[symbol] = currency

    def configure_trader(self, tid, **schedule):
        """Declare a trader's commission schedule and self-matching flag."""
        self.traders[tid] = RefTrader(**schedule)

    def trader(self, tid):
        """`tid`'s configuration; an unconfigured trader owes nothing and is gated.

        `trader-balances: "Self-matching is gated per trader"` gates unless the
        flag "is set", and `commissions` charges the trader's "configured"
        parameters -- so the default of an unmentioned trader is zero
        commission and no self-matching.
        """
        return self.traders.setdefault(tid, RefTrader())

    # -- submitting --------------------------------------------------------

    def submit(
        self,
        tid,
        instrument,
        side,
        order_type,
        qty,
        price=None,
        idNum=None,
        timestamp=None,
    ):
        """Accept an order, cross it, then rest or cancel what is left.

        Returns `(order, trades)`.

        The refusals are `order-lifecycle: "Invalid submissions raise library
        exceptions"` -- "non-positive quantity, unknown order type, unknown
        side, or (for limit orders) missing price" -- plus the identifier
        rules under `order-lifecycle: "Order identifiers are unique and
        stable"`. Every check runs before any state changes, so a refused
        submission leaves the book exactly as it was.

        A market order carrying a price is refused too. The specs give a
        market order no price of its own ("each fill prices at the resting
        order's limit price"), so a price on one is not a submission this
        model can interpret; the engine refuses it as well.
        """
        if side not in SIDES:
            raise ReferenceInvalid("unknown side %r" % (side,))
        if order_type not in ORDER_TYPES:
            raise ReferenceInvalid("unknown order type %r" % (order_type,))
        _check_qty(qty)

        if order_type == LIMIT:
            if price is None:
                raise ReferenceInvalid("a limit order needs a price")
            working = self.quantize(price)
        else:
            if price is not None:
                raise ReferenceInvalid(
                    "a market order takes no price, got %r" % (price,)
                )
            working = None

        order = RefOrder(
            idNum=self._assign_idNum(idNum),
            tid=tid,
            instrument=instrument,
            side=side,
            order_type=order_type,
            price=working,
            qty=qty,
            timestamp=timestamp if timestamp is not None else 0.0,
            priority=self._stamp(),
        )
        self.orders[order.idNum] = order
        self.instruments.setdefault(instrument, None)

        trades = self._match(order)
        if order.remaining > 0:
            if order_type == MARKET:
                # `order-lifecycle: "Market orders are immediate-or-cancel"`:
                # "any unfilled remainder SHALL be cancelled" and "A market
                # order SHALL never rest in the book".
                order.cancelled = True
            else:
                self._resting.append(order)
        return order, trades

    def cancel(self, idNum, side=None):
        """Take one order out of the book at its owner's request.

        `order-lifecycle: "Order identifiers are unique and stable"`: "Cancel
        targets exactly one order", and an identifier no order has raises with
        the book unchanged.

        Two further refusals the specs do not name, where the engine refuses
        and this model follows: an order already cancelled, and an order with
        nothing left to cancel. A cancel that can remove nothing is not a
        cancel, and reporting success for one is how a caller loses an order
        it believes it withdrew.

        Commission already charged stays charged -- `commissions: "Cancelling
        a partially filled order keeps its commission"` -- which is why
        nothing here touches a balance.
        """
        order = self.require(idNum)
        if side is not None and side != order.side:
            raise ReferenceInvalid("order %r is a %s" % (idNum, order.side))
        if order.cancelled:
            raise ReferenceInvalid("order %r is already cancelled" % (idNum,))
        if order.remaining <= 0:
            raise ReferenceInvalid("order %r has nothing left to cancel" % (idNum,))
        self._unrest(order)
        order.cancelled = True
        return order

    def modify(self, idNum, qty=None, price=None, side=None):
        """Change a resting order's price or quantity; return what it traded.

        `order-lifecycle: "Modify is validated and priority-aware"`, clause by
        clause:

        * "reject a side change" -- a `side` that is not the order's raises
          and the order is unchanged;
        * "clamp a quantity reduction below the already-fulfilled amount to
          the fulfilled amount";
        * "a price change or a quantity increase moves the order to the back
          of its price level's queue; a pure quantity decrease keeps its
          place" -- so a fresh priority stamp is taken in the first case and
          withheld in the second.

        `qty=None` and `price=None` each mean *leave that one alone*. A
        price=None is emphatically not "become a market order": an order that
        named a limit keeps it until someone names a different one.

        A non-passive modification is a re-submission at new terms: the order
        leaves the book, crosses as a taker, and only what survives goes back
        in.
        """
        order = self.require(idNum)
        if side is not None and side != order.side:
            raise ReferenceInvalid("order %r is a %s" % (idNum, order.side))
        if order.cancelled:
            raise ReferenceInvalid("order %r is cancelled" % (idNum,))
        if order.order_type != LIMIT:
            raise ReferenceInvalid("order %r is a market order" % (idNum,))
        if qty is not None:
            _check_qty(qty)

        prev_price, prev_qty = order.price, order.qty
        new_price = prev_price if price is None else self.quantize(price)
        new_qty = max(prev_qty if qty is None else qty, order.fulfilled)
        reprioritized = new_price != prev_price or new_qty > prev_qty

        if not reprioritized:
            order.qty = new_qty
            if order.remaining <= 0:
                self._unrest(order)
            return []

        self._unrest(order)
        order.price = new_price
        order.qty = new_qty
        order.priority = self._stamp()
        trades = self._match(order)
        if order.remaining > 0:
            self._resting.append(order)
        return trades

    # -- matching ----------------------------------------------------------

    def _match(self, taker):
        """Trade `taker` against the opposite side as far as it is entitled to.

        `order-lifecycle: "Price-time priority is deterministic"`: "Orders at
        a better price SHALL match first; among equal prices, arrival order
        SHALL decide" -- which is the sort in `book`, and nothing else decides
        anything here.

        The walk stops at the taker's limit (`order-matching`: an order trades
        only what it is eligible for, and a limit order is not eligible above
        its own price); a market order has no limit and crosses every level.

        `trader-balances: "Self-matching is gated per trader"`: a gated
        maker is skipped, "remain[s] in the book, with matching continuing
        past them in priority order".

        `order-matching: "An order never trades beyond its unfulfilled
        remainder"` bounds the whole walk -- which matters most for a repriced
        order that already has fills against it.
        """
        trades = []
        opposite = ASK if taker.side == BID else BID
        gated = not self.trader(taker.tid).allow_self_matching

        for maker in self.book(taker.instrument, opposite):
            if taker.remaining <= 0:
                break
            if taker.price is not None:
                if opposite == ASK and maker.price > taker.price:
                    break
                if opposite == BID and maker.price < taker.price:
                    break
            if gated and maker.tid == taker.tid:
                continue
            trades.append(self._execute(taker, maker))
        return trades

    def _execute(self, taker, maker):
        """One execution: fill both orders, charge both, move four balances."""
        price = maker.price
        qty = min(taker.remaining, maker.remaining)
        value = qty * price

        # `order-matching: "Fills are credited to the orders that traded"`:
        # "credited to exactly the two orders that participated ... and no
        # others".
        taker.fulfilled += qty
        maker.fulfilled += qty
        taker_delta = self._charge(taker, value)
        maker_delta = self._charge(maker, value)

        # `book-queries: "Last-trade price is reporting, not matching state"`:
        # "the price of the most recent trade per instrument". The most recent
        # trade priced at the maker, so this is the maker's price and never
        # the taker's limit.
        self._last_price[taker.instrument] = price

        if maker.remaining <= 0:
            self._unrest(maker)

        if taker.side == BID:
            bid, ask = taker, maker
            bid_delta, ask_delta = taker_delta, maker_delta
        else:
            bid, ask = maker, taker
            bid_delta, ask_delta = maker_delta, taker_delta
        self._settle(taker.instrument, bid, ask, qty, value, bid_delta, ask_delta)

        trade = RefTrade(
            instrument=taker.instrument,
            price=price,
            qty=qty,
            taker_side=taker.side,
            bid_idNum=bid.idNum,
            bid_tid=bid.tid,
            ask_idNum=ask.idNum,
            ask_tid=ask.tid,
        )
        self._trades.append(trade)
        return trade

    def _charge(self, order, value):
        """Add one fill to `order`'s totals; return the commission increment.

        `commissions: "Commission is recomputed on cumulative fills"`: "a
        function of the order's cumulative fills, recomputed as fills accrue
        (not summed per trade), and the trader SHALL be charged exactly the
        recomputed total -- increments debit only the difference".
        """
        order.value += value
        charged = commission_for(self.trader(order.tid), order.fulfilled, order.value)
        delta = charged - order.commission
        order.commission = charged
        return delta

    def _settle(self, instrument, bid, ask, qty, value, bid_delta, ask_delta):
        """The four movements of one trade, then the two commission debits.

        `trader-balances: "Trades move balances symmetrically"`: "the buyer's
        instrument balance SHALL increase by Q and their C balance decrease by
        Q x P; the seller's instrument balance SHALL decrease by Q and their C
        balance increase by Q x P".

        `commissions: "Commission settles in the instrument's currency"`:
        "debited from the trader's balance in the currency of the traded
        instrument" -- a separate movement from the cash leg, which is how the
        spec describes it and why money is compared within a tolerance.

        An instrument with no declared currency moves its own leg and nothing
        else; the fill is still recorded and the cash leg is recoverable from
        it.
        """
        self._credit(bid.tid, instrument, float(qty))
        self._credit(ask.tid, instrument, -float(qty))

        currency = self.instruments.get(instrument)
        if currency is None:
            return
        self._credit(bid.tid, currency, -value)
        self._credit(ask.tid, currency, value)
        self._credit(bid.tid, currency, -bid_delta)
        self._credit(ask.tid, currency, -ask_delta)

    def _credit(self, tid, symbol, amount):
        """Move one balance.

        `trader-balances: "Balances track, they do not gate"`: "no margin
        check, no sufficient-funds check, negative balances are permitted and
        recorded".
        """
        key = (tid, symbol)
        self._balances[key] = self._balances.get(key, 0.0) + amount

    # -- reading -----------------------------------------------------------

    def book(self, instrument, side):
        """Resting orders on one side, in matching priority order.

        `book-queries: "Book snapshot is complete and consistent"`: "every
        resting order ... exactly once, ordered by matching priority". The
        sort key is the whole of `order-lifecycle: "Price-time priority is
        deterministic"`: better price first -- highest for a bid, lowest for
        an ask -- then arrival sequence.
        """
        rest = [
            order
            for order in self._resting
            if order.instrument == instrument and order.side == side
        ]
        rest.sort(key=lambda o: (-o.price if side == BID else o.price, o.priority))
        return rest

    def best(self, instrument, side):
        """Best resting price on a side, or None when the side is empty.

        `book-queries: "Best and worst prices reflect resting limit orders"`:
        highest bid, lowest ask, "`None` only when that side of the book is
        empty".
        """
        prices = [order.price for order in self.book(instrument, side)]
        if not prices:
            return None
        return max(prices) if side == BID else min(prices)

    def worst(self, instrument, side):
        """The opposite extreme from `best`, or None when the side is empty."""
        prices = [order.price for order in self.book(instrument, side)]
        if not prices:
            return None
        return min(prices) if side == BID else max(prices)

    def volume_at(self, instrument, side, price):
        """What an opposite order priced at `price` could take from `side`.

        `book-queries: "Volume at price answers the marketable question"`:
        "the total unfulfilled quantity of resting S-side orders that an
        opposite-side order priced at P would be eligible to match (bids
        priced >= P when S is bid; asks priced <= P when S is ask), and 0 when
        nothing qualifies".

        The query price goes on the grid first: a price off the grid is not a
        price this book can hold, so asking about it is asking about the grid
        point it names.
        """
        price = self.quantize(price)
        orders = self.book(instrument, side)
        if side == BID:
            return sum(o.remaining for o in orders if o.price >= price)
        return sum(o.remaining for o in orders if o.price <= price)

    def last_price(self, instrument):
        """The most recent trade price in `instrument`, or None before the first."""
        return self._last_price.get(instrument)

    def balance(self, tid, symbol):
        """`tid`'s holding of an instrument or a currency; 0 before any movement."""
        return self._balances.get((tid, symbol), 0.0)

    def trades(self, instrument):
        """Every execution in `instrument`, oldest first."""
        return [trade for trade in self._trades if trade.instrument == instrument]

    def order(self, idNum):
        """The order with that identifier, or None."""
        return self.orders.get(idNum)

    def require(self, idNum):
        """The order with that identifier, or a refusal.

        `order-lifecycle: "Order identifiers are unique and stable"`, scenario
        "Unknown identifier raises": "cancel or modify is called with an
        identifier no order has" raises "and the book is unchanged".
        """
        order = self.orders.get(idNum)
        if order is None:
            raise ReferenceUnknown("no order %r" % (idNum,))
        return order

    def quantize(self, price):
        """`price` on this book's tick grid, refusing what cannot be held."""
        working = quantize(price, self.tick_size)
        if working <= 0:
            raise ReferenceInvalid(
                "price %r quantizes to %r: under half a tick is not a price"
                % (price, working)
            )
        return working

    # -- internals ---------------------------------------------------------

    def _stamp(self):
        """The next arrival sequence number."""
        stamp = self._next_priority
        self._next_priority += 1
        return stamp

    def _assign_idNum(self, idNum):
        """Allocate or accept an identifier, keeping it unique for all time.

        `order-lifecycle: "Order identifiers are unique and stable"`: "unique
        within the book's lifetime, including across reloads of persisted
        state", and the scenario "Externally supplied duplicate is rejected".

        A supplied identifier at or above the counter pushes the counter past
        itself, which is what makes uniqueness survive a reload that replays
        every identifier it saw.
        """
        if idNum is None:
            idNum = self._next_idNum
            self._next_idNum += 1
            return idNum
        if not isinstance(idNum, int) or isinstance(idNum, bool):
            raise ReferenceInvalid("idNum must be an integer, got %r" % (idNum,))
        if idNum in self.orders:
            raise ReferenceInvalid("idNum %r is already in use" % (idNum,))
        if idNum >= self._next_idNum:
            self._next_idNum = idNum + 1
        return idNum

    def _unrest(self, order):
        """Take `order` out of the resting list if it is in it."""
        for index, resting in enumerate(self._resting):
            if resting is order:
                del self._resting[index]
                return True
        return False


def _check_qty(qty):
    """A quantity the model will work with.

    `order-lifecycle: "Invalid submissions raise library exceptions"`,
    scenario "Non-positive quantity": "an exception is raised, no state
    changes, and the process continues".
    """
    if isinstance(qty, bool) or not isinstance(qty, int):
        raise ReferenceInvalid("quantity must be a whole number, got %r" % (qty,))
    if qty <= 0:
        raise ReferenceInvalid("quantity must be positive, got %r" % (qty,))
