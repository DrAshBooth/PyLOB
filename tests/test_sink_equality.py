"""Attaching a sink changes nothing: recorded and sinkless runs are one run.

ADR-0002 makes the sinkless configuration the default and the only one the
throughput target governs, and it justifies that with a workflow: run sinkless
and fast for the sweep -- an RL gym, a parameter sweep, many episodes -- then
attach a `SQLiteSink` for the handful of runs you actually want to read
afterwards. That workflow is only honest if the two configurations produce the
same run. If attaching a sink moved an outcome, every recorded deep-dive would
be describing something other than the fast runs it exists to explain, and the
sinkless default would be a performance number bought with a lie.

`PyLOB.events` argues structurally that they cannot differ: `seq` is a position
in the emitted stream and matching never reads it, while `priority` -- the
matching sort key -- is a separate counter that advances whether or not an
event is built. `PyLOB.engine` then guards every emission with
`if self.recording:`, so a sinkless engine constructs no event at all. This
module exists because the design being *intended* to hold is not the same
thing as it holding.

The same operations, twice
--------------------------

Both runs have to see one operation sequence, and it cannot simply be
regenerated from a shared seed, because this workload is *state-dependent*:
cancels, resizes and repricings pick their target out of the book
(`book.snapshot`), and `random.Random.choice` draws a number of random bits
that depends on the length of the list it is choosing from. A divergence in
the book would therefore change the operations themselves, and each run would
end up answering a different question -- exactly the circularity this test is
supposed to break.

So the first run *records* what it did: one `Op` per operation, carrying the
concrete identifier, quantity and price it used. The second run executes that
list verbatim and never touches the generator. The sinkless run is the one
that records, mirroring the ADR's workflow -- the fast sweep happens first and
it is the re-run under a sink that has to agree with it.

A recorded plan is a stricter comparison than a shared seed, not a looser one.
An `Op` names its target by identifier, so a second run that assigned
identifiers differently, filled an order the first run left resting, or
cancelled one the first run kept, fails on the operation itself rather than
quietly sliding onto a different workload; `rerun` turns that into a named
divergence. `skipped` steps are recorded too, so a point where the generating
run found an instrument's book empty is re-checked in the second run rather
than dropped -- the one place these tests compare the two engines mid-session
instead of at the end.

What is compared
----------------

    the book        every resting order, its price, remaining quantity and
                    position in its level's queue, on both sides of both
                    instruments -- the tuple position *is* the priority order
    the orders      per order: price, quantity, `fulfilled`, cumulative value,
                    cumulative commission, cancelled state and reason, and
                    `priority`, the field that would drift first if event
                    construction ever touched a counter matching reads
    the ledgers     every `(trader, symbol)` amount, instruments and
                    currencies alike
    reporting       each instrument's last-trade price
    the trades      the executions themselves: same trades, same prices, same
                    quantities, in the same order

The capture machinery, the money tolerance and the "was this workload
substantial" guard come from `tests/test_replay.py`, a sibling module rather
than a conftest and so importable. They are the same questions asked of a
different pair of engines, and a second copy of `EndState` would be a second
thing to keep correct. The comparison itself is restated here, because a
failure should say which configuration diverged and not "after replay".
"""

from __future__ import annotations

import random
from collections import Counter
from typing import NamedTuple

import pytest
from PyLOB.engine import OrderBook as InMemoryOrderBook
from PyLOB.engine import PyLOBError
from PyLOB.sinks.sqlite import SQLiteSink, read_events

# The replay suite is a sibling test module (`tests/` has no `__init__.py`, so
# pytest puts it on `sys.path`). Imported as a whole rather than name by name
# so that every borrowed piece is visibly borrowed at its use site.
import test_replay as replay_suite

TICK = replay_suite.TICK
INSTRUMENTS = replay_suite.INSTRUMENTS
SYMBOLS = replay_suite.SYMBOLS
TRADERS = replay_suite.TRADERS
TIDS = replay_suite.TIDS
SIDES = replay_suite.SIDES
OPS = replay_suite.OPS
WEIGHTS = replay_suite.WEIGHTS
N_OPS = replay_suite.N_OPS

