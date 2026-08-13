"""The differential harness: a spec-derived reference matcher as the oracle.

Seeded random workloads run through two implementations in lockstep, and after
*every* operation the two are compared on everything either can be asked --
both books in priority order, best/worst prices, volume-at-price, last price,
the whole trade log, every order's accounting, and every trader's balance in
every instrument and currency.

The two implementations are `PyLOB.engine.OrderBook` and `tests/reference`, a
matching engine written from the frozen specs that shares no code with it. Both
are driven through the acceptance suites' adapter surface
(`tests/acceptance/conftest.py`), which is what makes a differential harness
expressible at all: `limit`, `market`, `cancel`, `modify`, `reopen`,
`order_state`, `snapshot`, `trades`, `best`, `worst`, `volume_at`,
`last_price`, `balance` mean the same thing on both sides of the comparison.


What replaced what, and why the whitelist is gone
-------------------------------------------------

Until ADR-0003 the oracle was the legacy SQL engine. It was an oracle by
accident of history: it disagreed with the frozen specs in nine known ways, so
this file carried a two-entry whitelist of expected divergences and nine
generator constraints, each named after a legacy defect, each one excluding
inputs the harness would otherwise have wanted most.

A spec-derived matcher has none of those divergences, so all of it is gone --
the whitelist entirely, and seven of the nine constraints. The workload now
generates exactly what the old one had to exclude:

============================  ==========================================
excluded before               generated now
============================  ==========================================
`lob-0bl` market remainders   market orders sized freely; the remainder is
                              cancelled on both sides (`order-lifecycle`:
                              market orders are immediate-or-cancel)
`lob-pn3` reprices into an    reprices into any level, occupied or not --
occupied level                which is the case that tells the two
                              priority rules apart
`lob-a17` supplied            the data-replay path, identifiers and
identifiers                   timestamps supplied by the caller, several
                              orders sharing one timestamp
`lob-7e7`/`lob-bis` reloads   the engine reloads mid-workload and must come
                              back agreeing with a model that never went
                              away
`lob-crf` `modify(price=      generated, alongside the "don't mention it"
None)`                        form, which must produce the same outcome
`lob-we3` off-grid prices     half-tick prices on three different ticks, so
                              the two quantizers are compared rather than
                              both being the identity
`lob-z45` one instrument      several instruments, one profile with two
                              currencies
============================  ==========================================

`CONSTRAINTS` is what is left: two entries, both about generating *legal*
input, neither naming a defect in anything.


Why money is compared within a tolerance
----------------------------------------

The two implementations apply the same commission formula to the same
cumulative (Q, V) but move the currency leg in a different number of
floating-point steps: `trader-balances` and `commissions` describe the cash
movement and the commission debit separately and the model does them
separately, while `OrderBook._settle` debits `value + delta` in one. `a - v -
c` and `a - (v + c)` are not the same double. The commissions contract already
says money is compared within a tolerance and never for bit equality;
`MONEY_REL` / `MONEY_ABS` (1e-9) are the acceptance suites' own.

Everything else -- prices, quantities, identifiers, queue positions -- is
compared exactly, because there is no reason for it to differ by so much as a
bit.
"""

import ast
import random
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from acceptance_surface import (
    CURRENCY,
    INSTRUMENT,
    MONEY_ABS,
    MONEY_REL,
    UNSET,
    BookEntry,
    Commissions,
    NO_COMMISSION,
    build_inmemory,
)
from reference import build_reference
from reference import matcher as reference_matcher

TRADERS = (1, 2, 3, 4, 5)

#: Every supplied-identifier order carries this timestamp, so several of them
#: share one. `order-lifecycle`'s "Same-timestamp arrivals keep arrival order"
#: is then a property of the workload rather than a scenario off to one side.
REPLAY_TIMESTAMP = 7.0

#: Supplied identifiers start well above anything the engines assign, so that
#: "supplied" and "assigned" cannot collide by accident. Both sides push their
#: counter past a supplied identifier, and the harness compares the next
#: assignment, so the rule is checked rather than assumed.
FIRST_SUPPLIED_ID = 1_000_000

