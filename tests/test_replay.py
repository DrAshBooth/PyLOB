"""Replay: a persisted event stream fed into a fresh engine reaches the same state.

This suite pins the `recording-sink` scenario "State reconstructs from the
log", and it pins it on the shipped `PyLOB.replay` -- the function a researcher
calls -- rather than on a copy of it kept here.

`PyLOB.events` (the "Replay" section) says the stream, not a live database, is
what a session persists: re-issuing the *replayable* events in `seq` order into
a fresh `PyLOB.engine.OrderBook` must reproduce the session. `Filled` and the
`Cancelled` of an IOC remainder are engine **output** -- the replayed engine
re-derives every one of them from the commands -- which is what makes this a
determinism check rather than a restore. Nothing here reads a fill back in.

So the assertions are on *end state*, not on the stream having been read:

    the book        every resting order, its price, its remaining quantity, and
                    its position in its level's queue, on both sides of both
                    instruments
    the orders      per order: price, quantity, `fulfilled`, cumulative value,
                    cumulative commission, cancelled state and reason, priority
    the ledgers     every `(trader, symbol)` amount the ledger has moved,
                    instruments and currencies alike
    reporting       each instrument's last-trade price
    the trades      the executions themselves: same trades, same prices, same
                    quantities, same order

The workload is seeded and randomised rather than hand-written -- a five-line
scenario replays correctly on an engine that could not replay anything harder.
Each test carries its own seed, so a failure reproduces exactly.

Repricing modifies are the interesting operations here and the workload is
weighted to produce plenty of them: a repriced order re-enters matching as a
taker, and whether it may trade at all depends on `fulfilled`, which the replay
has to have rebuilt correctly from every earlier command rather than read from
a projection.

`harness.inmemory`'s `InMemoryAdapter.reopen` performs the same reload behind
the engine-neutral acceptance surface, and calls the same shipped function.
This suite works at the engine level instead, because that surface hides
exactly what it exists to compare -- per-order accounting, priority stamps, the
whole ledger, more than one instrument.
"""

from __future__ import annotations

import random
import subprocess
import sys
from collections import Counter
from dataclasses import replace as replace_field
from typing import NamedTuple

import pytest
from harness import MONEY_ABS, MONEY_REL
from PyLOB import ReplayError, replay
from PyLOB.engine import OrderBook as InMemoryOrderBook
from PyLOB.events import Filled, is_replayable
from PyLOB.sinks import ListSink
from PyLOB.sinks.sqlite import SQLiteSink, read_events

# --------------------------------------------------------------------------
# comparing money
# --------------------------------------------------------------------------

# Commissions and balances are unrounded floating-point sums -- the
# `commissions` contract is explicit that the value is the exact result of the
# formula, with no currency quantization -- so they are compared within a
# tolerance and never for bit equality. `MONEY_REL` / `MONEY_ABS` are the
# acceptance suites' own, from `tests/harness/`, which is where the
# `approx_money` fixture gets them too.


def approx_money(expected):
    """A commission or balance, compared within the money tolerance."""
    return pytest.approx(expected, rel=MONEY_REL, abs=MONEY_ABS)


# --------------------------------------------------------------------------
# the workload
# --------------------------------------------------------------------------

TICK = 0.05

#: Two instruments in two currencies, so the ledger holds several symbols per
#: trader and a replay that mixed the books up would show it.
INSTRUMENTS = (("FAKE", "USD"), ("BOGUS", "EUR"))
SYMBOLS = tuple(symbol for symbol, _ in INSTRUMENTS)

#: (tid, allow_self_matching, commission_min, commission_max_percnt,
#: commission_per_unit). Deliberately unalike: trader 1 pays nothing, 2 and 3
#: are floor-bound, 4 and 5 are cap-bound, so the two branches of
#: `commission_for` both run. Trader 3 may match its own resting orders, which
#: is the one rule that makes matching depend on *who* is submitting.
TRADERS = (
    (1, False, 0.0, 0.0, 0.0),
    (2, False, 2.5, 1.0, 0.01),
    (3, True, 1.0, 0.5, 0.02),
    (4, False, 0.0, 2.0, 0.05),
    (5, False, 5.0, 0.25, 0.0),
)
TIDS = tuple(tid for tid, *_ in TRADERS)

SIDES = ("bid", "ask")

#: Operation mix. Passive limits build the book, crossing limits and markets
#: take from it, and cancels and modifies churn it. Repricing modifies are
#: over-represented on purpose (see the module docstring).
OPS = ("passive", "cross", "market", "cancel", "resize", "reprice")
WEIGHTS = (30, 20, 8, 12, 12, 18)

