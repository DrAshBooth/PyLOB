"""Fixtures shared by the acceptance suites.

The acceptance suites encode the *frozen contracts* under
`openspec/changes/spec-book-queries/specs/` and
`openspec/changes/spec-commissions-balances/specs/` -- target behaviour, not
what any particular engine does today. Every scenario therefore runs against
every engine under test, and the differences between engines live here rather
than in the suites.

Three seams:

1. **`ENGINES`** -- the registry of engines under test. One entry per engine;
   the `engine` fixture is parameterized over it, so a new engine reaches all
   four suites without a line changing in any of them. An engine whose module
   is not importable yet is *skipped*, not failed: today only the legacy SQL
   `OrderBook` exists and every `inmemory` parameterization skips.

2. **The adapter** (`LegacyAdapter`) -- one engine-neutral vocabulary for
   submitting orders and observing the result. The suites never touch SQL, the
   `trade_order` table, or `processOrder`'s dict quotes; they call
   `engine.limit(...)`, read `order.fulfilled`, ask for `engine.snapshot("bid")`.
   That is what lets the same test body run against an engine that has no
   database at all.

3. **`@pytest.mark.engine_xfail`** -- the declarative record of a known
   divergence, per (engine, scenario)::

       @pytest.mark.engine_xfail("legacy", "lob-0bl: rests the remainder")
       def test_market_remainder_is_cancelled(engine): ...

   The marker is applied at collection, strict by default: when the engine is
   fixed the test xpasses, the run goes red, and the stale marker has to go.
   No `if engine.id == ...` anywhere in a suite.

Each engine gets its own sqlite *file* under `tmp_path`, built from
`src/create_lob.sql` -- the same fresh-database pattern as `tests/conftest.py`,
with commission parameters and the self-matching flag made settable. The
committed `src/lob.db` is never opened.
"""

import sqlite3
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from itertools import count
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

# `tests/conftest.py` does this too; repeating it keeps this module importable
# on its own terms rather than by ancestor-conftest side effect.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PyLOB import OrderBook  # noqa: E402  (import needs the sys.path above)

SCHEMA = SRC / "create_lob.sql"

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
# the legacy SQL engine, behind the adapter surface
# --------------------------------------------------------------------------

SEED_TRADER = """
insert into trader (
    tid, name, allow_self_matching,
    commission_min, commission_max_percnt, commission_per_unit
)
values (
    :tid, :name, :allow_self_matching,
    :commission_min, :commission_max_percnt, :commission_per_unit
)
on conflict do nothing;"""

SEED_INSTRUMENT = """
insert into instrument (symbol, currency)
values (:symbol, :currency)
on conflict do nothing;"""

ORDER_STATE = """
select idNum, side, order_type, price, qty, fulfilled, cancel, active, commission
from trade_order
where idNum = :idNum
"""

ORDER_OWNER = "select trader from trade_order where idNum = :idNum"

TRADE_LOG = """
select bid_order.idNum, ask_order.idNum, trade.price, trade.qty
from trade
inner join trade_order as bid_order on bid_order.order_id = trade.bid_order
inner join trade_order as ask_order on ask_order.order_id = trade.ask_order
where bid_order.instrument = :instrument
order by trade.trade_id
"""

BALANCE = """
select amount
from trader_balance
where trader = :tid and instrument = :instrument
"""

LAST_PRICE = "select lastprice from instrument where symbol = :instrument"