#: ...and each supplied identifier clears the last by more than a workload's
#: worth of operations. `order-lifecycle` makes a supplied identifier push the
#: counter past itself, so the identifiers assigned *after* one climb out of
#: the range the next supplied one would otherwise sit in. A stride wider than
#: `Profile.ops` is what keeps "supplied" and "assigned" from meeting.
SUPPLIED_STRIDE = 1000


#: Every constraint the workload generator carries, and why. Two entries, both
#: about generating input that is *legal*; nothing here is a divergence being
#: dodged, which is the difference between this list and the nine it replaces.
CONSTRAINTS = {
    "every generated operation is legal input": (
        "The comparator compares state, and a refusal changes no state: two "
        "implementations that both raise have agreed about nothing anyone can "
        "see. Illegal input is therefore worth generating only as a pair of "
        "raises to assert on, which is a deterministic test and not a random "
        "walk -- `test_both_refuse_what_the_specs_refuse` is that test, and "
        "`tests/acceptance/` plus `tests/test_engine_boundaries.py` own the "
        "error paths in depth. Note what this constraint no longer says: it "
        "does not exclude a *hostile* input, only an invalid one. Half-tick "
        "prices, market orders that outrun the book, reprices onto a busy "
        "level and identifiers chosen by the caller are all legal, and all "
        "generated."
    ),
    "cancel and modify address a live resting order": (
        "The same rule as above, for the two operations that name an order "
        "instead of describing one: `order-lifecycle` makes an unknown "
        "identifier raise, and both implementations refuse to cancel an order "
        "that is already cancelled or already fully filled. Choosing the "
        "target from a book snapshot is how the generator names an order that "
        "exists without having to track what became of every order it ever "
        "submitted. It costs nothing: every resting order is reachable, and "
        "the interesting modifies -- the repricing ones -- are only defined "
        "for an order that is still in the book."
    ),
}


# --------------------------------------------------------------------------
# the workload
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Op:
    """One operation, in the adapter's vocabulary.

    Rendered rather than replayed: a failing run prints these, and each line is
    the call that produced it.

    `price` and `qty` are three-valued on a modify. `UNSET` means the call does
    not mention them and the adapter fills in the order's current value;
    `None` means an explicit `None` was passed, which `order-lifecycle` reads
    as "leave that one alone". The two are different calls that must have the
    same effect, so the generator sends both.
    """

    kind: str
    instrument: str | None = None
    side: str | None = None
    qty: object = UNSET
    price: object = UNSET
    tid: int | None = None
    idNum: int | None = None
    timestamp: float | None = None
    #: What this operation is an example of, for the coverage assertions.
    tags: tuple = ()

    def render(self):
        if self.kind == "reload":
            return "reopen()"
        if self.kind == "cancel":
            return "cancel(%d)" % (self.idNum,)
        if self.kind in ("limit", "market"):
            parts = ["%r" % self.side, "qty=%d" % self.qty]
            if self.kind == "limit":
                parts.append("price=%r" % self.price)
            parts.append("tid=%d" % self.tid)
            parts.append("instrument=%r" % self.instrument)
            if self.idNum is not None:
                parts.append("idNum=%d" % self.idNum)
                parts.append("timestamp=%r" % self.timestamp)
            return "%s(%s)" % (self.kind, ", ".join(parts))
        parts = ["%d" % self.idNum]
        if self.qty is not UNSET:
            parts.append("qty=%r" % self.qty)
        if self.price is not UNSET:
            parts.append("price=%r" % self.price)
        return "modify(%s)" % ", ".join(parts)


