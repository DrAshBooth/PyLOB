"""The differential harness: the legacy SQL engine as an oracle for the new one.

ADR-0001 keeps `PyLOB.orderbook` in the tree as a cross-check until the
in-memory engine passes the same suites. This is that cross-check: seeded
random workloads run through both engines in lockstep, and after *every*
operation the two are compared on everything either can be asked -- the full
book on both sides in priority order, best/worst prices, volume-at-price,
last price, the whole trade log, every order's accounting, and every trader's
balances in both the instrument and the currency.

Both engines are driven through the acceptance suites' adapter
(`tests/acceptance/conftest.py`), which is what makes a differential harness
expressible at all: `limit`, `market`, `cancel`, `modify`, `order_state`,
`snapshot`, `trades`, `best`, `worst`, `volume_at`, `last_price`, `balance`
mean the same thing on both sides of the comparison.


The whitelist, and why it has exactly two entries
-------------------------------------------------

`openspec/changes/inmemory-engine/design.md` decision 6 names two expected
divergences: **IOC remainder handling** and **priority-on-price-change**.
`WHITELIST` below is that list, and it is not a set of comparisons the
comparator skips -- it is a set of divergences this file *pins with explicit
scenarios* (`test_whitelist_ioc_remainder`,
`test_whitelist_priority_on_price_change`). Each asserts what both engines do,
so the day the legacy engine is fixed or the new one regresses, the pin fails
loudly. A skipped comparison would go quiet instead.

The randomized workload is then constrained so that no whitelisted divergence
is reachable from it, and every comparison in it is exact (money excepted --
see below). `CONSTRAINTS` records each constraint, the bead it corresponds to,
and why the constraint was preferred to a whitelist entry. The rule applied
throughout, and the maintainer's recommendation on lob-we3: **a generator
constraint costs one line and is easy to state; a whitelist entry blinds the
harness to a whole class of difference forever.**

Both whitelisted divergences are *state-poisoning* rather than local: once a
legacy market remainder is resting, or once a repriced order sits ahead of the
queue it should be behind, every subsequent fill, balance and trade differs.
There is no narrow way to blind the comparator to them, which is a second
reason the constraint-plus-pin shape is the only honest one.


Why money is compared within a tolerance
----------------------------------------

The two engines apply the same commission formula to the same cumulative
(Q, V), but move the currency leg in a different number of floating-point
steps: legacy's `trade_insert` trigger debits the value and the commission as
separate `UPDATE`s, while `OrderBook._settle` debits `value + delta` in one.
`a - v - c` and `a - (v + c)` are not the same double. The commissions
contract already says money is compared within a tolerance and never for bit
equality; `MONEY_REL` / `MONEY_ABS` (1e-9) are the acceptance suites' own.

Everything else -- prices, quantities, identifiers, queue positions -- is
compared exactly, because on the constrained workload there is no reason for
it to differ by so much as a bit.
"""

import importlib.util
import random
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# the acceptance surface, borrowed
# --------------------------------------------------------------------------

_ACCEPTANCE = Path(__file__).resolve().parent / "acceptance" / "conftest.py"