#: Operations per seeded session. Large enough that the book keeps depth on
#: both sides while orders are constantly being filled, cancelled and moved.
N_OPS = 2000

MID = 100.0


def configure(book):
    """Declare both instruments and all five commission schedules; return `book`.

    Configuration goes through the engine's own calls so that the tick size,
    both currencies and all five commission schedules reach the stream as
    events -- a replay cannot charge the same commissions otherwise.
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
    return book


def build_recorded(db_path):
    """A fresh engine recording to `db_path`, configured and empty."""
    return configure(InMemoryOrderBook(tick_size=TICK, sink=SQLiteSink(db_path)))


def build_listed():
    """A fresh engine recording to a `ListSink`; returns `(book, sink)`.

    No file and no `sqlite3`: the same session, kept in memory, which is the
    other shape of input `replay` accepts.
    """
    sink = ListSink()
    return configure(InMemoryOrderBook(tick_size=TICK, sink=sink)), sink


def _price(rng, side, aggressive):
    """A price on the passive or the aggressive side of `MID`.

    Off the tick grid on purpose -- the engine quantizes, and `Accepted.price`
    carries the quantized value, so a replay that re-quantized differently
    would put orders at different levels.

    The passive band reaches further out than the aggressive one does, so the
    session ends with real depth on both sides of both books rather than with
    the last few orders that happened to survive.
    """
    edge = -rng.uniform(0.0, 0.6) if aggressive else rng.uniform(0.05, 2.5)
    return MID - edge if side == "bid" else MID + edge


def _resting(book, symbol, rng):
    """One resting order of `symbol`, or None when the book is empty.

    Drawn from the engine's own snapshots, so the operation that follows is
    always a legal one: a resting order is neither cancelled nor fully filled.
    """
    orders = book.snapshot(symbol, "bid") + book.snapshot(symbol, "ask")
    return rng.choice(orders) if orders else None


def run_workload(book, rng, n_ops=N_OPS):
    """Drive `n_ops` random operations through `book`.

    Returns `(trades, counts)`: every execution the engine reported, in the
    order it reported them, and a tally of what was actually exercised -- which
    the tests assert on, so a workload that quietly stopped trading cannot pass
    by comparing two empty books.
    """
    trades = []
    counts = Counter()
    for _ in range(n_ops):
        symbol = rng.choice(SYMBOLS)
        tid = rng.choice(TIDS)
        op = rng.choices(OPS, weights=WEIGHTS)[0]

        if op in ("passive", "cross"):
            side = rng.choice(SIDES)
            _, made = book.submit(
                tid=tid,
                instrument=symbol,
                side=side,
                order_type="limit",
                qty=rng.randint(1, 10),
                price=_price(rng, side, aggressive=op == "cross"),
            )
        elif op == "market":
            _, made = book.submit(
                tid=tid,
                instrument=symbol,
                side=rng.choice(SIDES),
                order_type="market",
                qty=rng.randint(1, 15),
            )
        else:
            target = _resting(book, symbol, rng)
            if target is None:
                counts["skipped"] += 1
                continue
            side = str(target.side)
            if op == "cancel":
                book.cancelOrder(side, target.idNum)
                made = []
            elif op == "resize":
                # May land below `fulfilled`, which the engine clamps up to it
                # and reports as a modification rather than a cancellation.
                made, _ = book.modifyOrder(
                    target.idNum,
                    dict(side=side, qty=rng.randint(1, target.qty + 4), price=None),
                )
            else:
                made, _ = book.modifyOrder(
                    target.idNum,
                    dict(
                        side=side,
                        qty=None,
                        price=_price(rng, side, aggressive=rng.random() < 0.6),
                    ),
                )

        counts[op] += 1
        counts["trades"] += len(made)
        if made and op in ("resize", "reprice"):
            # A modification that crossed. The interesting replay case: whether
            # it was entitled to trade depends on accounting the replay rebuilt.
            counts["modify_trades"] += len(made)
        trades.extend(made)
    return trades, counts


# --------------------------------------------------------------------------
# replaying a recorded stream
# --------------------------------------------------------------------------

# There is no replay implementation here. `PyLOB.replay` is the one in the
# repository, and every test below drives it, so what these assertions hold is
# what a researcher gets.
#
# A recorded session is replayed by composing two shipped functions:
# `read_events` decodes the log into events, `replay` re-issues the commands
# among them. Composition rather than a `replay(path)` convenience is what
# keeps `sqlite3` off `import PyLOB` -- and it is what lets the same function
# replay a `ListSink`'s list, which no path would name.
#
# The filter is applied by `replay` itself, by calling `events.is_replayable`.
# Passing the *unfiltered* stream below is therefore deliberate: it exercises
# that filter rather than the sink's materialised `replayable` column, which
# `test_the_replayable_column_and_the_filter_agree` compares it against.


def replayed_from(db_path, sink=None):
    """`replay` over the whole recorded log at `db_path`. Two calls, no logic."""
    return replay(read_events(db_path), sink=sink)


# --------------------------------------------------------------------------
# end state, field by field
# --------------------------------------------------------------------------


class QueueEntry(NamedTuple):
    """One resting order, as its level's queue holds it.

    Position in the captured tuple *is* the priority position, so comparing
    the tuples compares the queue order and not merely its membership.
    """

    idNum: int
    price: float
    remaining: int
    qty: int
    fulfilled: int
    priority: int


class OrderState(NamedTuple):
    """One order's non-money state, exactly comparable."""

    tid: int
    instrument: str
    side: str
    order_type: str
    price: float | None
    qty: int
    fulfilled: int
    cancelled: bool
    cancel_reason: str | None
    priority: int
    resting: bool