@dataclass(frozen=True)
class Profile:
    """One shape of workload: the market's parameters and the operation mix."""

    name: str
    #: The tick, and the grid as whole *half*-ticks -- so half the grid is on
    #: the tick and half of it is exactly between two ticks. See
    #: `test_the_grid_is_half_on_the_grid_and_half_off_it`.
    tick_size: float
    grid: tuple
    #: (symbol, currency) pairs. More than one of either is now allowed.
    instruments: tuple = ((INSTRUMENT, CURRENCY),)
    commissions: object = NO_COMMISSION
    self_matching: object = ()
    ops: int = 180
    #: Cumulative weights: limit / market / cancel / modify, remainder reload.
    p_limit: float = 0.52
    p_market: float = 0.68
    p_cancel: float = 0.82
    p_modify: float = 1.00
    #: How often a submission supplies its own identifier and timestamp.
    p_supplied: float = 0.0
    #: At most this many reloads, however the dice fall.
    max_reloads: int = 0
    #: Behaviour this profile must actually reach, or the run proves less than
    #: it claims. Checked at the end of every workload.
    expects: frozenset = frozenset()

    @property
    def symbols(self):
        """Every symbol a balance can be held in: instruments and currencies."""
        seen = []
        for symbol, currency in self.instruments:
            for name in (symbol, currency):
                if name not in seen:
                    seen.append(name)
        return tuple(seen)

    def price(self, half_ticks):
        """A grid point as a price: `half_ticks` halves of this profile's tick."""
        return round(half_ticks * self.tick_size / 2, 12)


def _halves(low, high, step):
    return tuple(range(low, high + 1, step))


PROFILES = (
    # A wide book on a 0.01 tick: many price levels, mostly one order deep, so
    # the price index and the level bookkeeping do the work.
    Profile(
        name="wide",
        tick_size=0.01,
        grid=_halves(19940, 20060, 3),
        expects=frozenset({"market remainder", "off-grid price", "price=None modify"}),
    ),
    # Commissions on, two traders allowed to cross themselves, two instruments
    # sharing a currency: the ledgers, the self-matching gate and a currency
    # balance that nets across two books, all at once. The 0.05 tick is where
    # a quantizer that reads the tick as a double gets it wrong.
    Profile(
        name="ledgers",
        tick_size=0.05,
        grid=_halves(3990, 4012, 1),
        instruments=(("FAKE", "USD"), ("BAR", "USD")),
        commissions=Commissions(min=2.5, max_pct=1.0, per_unit=0.01),
        self_matching=(1, 3),
        expects=frozenset({"market remainder", "off-grid price", "two instruments"}),
    ),
    # A narrow book: every level many orders deep, which is where FIFO order,
    # partial fills and the modify rules actually collide -- and where a
    # reprice lands on an occupied level nearly every time.
    Profile(
        name="deep",
        tick_size=0.01,
        grid=_halves(19995, 20009, 1),
        commissions=Commissions(min=0.5, max_pct=0.5, per_unit=0.02),
        self_matching=True,
        p_limit=0.44,
        p_market=0.58,
        p_cancel=0.74,
        p_modify=1.00,
        expects=frozenset(
            {"market remainder", "off-grid price", "reprice onto an occupied level"}
        ),
    ),
    # The replay shapes: identifiers and timestamps supplied by the caller,
    # several orders sharing one timestamp, two instruments in two currencies,
    # and the engine reloading itself from its event stream mid-workload.
    Profile(
        name="replay",
        tick_size=0.0001,
        grid=_halves(1999990, 2000010, 3),
        instruments=(("FAKE", "USD"), ("BAZ", "EUR")),
        commissions=Commissions(min=1.0, max_pct=2.0, per_unit=0.005),
        self_matching=(2,),
        ops=140,
        p_limit=0.50,
        p_market=0.64,
        p_cancel=0.78,
        p_modify=0.94,
        p_supplied=0.35,
        max_reloads=2,
        expects=frozenset(
            {"supplied identifier", "reload", "off-grid price", "two instruments"}
        ),
    ),
)

SEEDS = (1, 2, 3, 4, 5, 6, 7, 8)