def _acceptance_surface():
    """`tests/acceptance/conftest.py` as a module, under a name of its own.

    Loaded by path rather than imported: it is a conftest for the directory
    below this one, so pytest never makes it importable from here, and giving
    the copy a distinct module name keeps it out of `sys.modules['conftest']`
    where the two conftests would collide.
    """
    spec = importlib.util.spec_from_file_location(
        "pylob_acceptance_surface", _ACCEPTANCE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_surface = _acceptance_surface()

Commissions = _surface.Commissions
NO_COMMISSION = _surface.NO_COMMISSION
MONEY_REL = _surface.MONEY_REL
MONEY_ABS = _surface.MONEY_ABS

INSTRUMENT = _surface.INSTRUMENT
CURRENCY = _surface.CURRENCY

#: A decimal tick, and every generated price an exact multiple of it. See
#: CONSTRAINTS["on-grid prices, decimal tick"].
TICK = 0.01

TRADERS = (1, 2, 3, 4, 5)


# --------------------------------------------------------------------------
# the two named divergences (design.md decision 6)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Whitelisted:
    """One expected legacy/new divergence, pinned by a scenario in this file."""

    name: str
    bead: str
    #: Why a generator constraint could not stand in for the entry.
    unavoidable: str


WHITELIST = (
    Whitelisted(
        name="IOC remainder",
        bead="lob-0bl",
        unavoidable=(
            "This is the behaviour change itself, not an input the generator "
            "can steer around: the order-lifecycle contract says a market "
            "order never rests, and the legacy engine rests the remainder. "
            "The randomized workload avoids *producing* a remainder (see "
            "CONSTRAINTS['market orders never leave a remainder']) so that the "
            "comparison stays exact, but the divergence is real and is pinned "
            "here rather than merely dodged."
        ),
    ),
    Whitelisted(
        name="priority on price change",
        bead="lob-pn3",
        unavoidable=(
            "Also the behaviour change itself: order-lifecycle requires a "
            "price change to surrender time priority, and legacy re-stamps "
            "arrival only on a quantity increase. The randomized workload "
            "reprices only into empty levels (see CONSTRAINTS['reprice only "
            "into an empty level']), where the two priority rules provably "
            "agree; the disagreement is pinned here."
        ),
    ),
)


#: Every constraint the workload generator carries, and why it is a constraint
#: rather than a third whitelist entry. Read alongside `WHITELIST`.
CONSTRAINTS = {
    "market orders never leave a remainder": (
        "lob-0bl",
        "A market order's quantity is drawn from the liquidity it may actually "
        "consume right now (opposite-side resting volume, minus the taker's "
        "own orders when the self-matching gate applies), so it always fills "
        "completely. Legacy rests any remainder at a null price that its book "
        "view sorts *first* and then trades at the next taker's price, which "
        "corrupts every later comparison rather than showing up as one "
        "difference. Whitelisting it would mean blinding the harness to the "
        "book, the trades and the balances from that point on.",
    ),
    "reprice only into an empty level": (
        "lob-pn3",
        "Legacy's queue key is the last arrival-or-quantity-increase; the new "
        "engine's is the last arrival-or-quantity-increase-or-reprice. They "
        "order a level identically unless some order already at the "
        "destination arrived between the repriced order's own arrival and its "
        "reprice. Repricing only into a level that is currently empty makes "
        "that impossible: nothing can join the level afterwards except by a "
        "fresh arrival (a later reprice would find the level occupied), and a "
        "fresh arrival is behind the repriced order under both keys. So the "
        "constraint keeps reprices in the workload -- about 8% of operations "
        "-- while keeping the queue comparison exact.",
    ),
    "no invalid submissions": (
        "lob-ihv",
        "Legacy calls `sys.exit` on a qty <= 0 or a bad side/type, which "
        "raises `SystemExit` and would tear down the whole pytest run rather "
        "than fail one test. The new engine raises `InvalidOrder`. The "
        "acceptance suites already pin that contract; generating invalid "
        "input here would buy nothing and cost the run.",
    ),
    "cancel and modify address a currently resting order": (
        "lob-0rb",
        "Legacy silently no-ops on an unknown identifier, and its "
        "`cancel_order.sql` will happily set cancel=1 on an order that is "
        "already filled or already cancelled; the new engine raises. Worse, "
        "legacy's `modifyOrder` on a *cancelled* order still reaches "
        "`processMatchesDB`, so a cancelled order can trade. All of it is "
        "error-path behaviour the acceptance suites pin directly, and none of "
        "it is what a differential harness is for.",
    ),
    "identifiers are engine-assigned, never supplied": (
        "lob-a17",
        "Legacy accepts a duplicate external idNum, after which every "
        "idNum-keyed operation addresses an arbitrary one of the two orders; "
        "the new engine raises `DuplicateOrderID`. Letting each engine assign "
        "its own identifiers side-steps it -- and the two assignments are "
        "themselves compared on every submission, so the harness still proves "
        "the counters agree rather than assuming it.",
    ),
    "no reload mid-workload": (
        "lob-7e7, lob-bis",
        "Legacy does not re-seed its idNum counter or rebuild `lastPrice` "
        "from the database on reload. Reload is not this harness's subject -- "
        "lob-5rt.7's replay tests own it -- so the workload simply never "
        "reopens a book.",
    ),
    "modify always names an explicit price": (
        "lob-crf",
        "Legacy reads `price=None` on a modify as 'match as a market order' "
        "and trades a limit order straight through its own limit; the new "
        "engine reads it as 'leave the price alone'. The adapter's default "
        "fills the order's current price in, so the generator never has to "
        "send a bare None.",
    ),
    "on-grid prices, decimal tick": (
        "lob-we3",
        "Legacy quantizes with `round(price, log10(1/tick))` on a binary "
        "double; the new engine divides by the tick in decimal and rounds "
        "half-even. The two disagree on an exact half-tick (100.00005 on a "
        "0.0001 tick: legacy 100.0001, new 100.0) and on any non-decimal tick "
        "(100.03 on a 0.05 tick: legacy 100.0, new 100.05 -- legacy simply "
        "wrong). Generating prices that are already exact multiples of a "
        "decimal tick means both quantizers are the identity and neither "
        "class arises. This is lob-we3's option (a), and the reason is the "
        "general rule: the alternative third whitelist entry would blind the "
        "harness to every price difference, including the ones where legacy "
        "is outright wrong.",
    ),
    "one instrument": (
        "lob-z45",
        "Legacy's `active_orders.sql` has no instrument filter, so a book "
        "snapshot leaks orders from other instruments. Single-instrument is "
        "a standing constraint of the current specs anyway.",
    ),
}


# --------------------------------------------------------------------------
# the workload
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Op:
    """One operation, in the adapter's vocabulary.

    Rendered rather than replayed: a failing run prints these, and each line
    is the call that produced it.
    """

    kind: str
    side: str | None = None
    qty: int | None = None
    price: float | None = None
    tid: int | None = None
    idNum: int | None = None

    def render(self):
        if self.kind == "limit":
            return "limit(%r, qty=%d, price=%r, tid=%d)" % (
                self.side,
                self.qty,
                self.price,
                self.tid,
            )
        if self.kind == "market":
            return "market(%r, qty=%d, tid=%d)" % (self.side, self.qty, self.tid)
        if self.kind == "cancel":
            return "cancel(%d)" % (self.idNum,)
        parts = ["%d" % self.idNum]
        if self.qty is not None:
            parts.append("qty=%d" % self.qty)
        if self.price is not None:
            parts.append("price=%r" % self.price)
        return "modify(%s)" % ", ".join(parts)


@dataclass(frozen=True)
class Profile:
    """One shape of workload: the market's parameters and the operation mix."""

    name: str
    #: Prices as whole ticks, so every generated price is exactly on the grid.
    grid: tuple[int, ...]
    commissions: object = NO_COMMISSION
    self_matching: object = ()
    ops: int = 150
    #: Cumulative weights for limit / market / cancel / modify.
    p_limit: float = 0.60
    p_market: float = 0.72
    p_cancel: float = 0.85


def _grid(low, high, step):
    return tuple(range(low, high + 1, step))


PROFILES = (
    # A wide book: many price levels, mostly one order deep, so the price
    # index and the level bookkeeping do the work.
    Profile(name="wide", grid=_grid(9500, 10500, 5)),
    # Commissions on and two traders allowed to cross themselves: exercises
    # the ledgers and the self-matching gate together.
    Profile(
        name="ledgers",
        grid=_grid(9800, 10200, 5),
        commissions=Commissions(min=2.5, max_pct=1.0, per_unit=0.01),
        self_matching=(1, 3),
    ),
    # A narrow book: every level many orders deep, which is where FIFO order,
    # partial fills and the modify rules actually collide.
    Profile(
        name="deep",
        grid=_grid(9980, 10020, 5),
        commissions=Commissions(min=0.5, max_pct=0.5, per_unit=0.02),
        self_matching=True,
        p_limit=0.50,
        p_market=0.62,
        p_cancel=0.78,
    ),
)

SEEDS = (1, 2, 3, 4)


def _price(ticks):
    """A grid point as a price. Exact: `ticks` hundredths on a 0.01 tick."""
    return ticks / 100.0


class Generator:
    """The seeded workload generator, and every constraint in `CONSTRAINTS`.

    State-adaptive by necessity rather than by taste: two of the constraints
    ("no remainder", "empty destination level") are questions about the book
    as it stands, so the generator reads the book before choosing. It reads it
    from the *legacy* engine, the oracle -- and only ever at a point where the
    two engines have just been compared equal, so which one it reads is
    immaterial.

    Deterministic given (profile, seed): the random stream is, and both
    engines are, so the operation sequence is too.
    """

    def __init__(self, profile, seed):
        self.profile = profile
        self.rng = random.Random(seed)
        #: idNum -> tid, so the generator can tell which resting liquidity a
        #: given taker is actually allowed to consume.
        self.owner = {}

    def next_op(self, oracle):
        rng = self.rng
        profile = self.profile
        resting = oracle.snapshot("bid") + oracle.snapshot("ask")
        roll = rng.random()

        if roll < profile.p_limit or not resting:
            return self._limit()
        if roll < profile.p_market:
            return self._market(oracle)
        if roll < profile.p_cancel:
            # CONSTRAINTS["cancel and modify address a currently resting
            # order"]: a snapshot lists exactly the resting orders.
            return Op(kind="cancel", idNum=rng.choice(resting).idNum)
        return self._modify(oracle, rng.choice(resting).idNum)

    def _limit(self):
        rng = self.rng
        return Op(
            kind="limit",
            side=rng.choice(("bid", "ask")),
            qty=rng.randint(1, 8),
            price=_price(rng.choice(self.profile.grid)),
            tid=rng.choice(TRADERS),
        )

    def _market(self, oracle):
        """A market order sized to the liquidity it may actually consume.

        CONSTRAINTS["market orders never leave a remainder"]. "May actually
        consume" is not the same as "is resting": a trader without
        `allow_self_matching` steps over their own orders, so those do not
        count towards what this order can fill against.
        """
        rng = self.rng
        side = rng.choice(("bid", "ask"))
        tid = rng.choice(TRADERS)
        makers = oracle.snapshot("ask" if side == "bid" else "bid")
        reachable = sum(
            entry.available
            for entry in makers
            if self._may_match(tid, self.owner.get(entry.idNum))
        )
        if reachable <= 0:
            return self._limit()
        return Op(kind="market", side=side, qty=rng.randint(1, reachable), tid=tid)

    def _may_match(self, taker, maker):
        if taker != maker:
            return True
        allowed = self.profile.self_matching
        return allowed is True or taker in allowed

    def _modify(self, oracle, idNum):
        """A quantity change, a price change, or both.

        A price change targets a level that is *currently empty* on the
        order's side -- CONSTRAINTS["reprice only into an empty level"]. The
        order's own level is occupied by the order itself, so this also
        guarantees the price really changes. When the book leaves no free
        level (the `deep` profile's grid is only nine wide) the operation
        falls back to a pure quantity change rather than being dropped, which
        keeps the operation mix stable across profiles.
        """
        rng = self.rng
        state = oracle.order_state(idNum)
        occupied = {entry.price for entry in oracle.snapshot(state.side)}
        free = [t for t in self.profile.grid if _price(t) not in occupied]

        wants_price = rng.random() < 0.5 and free
        wants_qty = rng.random() < 0.5 or not wants_price
        return Op(
            kind="modify",
            idNum=idNum,
            qty=rng.randint(1, 12) if wants_qty else None,
            price=_price(rng.choice(free)) if wants_price else None,
        )

    def observe(self, op, idNum):
        if op.kind in ("limit", "market"):
            self.owner[idNum] = op.tid


def apply_op(engine, op):
    """Run one operation; return `(idNum, trades)` as that engine saw them."""
    if op.kind == "limit":
        ref = engine.limit(op.side, op.qty, op.price, op.tid)
        return ref.idNum, tuple(ref.trades)
    if op.kind == "market":
        ref = engine.market(op.side, op.qty, op.tid)
        return ref.idNum, tuple(ref.trades)
    if op.kind == "cancel":
        engine.cancel(op.idNum)
        return op.idNum, ()
    kwargs = {}
    if op.qty is not None:
        kwargs["qty"] = op.qty
    if op.price is not None:
        kwargs["price"] = op.price
    return op.idNum, tuple(engine.modify(op.idNum, **kwargs))


# --------------------------------------------------------------------------
# the comparator
# --------------------------------------------------------------------------


@dataclass
class Difference:
    """One thing the two engines disagree about, ready to be printed."""

    what: str
    legacy: object
    inmemory: object
    detail: str = ""

    def render(self):
        lines = [
            "  %s" % self.what,
            "      legacy   : %s" % _show(self.legacy),
            "      inmemory : %s" % _show(self.inmemory),
        ]
        if self.detail:
            lines.append("      %s" % self.detail)
        return "\n".join(lines)


def _show(value):
    if isinstance(value, tuple) and value and hasattr(value[0], "available"):
        return _show_book(value)
    if isinstance(value, tuple) and value and hasattr(value[0], "bid"):
        return _show_trades(value)
    return repr(value)


def _show_book(entries):
    if not entries:
        return "<empty>"
    return ", ".join(
        "#%d %s x%d/%d" % (e.idNum, e.price, e.available, e.qty) for e in entries
    )


def _show_trades(trades):
    if not trades:
        return "<none>"
    return ", ".join(
        "bid#%d/ask#%d %sx%d" % (t.bid, t.ask, t.price, t.qty) for t in trades
    )


def _one(value):
    """One book entry / trade / order state, compactly."""
    if hasattr(value, "available"):
        return _show_book((value,))
    if hasattr(value, "bid"):
        return _show_trades((value,))
    return repr(value)


def _first_mismatch(left, right):
    """Where two sequences first part company, phrased for a human."""
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return "first differs at position %d: legacy %s, inmemory %s" % (
                index,
                _one(a),
                _one(b),
            )
    return "same first %d, then legacy has %d and inmemory %d" % (
        min(len(left), len(right)),
        len(left),
        len(right),
    )


def _money_differs(a, b):
    return a != pytest.approx(b, rel=MONEY_REL, abs=MONEY_ABS)


def differences(legacy, inmemory, known_ids=(), probes=()):
    """Everything the two engines disagree about right now, as a list.

    Empty means they agree on the whole of the observable surface: both books
    in priority order, the four price accessors, volume-at-price at `probes`,
    the last price, the entire trade log, every order in `known_ids`, and
    every trader's balance in both the instrument and the currency.

    Money is compared within `MONEY_REL` / `MONEY_ABS`; everything else
    exactly.
    """
    found = []

    for side in ("bid", "ask"):
        left, right = legacy.snapshot(side), inmemory.snapshot(side)
        if left != right:
            found.append(
                Difference(
                    "book[%s] (price, then priority order)" % side,
                    left,
                    right,
                    _first_mismatch(left, right),
                )
            )
        for query in ("best", "worst"):
            a, b = getattr(legacy, query)(side), getattr(inmemory, query)(side)
            if a != b:
                found.append(Difference("%s %s price" % (query, side), a, b))
        for price in probes:
            a = legacy.volume_at(side, price)
            b = inmemory.volume_at(side, price)
            if a != b:
                found.append(Difference("volume_at[%s, %s]" % (side, price), a, b))

    left, right = legacy.trades(), inmemory.trades()
    if left != right:
        found.append(
            Difference(
                "trade log (%d vs %d executions)" % (len(left), len(right)),
                left[-4:],
                right[-4:],
                _first_mismatch(left, right),
            )
        )

    a, b = legacy.last_price(), inmemory.last_price()
    if a != b:
        found.append(Difference("last price", a, b))

    for idNum in known_ids:
        left, right = legacy.order_state(idNum), inmemory.order_state(idNum)
        if left is None or right is None:
            if left is not right:
                found.append(Difference("order %d exists" % idNum, left, right))
            continue
        if left[:-1] != right[:-1]:
            fields = [
                "%s: legacy %r vs inmemory %r" % (name, a, b)
                for name, a, b in zip(left._fields, left, right)
                if a != b
            ]
            found.append(
                Difference("order %d state" % idNum, left, right, "; ".join(fields))
            )
        elif _money_differs(left.commission, right.commission):
            found.append(
                Difference(
                    "order %d commission" % idNum, left.commission, right.commission
                )
            )

    for tid in TRADERS:
        for symbol in (INSTRUMENT, CURRENCY):
            a = legacy.balance(tid, symbol)
            b = inmemory.balance(tid, symbol)
            if _money_differs(a, b):
                found.append(Difference("balance[%d, %s]" % (tid, symbol), a, b))

    return found


# --------------------------------------------------------------------------
# running a workload
# --------------------------------------------------------------------------


@dataclass
class Run:
    """One workload in flight: the two engines and the trail behind them."""

    profile: Profile
    seed: int
    legacy: object
    inmemory: object
    generator: Generator
    history: list = field(default_factory=list)
    known_ids: list = field(default_factory=list)

    @property
    def probes(self):
        grid = self.profile.grid
        return tuple(
            _price(grid[index]) for index in (0, len(grid) // 4, len(grid) // 2, -1)
        )

    def report(self, index, op, found):
        """The failure message. This is the whole point of the harness."""
        recent = self.history[max(0, index - 5) : index]
        lines = [
            "differential divergence: legacy and inmemory disagree",
            "",
            "  profile  : %s" % self.profile.name,
            "  seed     : %d" % self.seed,
            "  operation: %d of %d" % (index, self.profile.ops),
            "  op       : %s" % op.render(),
            "",
            "  %d difference%s:" % (len(found), "" if len(found) == 1 else "s"),
            "",
        ]
        lines.extend(item.render() for item in found)
        lines.append("")
        lines.append("  the %d operations before it:" % len(recent))
        lines.extend(
            "    %4d  %-46s -> id %d" % (position, done.render(), idNum)
            for position, done, idNum in recent
        )
        lines.append("")
        lines.append("  books at the divergence:")
        for side in ("bid", "ask"):
            lines.append(
                "    legacy   %s: %s" % (side, _show_book(self.legacy.snapshot(side)))
            )
            lines.append(
                "    inmemory %s: %s" % (side, _show_book(self.inmemory.snapshot(side)))
            )
        lines.append("")
        lines.append(
            "  reproduce: uv run pytest tests/test_differential.py -k "
            "'%s and seed%d'" % (self.profile.name, self.seed)
        )
        return "\n".join(lines)

    def step(self, index):
        op = self.generator.next_op(self.legacy)
        legacy_id, legacy_trades = apply_op(self.legacy, op)
        inmemory_id, inmemory_trades = apply_op(self.inmemory, op)

        found = []
        if legacy_id != inmemory_id:
            found.append(Difference("identifier assigned", legacy_id, inmemory_id))
        if legacy_trades != inmemory_trades:
            found.append(
                Difference(
                    "executions returned by this operation",
                    legacy_trades,
                    inmemory_trades,
                    _first_mismatch(legacy_trades, inmemory_trades),
                )
            )
        found.extend(
            differences(
                self.legacy,
                self.inmemory,
                known_ids=self.known_ids,
                probes=self.probes,
            )
        )

        self.generator.observe(op, legacy_id)
        if op.kind in ("limit", "market"):
            self.known_ids.append(legacy_id)
        self.history.append((index, op, legacy_id))

        if found:
            raise AssertionError(self.report(index, op, found))


def build_pair(tmp_path, profile, name="run"):
    """The two engines, configured identically, on their own scratch files."""
    options = dict(
        traders=TRADERS,
        commissions=profile.commissions,
        self_matching=profile.self_matching,
        instrument=INSTRUMENT,
        currency=CURRENCY,
        tick_size=TICK,
    )
    try:
        legacy = _surface.build_legacy(tmp_path / ("%s-legacy.db" % name), **options)
        inmemory = _surface.build_inmemory(
            tmp_path / ("%s-inmemory.db" % name), **options
        )
    except (ImportError, NotImplementedError) as exc:
        pytest.skip("the in-memory engine is not ready: %s" % exc)
    return legacy, inmemory


# --------------------------------------------------------------------------
# the randomized cross-check
# --------------------------------------------------------------------------


@pytest.fixture(params=PROFILES, ids=lambda profile: profile.name)
def profile(request):
    return request.param


@pytest.mark.parametrize("seed", SEEDS, ids=lambda seed: "seed%d" % seed)
def test_engines_agree_on_a_seeded_workload(tmp_path, profile, seed):
    """Every observable, after every operation, on both engines.

    A divergence here is a *finding*, not a test to relax: either the new
    engine has a bug, or the legacy oracle has one that no bead names yet.
    The whitelist is closed (`test_whitelist_is_the_two_named_divergences`)
    and adding to it is a design decision, not a way to make a red run green.
    """
    legacy, inmemory = build_pair(tmp_path, profile, name="seed%d" % seed)
    run = Run(
        profile=profile,
        seed=seed,
        legacy=legacy,
        inmemory=inmemory,
        generator=Generator(profile, seed),
    )
    try:
        for index in range(profile.ops):
            run.step(index)
        # A workload that never traded would agree trivially.
        executions = len(legacy.trades())
    finally:
        legacy.close()
        inmemory.close()

    assert len(run.known_ids) >= profile.ops // 2
    assert executions > 0, "the workload never traded -- it proves nothing"


def test_the_comparator_would_notice(tmp_path):
    """The harness has teeth: feed the two engines different input, see it fail.

    Without this, a comparator that quietly compared nothing would pass every
    test above.
    """
    legacy, inmemory = build_pair(tmp_path, PROFILES[0], name="teeth")
    legacy.limit("ask", 5, 101.0, 1)
    inmemory.limit("ask", 4, 101.0, 1)
    found = differences(legacy, inmemory, known_ids=[1], probes=(101.0,))
    legacy.close()
    inmemory.close()

    reported = {item.what for item in found}
    assert any("book[ask]" in what for what in reported), reported
    assert any("volume_at" in what for what in reported), reported
    assert any("order 1 state" in what for what in reported), reported


# --------------------------------------------------------------------------
# the whitelist, pinned
# --------------------------------------------------------------------------


def test_whitelist_is_the_two_named_divergences():
    """`design.md` decision 6 names two. Growing this list is a design change.

    Anything else the harness finds is a bug in one engine or the other, and
    the honest response is a bead, not an entry here.
    """
    assert [entry.name for entry in WHITELIST] == [
        "IOC remainder",
        "priority on price change",
    ]
    assert [entry.bead for entry in WHITELIST] == ["lob-0bl", "lob-pn3"]


def test_whitelist_ioc_remainder(tmp_path):
    """Whitelist entry 1 (lob-0bl): legacy rests a market remainder, we cancel.

    lob-0bl's own repro, run through both engines. The legacy remainder is not
    merely parked: it sorts ahead of every priced order and then buys at the
    *next taker's* price, which is why this divergence cannot be contained to
    one comparison and why the randomized workload never produces a remainder.
    """
    legacy, inmemory = build_pair(tmp_path, PROFILES[0], name="ioc")
    for engine in (legacy, inmemory):
        engine.limit("ask", 3, 101.0, 1)
        taker = engine.market("bid", 8, 2)
        assert taker.fulfilled == 3

    assert legacy.order_state(2).resting is True
    assert legacy.order_state(2).cancelled is False
    assert legacy.snapshot("bid") == (
        _surface.BookEntry(idNum=2, price=None, available=5, qty=8, fulfilled=3),
    )

    assert inmemory.order_state(2).resting is False
    assert inmemory.order_state(2).cancelled is True
    assert inmemory.snapshot("bid") == ()

    # The consequence, and the reason this one poisons a whole run.
    for engine in (legacy, inmemory):
        engine.limit("ask", 5, 200.0, 3)
    assert legacy.trades()[-1] == _surface.Trade(bid=2, ask=3, price=200.0, qty=5)
    assert len(inmemory.trades()) == 1

    found = differences(legacy, inmemory, known_ids=[1, 2, 3], probes=(101.0,))
    assert found, "lob-0bl no longer diverges -- retire this whitelist entry"
    legacy.close()
    inmemory.close()


def test_whitelist_priority_on_price_change(tmp_path):
    """Whitelist entry 2 (lob-pn3): a reprice costs priority here, not there.

    The order-lifecycle scenario: A and B rest at 99, A is repriced away and
    back, and B should now be ahead. Legacy re-stamps arrival only on a
    quantity increase, so A keeps its place -- a priority-jumping exploit.
    """
    legacy, inmemory = build_pair(tmp_path, PROFILES[0], name="reprice")
    for engine in (legacy, inmemory):
        first = engine.limit("bid", 5, 99.0, 1)
        engine.limit("bid", 5, 99.0, 2)
        engine.modify(first, price=98.0)
        engine.modify(first, price=99.0)

    assert [entry.idNum for entry in legacy.snapshot("bid")] == [1, 2]
    assert [entry.idNum for entry in inmemory.snapshot("bid")] == [2, 1]

    found = differences(legacy, inmemory, known_ids=[1, 2], probes=(99.0,))
    assert found, "lob-pn3 no longer diverges -- retire this whitelist entry"
    assert any("book[bid]" in item.what for item in found)
    legacy.close()
    inmemory.close()


# --------------------------------------------------------------------------
# the constraints, pinned
# --------------------------------------------------------------------------


def test_every_constraint_names_a_bead():
    """A constraint with no bead behind it is a hunch, and hunches rot."""
    for name, (bead, reason) in CONSTRAINTS.items():
        assert bead, name
        assert len(reason) > 80, name


def test_generated_prices_are_exactly_on_the_grid(tmp_path):
    """CONSTRAINTS["on-grid prices, decimal tick"], demonstrated.

    Both engines quantize a generated price to the identical double, so the
    quantizers cannot contribute a difference. The half-tick case lob-we3
    describes is right next door, and is left out of the workload rather than
    whitelisted.
    """
    legacy, inmemory = build_pair(tmp_path, PROFILES[0], name="grid")
    for profile in PROFILES:
        for ticks in profile.grid:
            price = _price(ticks)
            assert legacy.book.clipPrice(price) == price
            assert inmemory.book.clipPrice(price) == price
    legacy.close()
    inmemory.close()