#: A price on the passive or aggressive side of the mid. Private in the replay
#: suite, borrowed anyway: the workload mix has to stay identical to the one
#: `assert_workload_was_substantial` was calibrated against, and a second
#: hand-tuned price band would silently decouple the two.
price_for = replay_suite._price


# --------------------------------------------------------------------------
# the two configurations
# --------------------------------------------------------------------------


def configure(book):
    """Declare both instruments and all five traders on `book`.

    Deliberately identical for both configurations: the commission schedules
    and the self-matching flag change what matching does, so a difference here
    would be a difference in the experiment rather than in the sink.
    """
    for symbol, currency in INSTRUMENTS:
        book.configure_instrument(symbol, currency)
    for tid, self_matching, minimum, max_percnt, per_unit in TRADERS:
        book.configure_trader(
            tid,
            name="trader%d" % tid,
            allow_self_matching=self_matching,
            commission_min=minimum,
            commission_max_percnt=max_percnt,
            commission_per_unit=per_unit,
        )


def build(sink=None):
    """A fresh, configured engine recording to `sink` -- or to nothing."""
    book = InMemoryOrderBook(tick_size=TICK, sink=sink)
    configure(book)
    return book


class CountingSink:
    """Satisfies `events.EventSink` and does nothing but tally.

    No I/O and no encoding, so it isolates the question "was an event built at
    all" from everything `SQLiteSink` does with one afterwards.
    """

    def __init__(self):
        self.kinds = Counter()

    def consume(self, event, /):
        self.kinds[event.KIND] += 1

    @property
    def total(self):
        """How many events this sink has been handed."""
        return sum(self.kinds.values())


# --------------------------------------------------------------------------
# the workload, as a replayable plan
# --------------------------------------------------------------------------


class Op(NamedTuple):
    """One operation, recorded concretely enough to be re-executed verbatim.

    Every field the engine call needs is here as a value -- no generator, no
    book lookup, no random draw. `idNum` names the target of a cancel or a
    modify; `kind` `"skipped"` records that the generating run found the
    instrument's book empty and had no legal target to pick.
    """

    kind: str
    instrument: str
    tid: int | None = None
    side: str | None = None
    order_type: str | None = None
    qty: int | None = None
    price: float | None = None
    idNum: int | None = None


def resting_orders(book, instrument):
    """Every order resting in `instrument`, both sides, in priority order."""
    return book.snapshot(instrument, "bid") + book.snapshot(instrument, "ask")


def apply_op(book, op, counts):
    """Execute one recorded operation against `book`; return its executions.

    `counts` is tallied here rather than by the caller so that the generating
    run and the re-run cannot count differently -- the tallies are compared,
    and a comparison of two numbers produced by two pieces of code proves
    less than one of two numbers produced by the same piece.
    """
    if op.kind == "skipped":
        # The generating run found nothing resting in this instrument, so
        # there was no legal target to pick. Re-checking costs the snapshot
        # the generator paid for anyway and compares the two books here, in
        # the middle of the session, rather than only at the end of it.
        assert not resting_orders(book, op.instrument), (
            "%s has resting orders at this point and the other run had none"
            % op.instrument
        )
        counts["skipped"] += 1
        return []

    if op.kind in ("passive", "cross", "market"):
        _, made = book.submit(
            tid=op.tid,
            instrument=op.instrument,
            side=op.side,
            order_type=op.order_type,
            qty=op.qty,
            price=op.price,
        )
    elif op.kind == "cancel":
        book.cancelOrder(op.side, op.idNum)
        made = []
    elif op.kind == "resize":
        made, _ = book.modifyOrder(op.idNum, dict(side=op.side, qty=op.qty, price=None))
    elif op.kind == "reprice":
        made, _ = book.modifyOrder(
            op.idNum, dict(side=op.side, qty=None, price=op.price)
        )
    else:  # pragma: no cover - a new operation kind, unhandled
        raise AssertionError("unknown operation kind %r" % (op.kind,))

    counts[op.kind] += 1
    counts["trades"] += len(made)
    if made and op.kind in ("resize", "reprice"):
        # A modification that crossed: whether it was entitled to trade
        # depends on `fulfilled`, so these are where a drift would show.
        counts["modify_trades"] += len(made)
    return made