class Generator:
    """The seeded workload generator, and every constraint in `CONSTRAINTS`.

    State-adaptive by necessity rather than by taste: naming a live order means
    reading the book as it stands. It reads it from the *reference* -- the
    oracle -- and only ever at a point where the two have just been compared
    equal, so the choice is immaterial to the workload and material to nothing
    else. Reading the oracle rather than the engine also means a broken engine
    cannot steer the generator away from the input that would expose it.

    Deterministic given (profile, seed): the random stream is, and both
    implementations are, so the operation sequence is too.
    """

    def __init__(self, profile, seed):
        self.profile = profile
        self.rng = random.Random(seed)
        self.next_supplied = FIRST_SUPPLIED_ID
        self.reloads = 0

    def next_op(self, oracle):
        rng = self.rng
        profile = self.profile
        live = self._live(oracle)
        roll = rng.random()

        if roll < profile.p_limit or not live:
            return self._submit("limit")
        if roll < profile.p_market:
            return self._submit("market")
        if roll < profile.p_cancel:
            # CONSTRAINTS["cancel and modify address a live resting order"]:
            # a snapshot lists exactly the orders that are still in the book.
            return Op(kind="cancel", idNum=rng.choice(live)[0])
        if roll < profile.p_modify or self.reloads >= profile.max_reloads:
            return self._modify(oracle, rng.choice(live))
        self.reloads += 1
        return Op(kind="reload", tags=("reload",))

    def _live(self, oracle):
        """(idNum, instrument, side) for every order resting anywhere."""
        found = []
        for symbol, _currency in self.profile.instruments:
            for side in ("bid", "ask"):
                for entry in oracle.snapshot(side, symbol):
                    found.append((entry.idNum, symbol, side))
        return found

    def _submit(self, kind):
        """A limit or a market order, occasionally on the data-replay path.

        A market order's quantity is drawn without reference to the liquidity
        it can reach, so it often outruns the book -- `order-lifecycle` says
        the remainder is cancelled and never rests, and the old harness had to
        exclude exactly this because the legacy engine rested it (`lob-0bl`).
        """
        rng = self.rng
        profile = self.profile
        tags = []
        price = UNSET
        if kind == "limit":
            price = profile.price(rng.choice(profile.grid))
            if price != reference_matcher.quantize(price, profile.tick_size):
                tags.append("off-grid price")

        idNum = timestamp = None
        if rng.random() < profile.p_supplied:
            idNum = self.next_supplied
            self.next_supplied += SUPPLIED_STRIDE
            timestamp = REPLAY_TIMESTAMP
            tags.append("supplied identifier")

        return Op(
            kind=kind,
            instrument=rng.choice(profile.instruments)[0],
            side=rng.choice(("bid", "ask")),
            qty=rng.randint(1, 12 if kind == "market" else 8),
            price=price,
            tid=rng.choice(TRADERS),
            idNum=idNum,
            timestamp=timestamp,
            tags=tuple(tags),
        )

    def _modify(self, oracle, target):
        """A quantity change, a price change, both, or neither stated.

        The price is drawn from the whole grid, so a reprice lands on an
        occupied level whenever the book is busy -- the case where a rule that
        re-stamps arrival only on a quantity increase parts company with one
        that re-stamps it on a price change too (`lob-pn3`, which the old
        harness had to steer around).

        Not naming the price at all and naming it as an explicit `None` are
        both generated: `order-lifecycle` says they mean the same thing, and
        `lob-crf` is what happens when they do not.
        """
        rng = self.rng
        profile = self.profile
        idNum, instrument, side = target
        tags = []

        wants_price = rng.random() < 0.5
        wants_qty = rng.random() < 0.55 or not wants_price

        price = UNSET
        if wants_price:
            price = profile.price(rng.choice(profile.grid))
            if price != reference_matcher.quantize(price, profile.tick_size):
                tags.append("off-grid price")
            occupied = {entry.price for entry in oracle.snapshot(side, instrument)} - {
                oracle.order_state(idNum).price
            }
            if reference_matcher.quantize(price, profile.tick_size) in occupied:
                tags.append("reprice onto an occupied level")
        elif rng.random() < 0.5:
            price = None
            tags.append("price=None modify")

        return Op(
            kind="modify",
            instrument=instrument,
            side=side,
            idNum=idNum,
            qty=rng.randint(1, 12) if wants_qty else UNSET,
            price=price,
            tags=tuple(tags),
        )


