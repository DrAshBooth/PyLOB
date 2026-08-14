"""The engine-neutral vocabulary every adapter speaks.

The values an adapter returns (`OrderState`, `BookEntry`, `Trade`,
`OrderRef`), the `UNSET` sentinel that distinguishes "argument not supplied"
from an explicitly supplied `None`, the configuration an engine is built with
(`Commissions`, the default instrument/currency/tick, the seeded trader ids)
and the tolerance money is compared within.

No engine is imported here. That is what lets `harness.inmemory` and
`tests/reference/adapter.py` both build these types and have `isinstance`
hold across them.
"""

from typing import NamedTuple, Optional

INSTRUMENT = "FAKE"
CURRENCY = "USD"
DEFAULT_TICK = 0.0001

# Low ids so a suite can use the trader numbers its scenario text uses
# ("trader 2 buys 5 @ 101 from trader 1"), plus example.py's block.
TRADERS = (1, 2, 3, 4, 5) + tuple(range(100, 112))

# Commission and balance are floating-point sums with no quantization step, so
# the commissions contract requires comparison within a tolerance rather than
# for bit equality. The `approx_money` fixture is the only sanctioned
# comparison inside the acceptance suites; the suites outside them
# (`test_differential`, `test_replay`) build their own from these two.
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
# what every builder normalises the same way
# --------------------------------------------------------------------------


def as_commissions(commissions):
    if isinstance(commissions, Commissions):
        return commissions
    return Commissions(**commissions)


def self_matching_set(self_matching, traders):
    if self_matching is True:
        return set(traders)
    if self_matching is False:
        return set()
    return set(self_matching)