def generate(book, rng, n_ops=N_OPS):
    """Drive `n_ops` random operations through `book`, recording each one.

    The mix is `test_replay`'s -- passive limits build the book, crossing
    limits and markets take from it, cancels and modifies churn it, with
    repricing modifies over-represented -- because the substantiality guard
    below was calibrated against those weights.

    Returns `(plan, trades, counts)`. The plan is the whole point: it is what
    the second configuration executes, and it contains no reference to `rng`
    or to the state that produced it.
    """
    plan = []
    trades = []
    counts = Counter()
    for _ in range(n_ops):
        instrument = rng.choice(SYMBOLS)
        tid = rng.choice(TIDS)
        kind = rng.choices(OPS, weights=WEIGHTS)[0]

        if kind in ("passive", "cross"):
            side = rng.choice(SIDES)
            op = Op(
                kind,
                instrument,
                tid=tid,
                side=side,
                order_type="limit",
                qty=rng.randint(1, 10),
                price=price_for(rng, side, aggressive=kind == "cross"),
            )
        elif kind == "market":
            op = Op(
                kind,
                instrument,
                tid=tid,
                side=rng.choice(SIDES),
                order_type="market",
                qty=rng.randint(1, 15),
            )
        else:
            candidates = resting_orders(book, instrument)
            if not candidates:
                op = Op("skipped", instrument)
            else:
                # Drawn from the engine's own snapshot, so the operation is
                # always a legal one -- and this is exactly the dependence on
                # book state that rules out regenerating from the seed twice.
                target = rng.choice(candidates)
                side = str(target.side)
                if kind == "cancel":
                    op = Op(kind, instrument, side=side, idNum=target.idNum)
                elif kind == "resize":
                    # May land below `fulfilled`, which the engine clamps up
                    # to it and reports as a modification, not a cancellation.
                    op = Op(
                        kind,
                        instrument,
                        side=side,
                        qty=rng.randint(1, target.qty + 4),
                        idNum=target.idNum,
                    )
                else:
                    op = Op(
                        kind,
                        instrument,
                        side=side,
                        price=price_for(rng, side, aggressive=rng.random() < 0.6),
                        idNum=target.idNum,
                    )

        plan.append(op)
        trades.extend(apply_op(book, op, counts))
    return plan, trades, counts


def rerun(book, plan):
    """Execute a recorded `plan` against `book`, verbatim.

    An operation that the generating run performed legally and this one
    cannot is already a divergence -- the identifier names an order that does
    not exist here, or is filled here, or is cancelled here -- so the engine's
    own refusal is caught and reported as what it is rather than surfacing as
    a bare `PyLOBError` from somewhere inside the workload.
    """
    trades = []
    counts = Counter()
    for position, op in enumerate(plan):
        try:
            trades.extend(apply_op(book, op, counts))
        except PyLOBError as exc:
            raise AssertionError(
                "operation %d %r is illegal in this configuration but was "
                "legal in the one that recorded it: the two have already "
                "diverged (%s)" % (position, op, exc)
            ) from exc
    return trades, counts


# --------------------------------------------------------------------------
# comparing the two outcomes
# --------------------------------------------------------------------------


def assert_same_outcome(sinkless, recorded):
    """Every field of two `EndState`s, one kind at a time.

    Split up rather than asserted as one equality so that a failure names
    *what* the sink moved -- a book whose queue reordered, an order whose
    accounting drifted, a ledger that came out different -- instead of dumping
    two engines side by side. Any failure here is an ADR-0002 finding, not a
    test bug: it would mean recorded runs and fast runs are different runs.
    """
    for key, entries in sinkless.queues.items():
        assert recorded.queues[key] == entries, (
            "the %s %s book differs between the sink-attached and the "
            "sinkless run" % key
        )

    assert set(recorded.orders) == set(sinkless.orders), (
        "the two runs accepted different sets of order identifiers"
    )
    for idNum, state in sinkless.orders.items():
        assert recorded.orders[idNum] == state, (
            "order %d differs between the sink-attached and the sinkless run" % idNum
        )

    assert set(recorded.money) == set(sinkless.money)
    assert recorded.money == replay_suite.approx_money(sinkless.money), (
        "cumulative value or commission differs with a sink attached"
    )

    assert set(recorded.balances) == set(sinkless.balances), (
        "the two runs moved different sets of (trader, symbol) balances"
    )
    assert recorded.balances == replay_suite.approx_money(sinkless.balances), (
        "a trader balance differs with a sink attached"
    )

    assert recorded.last_price == sinkless.last_price, (
        "an instrument's last-trade price differs with a sink attached"
    )