def apply_op(engine, op):
    """Run one operation; return `(idNum, trades)` as that implementation saw it."""
    if op.kind == "reload":
        engine.reopen()
        return None, ()
    if op.kind == "limit":
        ref = engine.limit(
            op.side,
            op.qty,
            op.price,
            op.tid,
            instrument=op.instrument,
            idNum=op.idNum,
            timestamp=op.timestamp,
        )
        return ref.idNum, tuple(ref.trades)
    if op.kind == "market":
        ref = engine.market(
            op.side,
            op.qty,
            op.tid,
            instrument=op.instrument,
            idNum=op.idNum,
            timestamp=op.timestamp,
        )
        return ref.idNum, tuple(ref.trades)
    if op.kind == "cancel":
        engine.cancel(op.idNum)
        return op.idNum, ()
    kwargs = {}
    if op.qty is not UNSET:
        kwargs["qty"] = op.qty
    if op.price is not UNSET:
        # An explicit `price=None` reaches the adapter as an explicit None; a
        # price the generator did not mention never reaches it at all.
        kwargs["price"] = op.price
    return op.idNum, tuple(engine.modify(op.idNum, **kwargs))


# --------------------------------------------------------------------------
# the comparator
# --------------------------------------------------------------------------


@dataclass
class Difference:
    """One thing the two implementations disagree about, ready to be printed."""

    what: str
    reference: object
    engine: object
    detail: str = ""

    def render(self):
        lines = [
            "  %s" % self.what,
            "      reference: %s" % _show(self.reference),
            "      engine   : %s" % _show(self.engine),
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
            return "first differs at position %d: reference %s, engine %s" % (
                index,
                _one(a),
                _one(b),
            )
    return "same first %d, then reference has %d and engine %d" % (
        min(len(left), len(right)),
        len(left),
        len(right),
    )


def _money_differs(a, b):
    return a != pytest.approx(b, rel=MONEY_REL, abs=MONEY_ABS)


def differences(reference, engine, profile, known_ids=(), probes=()):
    """Everything the two implementations disagree about right now, as a list.

    Empty means they agree on the whole of the observable surface: for every
    instrument, both books in priority order, the four price accessors,
    volume-at-price at `probes`, the last price and the entire trade log; then
    every order in `known_ids`, and every trader's balance in every instrument
    and every currency.

    Money is compared within `MONEY_REL` / `MONEY_ABS`; everything else
    exactly.
    """
    found = []

    for symbol, _currency in profile.instruments:
        where = "" if len(profile.instruments) == 1 else " of %s" % symbol
        for side in ("bid", "ask"):
            left = reference.snapshot(side, symbol)
            right = engine.snapshot(side, symbol)
            if left != right:
                found.append(
                    Difference(
                        "book[%s]%s (price, then priority order)" % (side, where),
                        left,
                        right,
                        _first_mismatch(left, right),
                    )
                )
            for query in ("best", "worst"):
                a = getattr(reference, query)(side, symbol)
                b = getattr(engine, query)(side, symbol)
                if a != b:
                    found.append(
                        Difference("%s %s price%s" % (query, side, where), a, b)
                    )
            for price in probes:
                a = reference.volume_at(side, price, symbol)
                b = engine.volume_at(side, price, symbol)
                if a != b:
                    found.append(
                        Difference("volume_at[%s, %s]%s" % (side, price, where), a, b)
                    )

        left, right = reference.trades(symbol), engine.trades(symbol)
        if left != right:
            found.append(
                Difference(
                    "trade log%s (%d vs %d executions)"
                    % (where, len(left), len(right)),
                    left[-4:],
                    right[-4:],
                    _first_mismatch(left, right),
                )
            )

        a, b = reference.last_price(symbol), engine.last_price(symbol)
        if a != b:
            found.append(Difference("last price%s" % where, a, b))

    for idNum in known_ids:
        left, right = reference.order_state(idNum), engine.order_state(idNum)
        if left is None or right is None:
            if left is not right:
                found.append(Difference("order %d exists" % idNum, left, right))
            continue
        if left[:-1] != right[:-1]:
            fields = [
                "%s: reference %r vs engine %r" % (name, a, b)
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
        for symbol in profile.symbols:
            a = reference.balance(tid, symbol)
            b = engine.balance(tid, symbol)
            if _money_differs(a, b):
                found.append(Difference("balance[%d, %s]" % (tid, symbol), a, b))

    return found


# --------------------------------------------------------------------------
# running a workload
# --------------------------------------------------------------------------


@dataclass
class Run:
    """One workload in flight: the two implementations and the trail behind them."""

    profile: Profile
    seed: int
    reference: object
    engine: object
    generator: Generator
    history: list = field(default_factory=list)
    known_ids: list = field(default_factory=list)
    #: What this run has actually exercised, checked against `profile.expects`.
    tags: set = field(default_factory=set)

    @property
    def probes(self):
        """Prices to ask volume-at-price about: on the grid, and off it.

        The off-grid probe is not decoration: `book-queries` puts the query
        price on the grid before answering, so a probe between two ticks is
        the only thing that compares the two quantizers on the *read* path.
        """
        grid = self.profile.grid
        return tuple(
            self.profile.price(grid[index])
            for index in (0, len(grid) // 4, len(grid) // 2, -1)
        )

    def report(self, index, op, found):
        """The failure message. This is the whole point of the harness."""
        recent = self.history[max(0, index - 5) : index]
        lines = [
            "differential divergence: the reference matcher and the engine disagree",
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
            "    %4d  %-58s -> id %s" % (position, done.render(), idNum)
            for position, done, idNum in recent
        )
        lines.append("")
        lines.append("  books at the divergence:")
        for symbol, _currency in self.profile.instruments:
            for side in ("bid", "ask"):
                lines.append(
                    "    reference %s %-4s: %s"
                    % (symbol, side, _show_book(self.reference.snapshot(side, symbol)))
                )
                lines.append(
                    "    engine    %s %-4s: %s"
                    % (symbol, side, _show_book(self.engine.snapshot(side, symbol)))
                )
        lines.append("")
        lines.append(
            "  reproduce: uv run pytest tests/test_differential.py -k "
            "'%s and seed%d'" % (self.profile.name, self.seed)
        )
        return "\n".join(lines)

    def step(self, index):
        op = self.generator.next_op(self.reference)
        reference_id, reference_trades = apply_op(self.reference, op)
        engine_id, engine_trades = apply_op(self.engine, op)

        found = []
        if reference_id != engine_id:
            found.append(Difference("identifier assigned", reference_id, engine_id))
        if reference_trades != engine_trades:
            found.append(
                Difference(
                    "executions returned by this operation",
                    reference_trades,
                    engine_trades,
                    _first_mismatch(reference_trades, engine_trades),
                )
            )
        found.extend(
            differences(
                self.reference,
                self.engine,
                self.profile,
                known_ids=self.known_ids,
                probes=self.probes,
            )
        )

        self.tags.update(op.tags)
        if op.kind in ("limit", "market"):
            self.known_ids.append(reference_id)
            state = self.reference.order_state(reference_id)
            if op.kind == "market" and state.fulfilled < state.qty:
                self.tags.add("market remainder")
        self.history.append((index, op, reference_id))

        if found:
            raise AssertionError(self.report(index, op, found))


def build_pair(tmp_path, profile, name="run"):
    """The reference and the engine, configured identically."""
    first, first_currency = profile.instruments[0]
    options = dict(
        traders=TRADERS,
        commissions=profile.commissions,
        self_matching=profile.self_matching,
        instrument=first,
        currency=first_currency,
        tick_size=profile.tick_size,
    )
    reference = build_reference(tmp_path / ("%s-reference" % name), **options)
    engine = build_inmemory(tmp_path / ("%s-engine.db" % name), **options)
    for adapter in (reference, engine):
        # The acceptance builders configure one instrument; a differential
        # workload wants several. `configure_instrument` is the public call
        # that declares one, and on the engine it is also what puts an
        # `InstrumentConfigured` in the stream, so a reload rebuilds the same
        # set of books.
        for symbol, currency in profile.instruments[1:]:
            adapter.book.configure_instrument(symbol, currency)
    return reference, engine


# --------------------------------------------------------------------------
# the randomized cross-check
# --------------------------------------------------------------------------


@pytest.fixture(params=PROFILES, ids=lambda profile: profile.name)
def profile(request):
    return request.param


@pytest.mark.parametrize("seed", SEEDS, ids=lambda seed: "seed%d" % seed)
def test_the_engine_agrees_with_the_reference(tmp_path, profile, seed):
    """Every observable, after every operation, on both implementations.

    A divergence here is a *finding*, not a test to relax: either the engine
    has a bug or the reference matcher misreads a spec, and both are worth a
    bead. There is no whitelist to add it to -- that was the legacy oracle's
    apparatus and it went with the legacy oracle.
    """
    reference, engine = build_pair(tmp_path, profile, name="seed%d" % seed)
    run = Run(
        profile=profile,
        seed=seed,
        reference=reference,
        engine=engine,
        generator=Generator(profile, seed),
    )
    try:
        for index in range(profile.ops):
            run.step(index)
        # A workload that never traded would agree trivially.
        executions = {
            symbol: len(reference.trades(symbol))
            for symbol, _currency in profile.instruments
        }
    finally:
        reference.close()
        engine.close()

    assert len(run.known_ids) >= profile.ops // 3
    assert all(executions.values()), "an instrument never traded: %r" % (executions,)

    if len(profile.instruments) > 1:
        run.tags.add("two instruments")
    missing = profile.expects - run.tags
    assert not missing, (
        "this workload never reached %s, so it proves less than it claims; "
        "it reached %s" % (sorted(missing), sorted(run.tags))
    )


def test_the_comparator_would_notice(tmp_path):
    """The harness has teeth: feed the two different input, see it fail.

    Without this, a comparator that quietly compared nothing would pass every
    test above.
    """
    profile = PROFILES[0]
    reference, engine = build_pair(tmp_path, profile, name="teeth")
    reference.limit("ask", 5, 101.0, 1)
    engine.limit("ask", 4, 101.0, 1)
    found = differences(reference, engine, profile, known_ids=[1], probes=(101.0,))
    reference.close()
    engine.close()

    reported = {item.what for item in found}
    assert any("book[ask]" in what for what in reported), reported
    assert any("volume_at" in what for what in reported), reported
    assert any("order 1 state" in what for what in reported), reported


# --------------------------------------------------------------------------
# what makes the reference an oracle rather than a mirror
# --------------------------------------------------------------------------


def test_the_reference_imports_nothing_from_the_engine():
    """`tests/reference/matcher.py` may not import `PyLOB`. At all.

    This is the whole basis of the harness. An oracle that borrows the code
    under test agrees with it by construction, and the tempting way to silence
    a divergence -- import the engine's quantizer, reuse its enums -- is
    exactly the change that would turn this file into a mirror while every
    test stayed green.

    Reading the import statements rather than trusting a convention, because a
    convention is what this would be otherwise.
    """
    source = Path(reference_matcher.__file__).read_text()
    imported = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert imported, "no imports found -- did the parse work?"
    offenders = [name for name in imported if name.split(".")[0] == "PyLOB"]
    assert not offenders, (
        "the reference matcher imports %r from the engine it is supposed to "
        "check independently" % (offenders,)
    )


def test_every_constraint_says_why():
    """A constraint with no argument behind it is a habit, and habits rot.

    There are two, and both have to be about generating legal input. The nine
    that named legacy defects went with the legacy engine; if this list grows
    back, the reason had better not be "the harness found something".
    """
    assert set(CONSTRAINTS) == {
        "every generated operation is legal input",
        "cancel and modify address a live resting order",
    }
    for name, reason in CONSTRAINTS.items():
        assert len(reason) > 200, name


def test_both_refuse_what_the_specs_refuse(tmp_path):
    """The illegal input the workload does not generate, asserted directly.

    `order-lifecycle` names these: a non-positive quantity, an unknown side, an
    unknown order type, a limit order with no price, an identifier no order
    has, an externally supplied identifier already in use, and a modify that
    changes the side. Both implementations must refuse each one with *their own
    library exception* -- "Submissions with ... SHALL raise a library
    exception. The API SHALL never terminate the host process" -- and leave the
    book exactly as it was: "an exception is raised, no state changes, and the
    process continues".

    Asserting the library base class rather than `Exception` is what makes the
    process clause testable: a `SystemExit` (which is what the legacy engine
    did with a bad quantity) is not caught by either base, so it fails this
    test loudly instead of passing it quietly.

    Deterministic rather than random on purpose: a refusal changes nothing, so
    there is nothing for the randomized comparator to compare, and a fixed list
    says plainly which refusals are covered. Two of them go through the
    adapters' `_submit` because the public `limit`/`market` helpers each pin
    the order type, and those two cases are *about* the order type.
    """
    from PyLOB.engine import PyLOBError

    profile = PROFILES[0]
    reference, engine = build_pair(tmp_path, profile, name="refusals")
    try:
        reference.limit("bid", 5, 100.0, 1)
        engine.limit("bid", 5, 100.0, 1)
        assert not differences(reference, engine, profile, known_ids=[1])

        refusals = {
            "non-positive quantity": lambda book: book.limit("bid", 0, 100.0, 1),
            "negative quantity": lambda book: book.limit("ask", -3, 100.0, 1),
            "unknown side": lambda book: book.limit("buy", 5, 100.0, 1),
            "unknown order type": lambda book: book._submit(
                "stop", "bid", 5, 1, price=100.0
            ),
            "limit order with no price": lambda book: book.limit("bid", 5, None, 1),
            "market order carrying a price": lambda book: book._submit(
                "market", "bid", 5, 1, price=100.0
            ),
            "cancel of an unknown identifier": lambda book: book.cancel(9999),
            "modify of an unknown identifier": lambda book: book.modify(9999, qty=2),
            "supplied identifier already in use": lambda book: book.limit(
                "bid", 5, 100.0, 1, idNum=1, timestamp=1.0
            ),
            "modify to a different side": lambda book: book.modify(
                1, qty=3, side="ask"
            ),
        }
        sides = (
            (reference, reference_matcher.ReferenceError),
            (engine, PyLOBError),
        )
        for what, attempt in refusals.items():
            for book, library_error in sides:
                with pytest.raises(library_error):
                    attempt(book)
            found = differences(reference, engine, profile, known_ids=[1])
            assert not found, "%s: %s" % (
                what,
                "; ".join(item.render() for item in found),
            )

        # And the book really is untouched, not merely equal on both sides.
        assert reference.snapshot("bid") == (
            BookEntry(idNum=1, price=100.0, available=5, qty=5, fulfilled=0),
        )
    finally:
        reference.close()
        engine.close()


def test_the_grid_is_half_on_the_grid_and_half_off_it():
    """The deleted `lob-we3` constraint, inverted into a requirement.

    The old harness generated prices that were already exact multiples of the
    tick, so both quantizers were the identity and neither was tested. Every
    profile's grid is now whole *half*-ticks: the even ones land on a tick and
    the odd ones land exactly between two, which is where a quantizer that
    reads the tick as a binary double gets the answer wrong.

    Both quantizers must agree over the whole of every grid -- they are
    independent implementations of one clause (`order-lifecycle`: "Prices are
    quantized to the tick"), and this is the direct comparison of them.
    """
    from PyLOB.engine import quantize_price

    for profile in PROFILES:
        off_grid = 0
        for half_ticks in profile.grid:
            price = profile.price(half_ticks)
            theirs = quantize_price(price, profile.tick_size)
            ours = reference_matcher.quantize(price, profile.tick_size)
            assert ours == theirs, (profile.name, price, ours, theirs)
            if price != ours:
                off_grid += 1
        assert off_grid, "%s generates no off-grid price" % profile.name
        assert off_grid < len(profile.grid), (
            "%s generates nothing on the grid" % profile.name
        )