class LegacyAdapter:
    """The legacy SQL `OrderBook` behind the engine-neutral acceptance surface.

    Any further engine registered in `ENGINES` implements the same surface:

    operations
        `limit`, `market`, `cancel`, `modify`, `reopen`, `close`
    observation
        `order_state`, `snapshot`, `trades`, `best`, `worst`, `volume_at`,
        `last_price`, `balance`

    Everything below the surface -- SQL, `processOrder` dict quotes, the
    `trade_order` table -- is this class's business alone.
    """

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
        """Cancel an order, addressed by `OrderRef` or by raw identifier.

        `side` defaults to the order's own side; pass it to address the order
        by the wrong side deliberately.
        """
        idNum, side = self._target(order, side)
        self.book.cancelOrder(side, idNum)

    def modify(self, order, qty=UNSET, price=UNSET, side=UNSET, tid=UNSET):
        """Modify an order; return the executions the modification produced.

        Anything not passed keeps the order's current value. `price=None` is
        passed through as an explicit None -- it is a scenario, not a default.
        """
        idNum, side = self._target(order, side)
        state = self.order_state(idNum)
        if qty is UNSET:
            qty = state.qty if state else None
        if price is UNSET:
            price = state.price if state else None
        if tid is UNSET:
            tid = self._owner(idNum)

        before = len(self.trades())
        self.book.modifyOrder(idNum, dict(side=side, qty=qty, price=price, tid=tid))
        return self.trades()[before:]

    def reopen(self):
        """Reload the engine from persisted state; return self.

        Everything the engine held in memory is dropped and rebuilt from the
        database file -- the "survives reload" scenarios' whole point.
        """
        self.book.db.close()
        self.book = OrderBook(db=_connect(self.db_path), tick_size=self.tick_size)
        return self

    def close(self):
        self.book.db.close()

    # -- observation -------------------------------------------------------

    def order_state(self, idNum):
        """`OrderState` for `idNum`, or None if no order has that identifier."""
        row = self._cursor().execute(ORDER_STATE, dict(idNum=idNum)).fetchone()
        if row is None:
            return None
        idNum, side, order_type, price, qty, fulfilled, cancel, active, commission = row
        return OrderState(
            idNum=idNum,
            side=side,
            order_type=order_type,
            price=price,
            qty=qty,
            fulfilled=fulfilled,
            cancelled=bool(cancel),
            resting=bool(active) and not cancel and fulfilled < qty,
            commission=commission,
        )

    def snapshot(self, side, instrument=None):
        """Resting orders on `side`, in the engine's own matching priority order."""
        sql = self.book.active_orders + self.book.best_quotes_order_asc
        params = dict(instrument=instrument or self.instrument, side=side)
        return tuple(
            BookEntry(
                idNum=idNum,
                price=price,
                available=qty - fulfilled,
                qty=qty,
                fulfilled=fulfilled,
            )
            for idNum, qty, fulfilled, price, _event_dt, _instrument in self._cursor()
            .execute(sql, params)
            .fetchall()
        )

    def trades(self, instrument=None):
        """Every execution in the instrument, oldest first."""
        params = dict(instrument=instrument or self.instrument)
        return tuple(
            Trade(*row) for row in self._cursor().execute(TRADE_LOG, params).fetchall()
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
        """The recorded last-trade price -- the persisted one, so it survives reload."""
        params = dict(instrument=instrument or self.instrument)
        row = self._cursor().execute(LAST_PRICE, params).fetchone()
        return row[0] if row else None

    def balance(self, tid, instrument):
        """A trader's balance in an instrument or currency; 0 before any movement."""
        row = self._cursor().execute(BALANCE, dict(tid=tid, instrument=instrument))
        row = row.fetchone()
        return row[0] if row else 0

    # -- internals ---------------------------------------------------------

    def _cursor(self):
        return self.book.db.cursor()

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

        instrument = instrument or self.instrument
        quote = dict(
            type=order_type,
            side=side,
            instrument=instrument,
            qty=qty,
            price=price,
            tid=tid,
        )
        from_data = idNum is not None
        if from_data:
            quote["idNum"] = idNum
            quote["timestamp"] = timestamp if timestamp is not None else idNum

        before = len(self.trades(instrument))
        self.book.processOrder(quote, from_data, False)
        return OrderRef(self, quote["idNum"], side, self.trades(instrument)[before:])

    def _target(self, order, side):
        """Resolve (identifier, side) from an `OrderRef` or a raw identifier."""
        if isinstance(order, OrderRef):
            return order.idNum, order.side if side is UNSET else side
        if side is UNSET:
            state = self.order_state(order)
            side = state.side if state else None
        return order, side

    def _owner(self, idNum):
        row = self._cursor().execute(ORDER_OWNER, dict(idNum=idNum)).fetchone()
        return row[0] if row else None


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    # The engine drives its own `begin transaction` / `commit`, so autocommit
    # must be left to it -- same as example.py.
    conn.isolation_level = None
    return conn


def build_legacy(
    db_path,
    traders=TRADERS,
    commissions=NO_COMMISSION,
    self_matching=(),
    instrument=INSTRUMENT,
    currency=CURRENCY,
    tick_size=DEFAULT_TICK,
):
    """Build the legacy SQL engine on a fresh database at `db_path`."""
    conn = _connect(db_path)
    conn.executescript(SCHEMA.read_text())

    schedule = _as_commissions(commissions)
    allowed = _self_matching_set(self_matching, traders)

    crsr = conn.cursor()
    crsr.execute("begin transaction")
    for tid in traders:
        crsr.execute(
            SEED_TRADER,
            dict(
                tid=tid,
                name=str(tid),
                allow_self_matching=int(tid in allowed),
                commission_min=schedule.min,
                commission_max_percnt=schedule.max_pct,
                commission_per_unit=schedule.per_unit,
            ),
        )
    crsr.execute(SEED_INSTRUMENT, dict(symbol=instrument, currency=currency))
    crsr.execute("commit")

    return LegacyAdapter(
        OrderBook(db=conn, tick_size=tick_size),
        db_path=db_path,
        instrument=instrument,
        currency=currency,
        tick_size=tick_size,
    )


def build_inmemory(db_path, **options):
    """Build the in-memory engine (ADR-0001, epic lob-5rt).

    Unreachable while `PyLOB.engine` is absent: `ENGINES` skips an engine whose
    module does not import, so no acceptance test ever gets here. Writing this
    body -- an adapter with the same surface as `LegacyAdapter` -- is the whole
    of what wiring the new engine into the acceptance suites takes.
    """
    raise NotImplementedError(
        "the in-memory engine adapter lands with PyLOB.engine (epic lob-5rt)"
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
# the engine registry -- add an engine here and all four suites pick it up
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineSpec:
    """One engine under test.

    `requires` is the module that has to be importable for this engine to
    exist; while it is not, every parameterization of this engine is skipped
    rather than failed.
    """

    id: str
    requires: str
    #: (db_path, **options) -> an adapter with `LegacyAdapter`'s surface
    build: Callable


ENGINES = (
    EngineSpec(id="legacy", requires="PyLOB.orderbook", build=build_legacy),
    EngineSpec(id="inmemory", requires="PyLOB.engine", build=build_inmemory),
)


def _engine_params():
    for spec in ENGINES:
        marks = []
        if find_spec(spec.requires) is None:
            marks.append(
                pytest.mark.skip(
                    reason="engine %r is not present yet (no module %s)"
                    % (spec.id, spec.requires)
                )
            )
        yield pytest.param(spec, id=spec.id, marks=marks)


@pytest.fixture(params=list(_engine_params()))
def engine_spec(request):
    """The engine this test run is exercising."""
    return request.param


@pytest.fixture
def engine_factory(engine_spec, tmp_path):
    """Factory building independent engines of the engine under test.

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
        adapter = engine_spec.build(
            tmp_path / ("acceptance%d.db" % next(counter)), **options
        )
        built.append(adapter)
        return adapter

    yield factory

    for adapter in built:
        adapter.close()


@pytest.fixture
def engine(engine_factory):
    """An empty book on the engine under test, with commissions at zero."""
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


# --------------------------------------------------------------------------
# per-(engine, scenario) divergences
# --------------------------------------------------------------------------


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "engine_xfail(engines, reason, strict=True): this scenario is a known "
        "divergence on the named engine(s) -- an engine id or a tuple of them. "
        "Applied as a strict xfail, so fixing the engine turns the run red "
        "until the marker goes.",
    )


def pytest_collection_modifyitems(items):
    """Turn `engine_xfail` markers into xfails for the matching engine."""
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is None:
            continue
        spec = callspec.params.get("engine_spec")
        if not isinstance(spec, EngineSpec):
            continue
        for marker in item.iter_markers("engine_xfail"):
            engines, reason, strict = _divergence(marker)
            if spec.id in engines:
                item.add_marker(
                    pytest.mark.xfail(
                        reason="%s: %s" % (spec.id, reason), strict=strict
                    )
                )


def _divergence(marker):
    args = list(marker.args)
    kwargs = dict(marker.kwargs)
    engines = kwargs.pop("engines", None) if not args else args.pop(0)
    reason = kwargs.pop("reason", None) if not args else args.pop(0)
    strict = kwargs.pop("strict", True)
    if engines is None or reason is None or args or kwargs:
        raise TypeError(
            "engine_xfail takes (engines, reason, strict=True), got %r / %r"
            % (marker.args, marker.kwargs)
        )
    if isinstance(engines, str):
        engines = (engines,)
    return tuple(engines), reason, strict