class EndState(NamedTuple):
    """Everything observable about an engine once its session is over."""

    #: (instrument, side) -> the level queues, best price first.
    queues: dict
    #: idNum -> OrderState.
    orders: dict
    #: (idNum, field) -> a cumulative money total. Compared with a tolerance.
    money: dict
    #: (tid, symbol) -> amount. Compared with a tolerance.
    balances: dict
    #: instrument -> last trade price.
    last_price: dict


def capture(book):
    """The end state of `book`, in comparable form."""
    queues = {
        (symbol, side): tuple(
            QueueEntry(
                idNum=order.idNum,
                price=order.price,
                remaining=order.remaining,
                qty=order.qty,
                fulfilled=order.fulfilled,
                priority=order.priority,
            )
            for order in book.snapshot(symbol, side)
        )
        for symbol in SYMBOLS
        for side in SIDES
    }
    orders = {}
    money = {}
    for order in book.orders():
        orders[order.idNum] = OrderState(
            tid=order.tid,
            instrument=order.instrument,
            side=str(order.side),
            order_type=str(order.order_type),
            price=order.price,
            qty=order.qty,
            fulfilled=order.fulfilled,
            cancelled=order.cancelled,
            cancel_reason=(
                None if order.cancel_reason is None else str(order.cancel_reason)
            ),
            priority=order.priority,
            resting=order.resting,
        )
        money[order.idNum, "value"] = order.value
        money[order.idNum, "commission"] = order.commission
    return EndState(
        queues=queues,
        orders=orders,
        money=money,
        balances={(tid, symbol): amount for tid, symbol, amount in book.holdings()},
        last_price={symbol: book.getLastPrice(symbol) for symbol in SYMBOLS},
    )


def trade_log(trades):
    """The trade sequence, as comparable tuples in the order it happened."""
    return [
        (
            trade.trade_id,
            trade.instrument,
            trade.price,
            trade.qty,
            str(trade.taker_side),
            trade.bid_idNum,
            trade.bid_tid,
            trade.ask_idNum,
            trade.ask_tid,
        )
        for trade in trades
    ]


def assert_same_end_state(original, replayed):
    """Every field of two `EndState`s, compared one kind at a time.

    Split up rather than asserted as one equality so that a failure names
    *what* diverged -- a book that lost an order, an order whose accounting
    drifted, a ledger that double-counted -- instead of dumping two engines.
    """
    for key, entries in original.queues.items():
        assert replayed.queues[key] == entries, (
            "the %s %s book differs after replay" % key
        )

    assert set(replayed.orders) == set(original.orders), (
        "replay did not reproduce the same set of order identifiers"
    )
    for idNum, state in original.orders.items():
        assert replayed.orders[idNum] == state, "order %d differs after replay" % idNum

    assert set(replayed.money) == set(original.money)
    assert replayed.money == approx_money(original.money), (
        "cumulative value or commission differs after replay"
    )

    assert set(replayed.balances) == set(original.balances), (
        "replay moved a different set of (trader, symbol) balances"
    )
    assert replayed.balances == approx_money(original.balances), (
        "a trader balance differs after replay"
    )

    assert replayed.last_price == original.last_price