# --------------------------------------------------------------------------
# the tests
# --------------------------------------------------------------------------

SEEDS = (3, 11, 29, 61, 97)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_sink_changes_no_outcome(tmp_path, seed):
    """The same workload, sinkless and then recorded, ends up identical.

    The order is ADR-0002's workflow: the fast sinkless run happens first and
    records what it did, and the run a user would attach a `SQLiteSink` to in
    order to inspect it afterwards has to reproduce it exactly.
    """
    sinkless = build(sink=None)
    assert not sinkless.recording
    plan, sinkless_trades, sinkless_counts = generate(sinkless, random.Random(seed))
    sinkless_state = replay_suite.capture(sinkless)
    replay_suite.assert_workload_was_substantial(
        sinkless_state, sinkless_trades, sinkless_counts
    )

    db_path = tmp_path / ("session%d.db" % seed)
    recorded = build(sink=SQLiteSink(db_path))
    assert recorded.recording
    recorded_trades, recorded_counts = rerun(recorded, plan)
    recorded_state = replay_suite.capture(recorded)
    recorded.close()

    assert recorded_counts == sinkless_counts, (
        "the two runs did not execute the same operations"
    )
    assert_same_outcome(sinkless_state, recorded_state)
    # The trade sequence: derived independently by each engine, never copied
    # between them, so equality here is the matching running the same way.
    assert replay_suite.trade_log(recorded_trades) == replay_suite.trade_log(
        sinkless_trades
    )

    # And the sink was genuinely doing its work rather than quietly detached,
    # which would make everything above vacuous. One `Accepted` per submission
    # and one `Filled` per execution the engine reported.
    kinds = Counter(event.KIND for event in read_events(db_path))
    submissions = sum(sinkless_counts[kind] for kind in ("passive", "cross", "market"))
    assert kinds["accepted"] == submissions
    assert kinds["filled"] == sinkless_counts["trades"]


def test_a_sinkless_engine_builds_no_event():
    """Nothing is constructed when nothing is listening -- ADR-0002's premise.

    ADR-0002 rests on sinkless being *free* rather than merely cheap: "the
    engine constructs no event at all when no sink is attached". Every event
    in `PyLOB.engine` is built as the argument of an `emit` call guarded by
    `if self.recording:`, so an `emit` that is never reached is an event that
    was never built, and a tripwire in place of `emit` measures exactly that.

    The one exception is `SessionStarted`, which the constructor emits
    ungated: a sinkless engine builds precisely one event, once, before it has
    accepted anything. The tripwire goes on immediately afterwards, so
    configuration and the whole workload are covered.

    This test also drives the plan the other way round -- the recorded run
    generates and the sinkless run replays -- and compares the outcomes, so
    the guarantee is shown not to depend on the direction, and not to be a
    property of `SQLiteSink` in particular.
    """
    counting = CountingSink()
    recorded = build(sink=counting)
    plan, recorded_trades, recorded_counts = generate(
        recorded, random.Random(2024), n_ops=600
    )
    recorded_state = replay_suite.capture(recorded)
    assert counting.total > len(plan), counting.kinds
    assert counting.kinds["accepted"] and counting.kinds["filled"]

    sinkless = InMemoryOrderBook(tick_size=TICK, sink=None)
    assert not sinkless.recording
    built = []
    # `OrderBook` has no `__slots__`, so this shadows the bound method for
    # this instance only and every internal `self.emit(...)` finds it.
    sinkless.emit = built.append
    configure(sinkless)
    sinkless_trades, sinkless_counts = rerun(sinkless, plan)

    assert built == [], (
        "a sinkless engine constructed %d events; ADR-0002's sinkless "
        "throughput figure assumes it constructs none" % len(built)
    )

    assert sinkless_counts == recorded_counts
    assert_same_outcome(replay_suite.capture(sinkless), recorded_state)
    assert replay_suite.trade_log(sinkless_trades) == replay_suite.trade_log(
        recorded_trades
    )