def assert_workload_was_substantial(state, trades, counts):
    """Guard against a vacuous pass: two empty books also compare equal.

    Every clause here is a shape the comparison above is supposed to be
    testing -- depth on both sides, partial fills, cancellations of both kinds,
    modifications that traded, commissions charged, balances on both signs.
    """
    assert counts["trades"] > 200, counts
    assert len(trades) == counts["trades"]
    for key, entries in state.queues.items():
        assert entries, "no depth left on the %s %s side: %r" % (key + (counts,))

    orders = state.orders.values()
    assert sum(1 for o in orders if 0 < o.fulfilled < o.qty) > 20, "no partial fills"
    assert sum(1 for o in orders if o.cancel_reason == "requested") > 20
    assert sum(1 for o in orders if o.cancel_reason == "ioc_remainder") > 3
    assert sum(1 for o in orders if o.order_type == "market") > 50
    assert counts["reprice"] > 100 and counts["resize"] > 50, counts
    assert counts["modify_trades"] > 50, "no modification ever crossed"

    assert any(
        value > 0 for (_, field), value in state.money.items() if field == "value"
    )
    assert (
        sum(
            1
            for (_, field), charge in state.money.items()
            if field == "commission" and charge > 0
        )
        > 50
    ), "no commissions were charged"
    assert any(amount > 0 for amount in state.balances.values())
    assert any(amount < 0 for amount in state.balances.values())
    assert all(price is not None for price in state.last_price.values())


# --------------------------------------------------------------------------
# the tests
# --------------------------------------------------------------------------

SEEDS = (1, 7, 13, 42, 99)


@pytest.mark.parametrize("seed", SEEDS)
def test_replay_reaches_the_same_end_state(tmp_path, seed):
    """A recorded session, re-issued into a fresh engine, ends up identical."""
    db_path = tmp_path / ("session%d.db" % seed)
    book = build_recorded(db_path)
    trades, counts = run_workload(book, random.Random(seed))
    original = capture(book)
    assert_workload_was_substantial(original, trades, counts)
    book.close()

    replayed_book, replayed_trades = replayed_from(db_path)

    assert_same_end_state(original, capture(replayed_book))
    # The replayed engine derived these itself: no `Filled` was ever fed back
    # in, so an identical trade sequence is the matching re-running the same
    # way, not the log being copied.
    assert trade_log(replayed_trades) == trade_log(trades)


@pytest.mark.parametrize("seed", (5, 23))
def test_identifiers_after_replay_cannot_collide(tmp_path, seed):
    """`order-lifecycle`: identifiers stay unique across a reload (lob-7e7).

    The counter is re-seeded by the replay itself -- every `Accepted` is
    re-issued with its original identifier and each one pushes the high-water
    mark past itself -- so there is no restore step to forget. This proves the
    property that follows from it: nothing assigned after the reload names an
    order from before it.
    """
    db_path = tmp_path / ("session%d.db" % seed)
    book = build_recorded(db_path)
    run_workload(book, random.Random(seed), n_ops=600)
    before = {order.idNum for order in book.orders()}
    assert before
    book.close()

    replayed_book, _ = replayed_from(db_path)
    assert {order.idNum for order in replayed_book.orders()} == before

    for expected in range(max(before) + 1, max(before) + 21):
        assert replayed_book.order(expected) is None
        order, _ = replayed_book.submit(
            tid=1,
            instrument=SYMBOLS[0],
            side="bid",
            order_type="limit",
            qty=1,
            price=MID - 50,
        )
        assert order.idNum not in before, "a post-reload order reused an identifier"
        assert order.idNum == expected


def test_replay_re_emits_an_identical_stream(tmp_path):
    """Recording the replay produces the same stream, event for event.

    The strongest form of the determinism claim: not merely the same end state
    but the same transitions in the same order, including the `Filled` and
    IOC-`Cancelled` events that were never fed back in, and the `seq` numbers
    they were emitted with.
    """
    original_path = tmp_path / "original.db"
    book = build_recorded(original_path)
    run_workload(book, random.Random(2024), n_ops=800)
    book.close()

    replay_path = tmp_path / "replay.db"
    replayed_book, _ = replayed_from(original_path, sink=SQLiteSink(replay_path))
    replayed_book.close()

    original_stream = list(read_events(original_path))
    replayed_stream = list(read_events(replay_path))
    assert len(replayed_stream) == len(original_stream)
    for replayed_event, original_event in zip(replayed_stream, original_stream):
        assert replayed_event == original_event


# --------------------------------------------------------------------------
# what taking events rather than a path buys
# --------------------------------------------------------------------------


def test_replay_reconstructs_from_a_list_of_events_with_no_database():
    """The ratified scenario again, with no file and no `sqlite3` anywhere.

    `replay` takes an iterable of events, so a session recorded to a `ListSink`
    replays out of memory. The list still holds the fills -- nothing filtered
    it -- which is what makes this the test of `replay`'s own call to
    `events.is_replayable`: feed a `Filled` back in and the book double-books.
    """
    book, sink = build_listed()
    trades, counts = run_workload(book, random.Random(3), n_ops=800)
    original = capture(book)

    assert any(isinstance(event, Filled) for event in sink.events), (
        "the list holds engine output too, or this proves nothing about the filter"
    )
    assert counts["trades"] > 50 and counts["modify_trades"] > 10, counts

    replayed_book, replayed_trades = replay(sink.events)

    assert_same_end_state(original, capture(replayed_book))
    assert trade_log(replayed_trades) == trade_log(trades)


def test_the_replayable_column_and_the_filter_agree(tmp_path):
    """The sink's materialised filter and `is_replayable` are the same rule.

    `read_events(..., replayable_only=True)` applies the column the sink wrote
    at record time; `replay` applies `events.is_replayable` to whatever it is
    handed. Both come from the one function, so replaying the filtered read and
    the whole log must land in the same place -- and if they ever did not, one
    of the two would be a second copy of the rule.
    """
    db_path = tmp_path / "session.db"
    book = build_recorded(db_path)
    run_workload(book, random.Random(11), n_ops=600)
    book.close()

    whole = list(read_events(db_path))
    filtered = list(read_events(db_path, replayable_only=True))
    assert len(filtered) < len(whole), "the filter dropped nothing; nothing traded"
    assert filtered == [event for event in whole if is_replayable(event)]

    from_whole, trades_from_whole = replay(whole)
    from_filtered, trades_from_filtered = replay(filtered)

    assert_same_end_state(capture(from_whole), capture(from_filtered))
    assert trade_log(trades_from_filtered) == trade_log(trades_from_whole)


# --------------------------------------------------------------------------
# what replay refuses
# --------------------------------------------------------------------------


def recorded_events(n_ops=40, seed=17):
    """A short recorded session as a list of events. No file involved."""
    book, sink = build_listed()
    run_workload(book, random.Random(seed), n_ops=n_ops)
    return sink.events


def test_replay_refuses_a_stream_that_does_not_open_with_a_session():
    """Without `SessionStarted` there is no tick size to build an engine with.

    Dropping it is what slicing a log wrong looks like, and the failure it
    would otherwise cause is a book quantized to the default tick -- orders at
    plausible but different prices, and no exception anywhere.
    """
    events = recorded_events()
    with pytest.raises(ReplayError, match="not SessionStarted"):
        replay(events[1:])

    with pytest.raises(ReplayError, match="empty"):
        replay([])


def test_replay_refuses_two_sessions_concatenated():
    """Two logs appended together are two books, not a longer one."""
    with pytest.raises(ReplayError, match="second SessionStarted"):
        replay(recorded_events() + recorded_events(seed=18))


def test_replay_refuses_a_stream_version_it_does_not_implement():
    """`SessionStarted.stream_version`: refuse what this release cannot read.

    `read_events` refuses it too, from the recorded `session` row -- but a
    caller replaying a list of events never goes near that check, so the
    replayer keeps its own, as `events.SessionStarted` says it must.
    """
    events = recorded_events()
    with pytest.raises(ReplayError, match="stream_version"):
        replay([replace_field(events[0], stream_version=99)] + events[1:])


# --------------------------------------------------------------------------
# the import that must not happen
# --------------------------------------------------------------------------


def test_importing_PyLOB_does_not_import_sqlite3():
    """`PyLOB.__init__`, README: persistence is optional, so is its import.

    Checked in a subprocess because the claim is about a fresh interpreter:
    this one imported `sqlite3` at the top of three sink suites long before
    getting here. `replay` shipping inside the package is what makes the check
    worth having -- it is the first module in `PyLOB` whose whole purpose is
    reading a recorded session, and the composition with `read_events` rather
    than a `replay(path)` convenience is what keeps the promise true.
    """
    probe = (
        "import sys; import PyLOB; "
        "print(sorted(m for m in sys.modules if 'sqlite' in m))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", (
        "`import PyLOB` pulled in %s" % result.stdout.strip()
    )
