"""The emission set covers the public mutation set, and keeps covering it.

`recording-sink` says the persisted stream must be "sufficient to reconstruct
the book state and reporting values (including last-trade price)". That is a
claim about the *public surface*, not about the four lifecycle events: it holds
only while every public call that changes engine state leaves something behind
that a replayer can re-issue. lob-9fu is what it looks like when it stops
holding, and this module has two jobs -- pin the three ways it stopped, and
catch the fourth before it ships.

What went wrong (all three verified against the tree before the fix)
--------------------------------------------------------------------

**A currency could be withdrawn in silence.** `configure_instrument(sym, None)`
assigned the currency unconditionally and emitted only when the currency was
not `None`, so the withdrawal left no event at all. The engine then settled the
instrument leg alone while a sink -- still holding the currency it was last
told about -- went on booking the cash leg of every trade, and the two ledgers
walked apart with nothing in the log to say why. Replay reproduced the sink's
numbers, because replay reads the same log the sink did.

**`setLastPrice` overwrote a reporting value unrecorded.** `book-queries`
defines it as the price of the most recent trade and requires a reload to agree
with the persisted record; the setter made the engine report 999.0 while its
own log said the last real trade, and the replay landed on the log's answer.

**The public decomposition of `submit` is not replay-coherent.**
`create_order` emits `Accepted` and stops -- it neither matches nor rests --
while every replay driver turns an `Accepted` into a whole `submit`. A session
that accepted a crossing bid and went no further therefore replays as one that
traded: the replay *invents* a trade. And `match` would trade for any `Order`
object it was handed, including one the engine had never accepted: real fills
against real resting liquidity, four balances moved, for an order `order()`
could not find and no `Accepted` described.

What is fixed here, and what is recorded rather than fixed
-----------------------------------------------------------

The first two are fixed by shrinking the mutation set: both calls now raise,
because neither mutation can be expressed by any event this stream has, and
adding an event kind is a change to the wire format and to every sink that
folds it -- a maintainer's decision, not a bug fix (`engine.configure_
instrument`, `engine.setLastPrice`).

`match` and `rest` are fixed by guarding: an order the engine never accepted is
refused by both. That removes the live-divergence path without removing a
public name.

`create_order`'s incoherence is *recorded*, not fixed. Making it coherent means
either changing what `Accepted` means or making the method private, and both
change the public API, which `openspec/config.yaml` reserves to an ADR. So its
divergence is pinned by a test that asserts today's behaviour and by a strict
`xfail` that will turn red the day someone fixes it.

The drift test
--------------

`test_every_public_method_is_classified` is the part worth more than the three
fixes. It walks `OrderBook`'s public surface, calls each member on a fresh
engine, and *derives* what each one did -- raised, emitted, changed state -- by
observation. The declared table below has to agree, so:

    a new public method                fails until it is classified
    a mutation that stops emitting     fails: declared EMITS, observed
                                       UNRECORDED
    a query that starts mutating       fails: declared QUERY, observed
                                       UNRECORDED
    a refusal that half-applies        fails: REFUSED requires that nothing
                                       moved and nothing was emitted

That is the whole class of defect lob-9fu belongs to, not merely its three
instances. Verified by regression: reverting `setLastPrice` to its assignment
fails it on `setLastPrice`, and dropping the emission out of `_cancel` fails it
on `cancelOrder`, each naming the member.

Two limits, both deliberate. It calls each member *once*, in one representative
shape, so a member that emits for one argument and not for another is only as
covered as its recipe -- which is exactly what the currency withdrawal was, and
why the withdrawal has tests of its own above. And it reads a few private
counters (`_next_priority` and friends): they are the drift surface -- a stamp
taken and not recorded is precisely what `next_priority` does wrong -- and
there is no public way to observe them that does not itself mutate.
"""

from __future__ import annotations

import sqlite3

import pytest
from PyLOB.engine import InvalidOrder, Order, OrderBook, PyLOBError, UnknownOrder
from PyLOB.events import OrderType, SessionStarted, Side
from PyLOB.sinks.sqlite import SQLiteSink, read_events

# The replay suite is a sibling test module (`tests/` has no `__init__.py`, so
# pytest puts it on `sys.path`), and `tests/test_sink_equality.py` already
# borrows from it. The round-trip test below reuses its capture and its
# comparison rather than growing a second `EndState` to keep correct.
# `replayed_from` is its two-line composition of `read_events` with the shipped
# `PyLOB.replay`; the replaying itself is not this suite's, nor that suite's.
import test_replay as replay_suite

TICK = 0.01
INSTRUMENT = "FAKE"
CURRENCY = "USD"


class ListSink:
    """Satisfies `events.EventSink` and keeps what it is given.

    No encoding and no I/O: these tests ask what the engine emitted, and a
    `SQLiteSink` would answer a question about SQLite as well.
    """

    def __init__(self):
        self.events = []

    def consume(self, event, /):
        self.events.append(event)


def build(sink=None, tick=TICK):
    """A recording engine with one instrument and three traders declared."""
    book = OrderBook(tick_size=tick, sink=sink)
    book.configure_instrument(INSTRUMENT, CURRENCY)
    for tid in (1, 2, 3):
        book.configure_trader(tid, name="t%d" % tid, commission_min=1.0)
    return book


def rest_ask(book, tid=1, qty=5, price=100.0):
    """One resting ask, submitted the sanctioned way."""
    order, _ = book.submit(
        tid=tid,
        instrument=INSTRUMENT,
        side="ask",
        order_type="limit",
        qty=qty,
        price=price,
    )
    return order


# --------------------------------------------------------------------------
# (a) a currency cannot be withdrawn in silence
# --------------------------------------------------------------------------


@pytest.mark.parametrize("withdrawal", [None, ""])
def test_withdrawing_a_currency_is_refused(withdrawal):
    """The unrecordable mutation raises, and leaves nothing half-applied."""
    sink = ListSink()
    book = build(sink)
    before = len(sink.events)

    with pytest.raises(InvalidOrder) as raised:
        book.configure_instrument(INSTRUMENT, withdrawal)

    assert "currency" in str(raised.value)
    assert book.book(INSTRUMENT).currency == CURRENCY, (
        "the refusal must not have applied the assignment it refused"
    )
    assert sink.events[before:] == [], "a refused call emitted an event"


def test_a_withdrawn_currency_used_to_split_the_two_ledgers(tmp_path):
    """The lob-9fu repro: engine ledger vs the sink's, across a withdrawal.

    Before the fix this session emitted nothing for the withdrawal, after
    which the engine booked no currency leg at all while the sink went on
    booking one from the currency it still held -- verified at 1005.0 in the
    sink against 0.0 in the engine. The withdrawal is refused now, so the two
    ledgers are compared directly: the sink's `balance` table is the fold of
    the stream, and the engine's `holdings()` is what actually happened.
    """
    path = tmp_path / "ledgers.db"
    book = build(SQLiteSink(path))

    with pytest.raises(InvalidOrder):
        book.configure_instrument(INSTRUMENT, None)

    for i in range(2):
        rest_ask(book, tid=1, qty=5, price=100.0 + i)
        book.submit(
            tid=2,
            instrument=INSTRUMENT,
            side="bid",
            order_type="limit",
            qty=5,
            price=100.0 + i,
        )
    book.close()

    engine_ledger = {(tid, symbol): amount for tid, symbol, amount in book.holdings()}
    conn = sqlite3.connect(path)
    try:
        sink_ledger = {
            (tid, symbol): amount
            for tid, symbol, amount in conn.execute(
                "SELECT tid, symbol, amount FROM balance"
            )
        }
    finally:
        conn.close()

    assert engine_ledger[1, CURRENCY] != 0.0, "the session moved no cash at all"
    assert sink_ledger == pytest.approx(engine_ledger, rel=1e-9, abs=1e-9)


def test_a_currency_may_still_be_changed_and_is_recorded():
    """Refusing a withdrawal is not refusing a reconfiguration."""
    sink = ListSink()
    book = build(sink)
    book.configure_instrument(INSTRUMENT, "EUR")

    assert book.book(INSTRUMENT).currency == "EUR"
    assert sink.events[-1].KIND == "instrument_configured"
    assert sink.events[-1].currency == "EUR"


# --------------------------------------------------------------------------
# (b) the last-trade price is engine output
# --------------------------------------------------------------------------


def test_setting_the_last_price_is_refused():
    """It raises, emits nothing, and leaves the reported value alone."""
    sink = ListSink()
    book = build(sink)
    rest_ask(book, tid=1, qty=5, price=100.0)
    book.submit(
        tid=2,
        instrument=INSTRUMENT,
        side="bid",
        order_type="limit",
        qty=5,
        price=100.0,
    )
    traded_at = book.getLastPrice(INSTRUMENT)
    before = len(sink.events)

    with pytest.raises(InvalidOrder):
        book.setLastPrice(INSTRUMENT, 999.0)

    assert traded_at == 100.0
    assert book.getLastPrice(INSTRUMENT) == traded_at, (
        "the refusal must not have applied the assignment it refused"
    )
    assert sink.events[before:] == []


def test_the_last_price_a_replay_reports_is_the_one_the_engine_reports(tmp_path):
    """The lob-9fu repro: engine 999.0, sink NULL, replay somewhere else.

    The only path left to the value is an execution, and an execution emits,
    so the log is sufficient for it -- which is what `book-queries` ("after
    reloading persisted state the reported value SHALL equal the last trade in
    the persisted record") and `recording-sink` both require.
    """
    path = tmp_path / "lastprice.db"
    book = build(SQLiteSink(path), tick=replay_suite.TICK)
    rest_ask(book, tid=1, qty=5, price=100.0)
    book.submit(
        tid=2,
        instrument=INSTRUMENT,
        side="bid",
        order_type="limit",
        qty=5,
        price=100.0,
    )
    with pytest.raises(InvalidOrder):
        book.setLastPrice(INSTRUMENT, 999.0)
    book.close()

    replayed, _ = replay_suite.replayed_from(path)
    assert replayed.getLastPrice(INSTRUMENT) == book.getLastPrice(INSTRUMENT) == 100.0


# --------------------------------------------------------------------------
# (c) the public decomposition of `submit`
# --------------------------------------------------------------------------


def create_order_only_session(path):
    """Accept a crossing bid and stop: `create_order` with no `match`.

    Returns the engine, closed, having emitted `Accepted` for an order that
    neither traded nor rested.
    """
    book = build(SQLiteSink(path), tick=replay_suite.TICK)
    rest_ask(book, tid=1, qty=5, price=100.0)
    book.create_order(
        tid=2,
        instrument=INSTRUMENT,
        side="bid",
        order_type="limit",
        qty=5,
        price=101.0,
    )
    book.close()
    return book


def test_create_order_accepts_without_matching_or_resting():
    """What the method does, stated once so the tests below mean something."""
    book = build(ListSink())
    ask = rest_ask(book, tid=1, qty=5, price=100.0)
    bid = book.create_order(
        tid=2,
        instrument=INSTRUMENT,
        side="bid",
        order_type="limit",
        qty=5,
        price=101.0,
    )

    assert book.order(bid.idNum) is bid, "it registers the order"
    assert bid.fulfilled == 0, "it does not match"
    assert book.snapshot(INSTRUMENT, "bid") == (), "it does not rest"
    assert book.snapshot(INSTRUMENT, "ask") == (ask,), "the crossing ask is untouched"


def test_a_create_order_only_session_replays_as_a_full_submission(tmp_path):
    """The lob-9fu repro, pinned: the replay invents a trade (not fixed).

    `Accepted` is the record of a submission and a replayer re-issues it as
    one. A caller that accepted an order and stopped therefore wrote a log of
    a session it did not run. This asserts the divergence rather than the
    absence of it, because closing it means changing the public API -- see
    the module docstring and `engine.create_order`.
    """
    path = tmp_path / "createonly.db"
    original = create_order_only_session(path)

    assert original.getLastPrice(INSTRUMENT) is None, "the original never traded"
    assert [order.idNum for order in original.snapshot(INSTRUMENT, "ask")] == [1]

    replayed, trades = replay_suite.replayed_from(path)

    assert len(trades) == 1, "the replay was expected to invent exactly one trade"
    assert replayed.getLastPrice(INSTRUMENT) == 100.0
    assert replayed.snapshot(INSTRUMENT, "ask") == (), (
        "the replay drained a book the original left resting"
    )


@pytest.mark.xfail(
    strict=True,
    reason="lob-9fu: create_order emits Accepted and stops, and a replayer "
    "re-issues an Accepted as a whole submit. Fixing it changes the public "
    "API (privatize, or split the event), so it is a maintainer's decision. "
    "This turns red the day it is fixed.",
)
def test_a_create_order_only_session_round_trips(tmp_path):
    """The property that ought to hold for every public path, and does not."""
    path = tmp_path / "createonly.db"
    original = create_order_only_session(path)
    replayed, _ = replay_suite.replayed_from(path)

    assert [order.idNum for order in replayed.snapshot(INSTRUMENT, "ask")] == [
        order.idNum for order in original.snapshot(INSTRUMENT, "ask")
    ]


def ghost_order(idNum=999, tid=2, price=101.0, qty=5):
    """An `Order` the engine never accepted, built the way lob-9fu built it."""
    return Order(
        idNum=idNum,
        tid=tid,
        instrument=INSTRUMENT,
        side=Side.BID,
        order_type=OrderType.LIMIT,
        price=price,
        qty=qty,
        timestamp=0.0,
        priority=idNum,
    )


def test_match_refuses_an_order_the_engine_never_accepted():
    """The lob-9fu repro: a ghost order traded, and `order(999)` was None."""
    sink = ListSink()
    book = build(sink)
    ask = rest_ask(book, tid=1, qty=5, price=100.0)
    before = len(sink.events)

    with pytest.raises(UnknownOrder):
        book.match(ghost_order())

    assert book.order(999) is None
    assert ask.fulfilled == 0, "a refused match filled a resting order"
    assert book.snapshot(INSTRUMENT, "ask") == (ask,)
    assert book.getLastPrice(INSTRUMENT) is None
    assert list(book.holdings()) == [], "a refused match moved a balance"
    assert sink.events[before:] == [], "a refused match emitted an event"


def test_match_refuses_an_impostor_carrying_a_live_identifier():
    """Membership is not enough: the check is identity.

    A second `Order` object answering to an identifier the engine has issued
    is exactly as unaccounted-for as one with an identifier nobody has, and
    it is the harder case -- `idNum in _orders` would wave it through.
    """
    book = build(ListSink())
    real = rest_ask(book, tid=1, qty=5, price=100.0)
    impostor = ghost_order(idNum=real.idNum, tid=2, price=101.0)
    assert book.order(real.idNum) is real

    with pytest.raises(UnknownOrder):
        book.match(impostor)

    assert real.fulfilled == 0
    assert book.snapshot(INSTRUMENT, "ask") == (real,)


def test_rest_refuses_an_order_the_engine_never_accepted():
    """The same hole on the passive side: liquidity no event describes."""
    sink = ListSink()
    book = build(sink)
    before = len(sink.events)

    with pytest.raises(UnknownOrder):
        book.rest(ghost_order())

    assert book.snapshot(INSTRUMENT, "bid") == ()
    assert book.getVolumeAtPrice(INSTRUMENT, "bid", 101.0) == 0
    assert sink.events[before:] == []


def test_the_guards_leave_the_sanctioned_paths_alone():
    """`submit` and a repricing `modifyOrder` both match through the guard."""
    book = build(ListSink())
    rest_ask(book, tid=1, qty=5, price=100.0)
    resting, trades = book.submit(
        tid=2,
        instrument=INSTRUMENT,
        side="bid",
        order_type="limit",
        qty=8,
        price=99.0,
    )
    assert trades == [] and resting.resting

    made, _ = book.modifyOrder(resting.idNum, dict(side="bid", qty=None, price=100.0))
    assert len(made) == 1, "a repriced order still crosses as a taker"
    assert resting.fulfilled == 5


# --------------------------------------------------------------------------
# the drift test: every public member, classified by observation
# --------------------------------------------------------------------------

#: Changes state and emits at least one event. The stream carries it.
EMITS = "emits"
#: Changes nothing and emits nothing. A read.
QUERY = "query"
#: Changes state and emits nothing. Legitimate only with a reason, and every
#: reason is written out in `SURFACE` below.
UNRECORDED = "unrecorded"
#: Raises, having changed nothing and emitted nothing.
REFUSED = "refused"
#: Emits without changing state: `emit` itself, and nothing else.
INJECTS = "injects"


def fingerprint(book):
    """Everything a public call could change, in comparable form.

    Deliberately wider than `test_replay.capture`: that compares two sessions
    and so looks at what a session *means*, while this has to notice a single
    call touching anything at all -- including the three counters, which are
    invisible until the order they stamp shows up somewhere.
    """
    symbols = sorted(book.instruments())
    return (
        tuple(symbols),
        tuple(
            (
                symbol,
                book.book(symbol).currency,
                book.getLastPrice(symbol),
                tuple(
                    (order.idNum, order.price, order.remaining, order.priority)
                    for side in ("bid", "ask")
                    for order in book.snapshot(symbol, side)
                ),
            )
            for symbol in symbols
        ),
        tuple(
            sorted(
                (
                    order.idNum,
                    order.tid,
                    str(order.side),
                    str(order.order_type),
                    order.price,
                    order.qty,
                    order.fulfilled,
                    order.value,
                    order.commission,
                    order.cancelled,
                    str(order.cancel_reason),
                    order.priority,
                )
                for order in book.orders()
            )
        ),
        tuple(sorted(book.holdings())),
        tuple(
            sorted(
                (
                    trader.tid,
                    trader.name,
                    trader.allow_self_matching,
                    trader.commission_min,
                    trader.commission_max_percnt,
                    trader.commission_per_unit,
                )
                # No public reader: `trader()` creates what it cannot find.
                for trader in book._traders.values()
            )
        ),
        book.time,
        book._next_idNum,
        book._next_priority,
        book._next_trade_id,
        book._next_seq,
    )


def scenario():
    """A recording engine with depth on both sides, a trade, and a cancel.

    Every recipe below gets its own, so one recipe cannot set another up.
    """
    sink = ListSink()
    book = build(sink)
    rest_ask(book, tid=1, qty=5, price=100.0)
    rest_ask(book, tid=1, qty=5, price=100.5)
    book.submit(
        tid=2,
        instrument=INSTRUMENT,
        side="bid",
        order_type="limit",
        qty=4,
        price=99.0,
    )
    doomed, _ = book.submit(
        tid=3,
        instrument=INSTRUMENT,
        side="bid",
        order_type="limit",
        qty=3,
        price=98.5,
    )
    book.submit(
        tid=3,
        instrument=INSTRUMENT,
        side="bid",
        order_type="limit",
        qty=2,
        price=100.0,
    )
    book.cancelOrder("bid", doomed.idNum)
    return book, sink


def _accepted(book, side="bid", price=101.0, qty=2, tid=2):
    """An accepted-but-unprocessed order, for the recipes that need one."""
    return book.create_order(
        tid=tid,
        instrument=INSTRUMENT,
        side=side,
        order_type="limit",
        qty=qty,
        price=price,
    )


#: name -> (expected class, why). One entry per public member of `OrderBook`;
#: the test fails if the surface and this table stop agreeing, which is what
#: makes a newly added public method someone's problem before it ships.
SURFACE = {
    # -- configuration: recorded, and re-applied by a replay as configuration
    "configure_instrument": (EMITS, "InstrumentConfigured"),
    "configure_trader": (EMITS, "TraderConfigured"),
    # -- operations: recorded, and re-issued by a replay as the same call
    "submit": (EMITS, "Accepted, then Filled per execution, then any IOC cancel"),
    "processOrder": (EMITS, "submit in the legacy dict shape"),
    "cancelOrder": (EMITS, "Cancelled(REQUESTED)"),
    "modifyOrder": (EMITS, "Modified, then any Filled the new price causes"),
    "create_order": (EMITS, "Accepted -- but see NOT_REPLAY_COHERENT"),
    "match": (EMITS, "Filled per execution"),
    # -- refusals: mutations no event can express (lob-9fu)
    "setLastPrice": (
        REFUSED,
        "the last-trade price is engine output; overwriting it reports a "
        "price no trade made and no event records",
    ),
    # -- unrecorded mutations, each with its reason
    "rest": (
        UNRECORDED,
        "resting is the absence of a transition; the Accepted of the order "
        "that got here is what records it, and a replayer re-issues that as a "
        "whole submit",
    ),
    "trader": (
        UNRECORDED,
        "registers the default schedule for a trader nobody configured. "
        "Benign: the default is deterministic and a replay re-creates exactly "
        "the same one, which is why no TraderConfigured is owed for it",
    ),
    "next_priority": (UNRECORDED, "a stamp taken outside an order's acceptance"),
    "next_trade_id": (UNRECORDED, "an identifier taken outside an execution"),
    "next_seq": (UNRECORDED, "a stream position taken without an event to fill it"),
    # -- the sink seam
    "emit": (INJECTS, "hands an event over; it records, it does not act"),
    "close": (QUERY, "flushes the sink; the engine's own state is untouched"),
    "recording": (QUERY, "is a sink attached"),
    # -- reads
    "quantize": (QUERY, "the tick grid applied to a price"),
    "clipPrice": (QUERY, "quantize under its legacy name"),
    "order": (QUERY, "the store, by identifier"),
    "require_order": (QUERY, "the store, by identifier, raising when absent"),
    "orders": (QUERY, "every order accepted"),
    "book": (
        QUERY,
        "the book for an instrument. Asked for a symbol it has never seen it "
        "creates an empty one, which is a mutation and is lob-kbx's, not this "
        "bead's; the recipe asks for the instrument the scenario configured",
    ),
    "instruments": (QUERY, "every instrument with a book"),
    "snapshot": (QUERY, "resting orders in matching priority order"),
    "getBestBid": (QUERY, "book-queries"),
    "getWorstBid": (QUERY, "book-queries"),
    "getBestAsk": (QUERY, "book-queries"),
    "getWorstAsk": (QUERY, "book-queries"),
    "getVolumeAtPrice": (QUERY, "book-queries"),
    "getLastPrice": (QUERY, "book-queries"),
    "balance": (QUERY, "trader-balances"),
    "holdings": (QUERY, "trader-balances"),
    "print": (QUERY, "the book as text, for eyeballs"),
}

#: The public members that are not a self-contained, re-issuable transition.
#: Each one's docstring has to say so, in those words, because the docstring
#: is what a caller reads before reaching for it.
NOT_REPLAY_COHERENT = frozenset(
    {
        "create_order",
        "rest",
        "match",
        "emit",
        "next_priority",
        "next_trade_id",
        "next_seq",
    }
)

#: Public members that are not callables: constants, and the two instance
#: attributes the engine exposes. Listed so that adding one is noticed --
#: a public attribute is assigned, not called, and no guard can catch it.
CONSTANTS = frozenset({"valid_types", "valid_sides"})
PUBLIC_ATTRIBUTES = frozenset({"tick_size", "time"})


def _match_recipe(book):
    """`match` needs an accepted taker; accepting it is not what is measured."""
    taker = _accepted(book)
    return lambda: book.match(taker)


def _rest_recipe(book):
    """Same for `rest`, with a price that will not cross."""
    order = _accepted(book, price=97.0)
    return lambda: book.rest(order)


def _emit_recipe(book):
    """An event to hand to `emit`, built the way the engine builds one.

    `seq=-1` because this event is not part of the stream and must not look
    as though it were: `emit` is the one member that records without acting.
    """
    event = SessionStarted(seq=-1, timestamp=book.time, tick_size=book.tick_size)
    return lambda: book.emit(event)


#: name -> a callable that performs the member's own setup and returns the
#: one call to be measured. Nothing outside the returned callable is observed,
#: so a recipe may accept, rest and cancel as much as it needs to first.
RECIPES = {
    "configure_instrument": lambda b: lambda: b.configure_instrument(INSTRUMENT, "GBP"),
    "configure_trader": lambda b: lambda: b.configure_trader(
        1, name="renamed", commission_min=7.0
    ),
    "submit": lambda b: lambda: b.submit(
        tid=2,
        instrument=INSTRUMENT,
        side="bid",
        order_type="limit",
        qty=3,
        price=100.0,
    ),
    "processOrder": lambda b: lambda: b.processOrder(
        dict(tid=2, instrument=INSTRUMENT, side="bid", type="limit", qty=3, price=100.0)
    ),
    "cancelOrder": lambda b: lambda: b.cancelOrder(
        "bid", b.snapshot(INSTRUMENT, "bid")[0].idNum
    ),
    "modifyOrder": lambda b: lambda: b.modifyOrder(
        b.snapshot(INSTRUMENT, "bid")[0].idNum,
        dict(side="bid", qty=None, price=100.0),
    ),
    "create_order": lambda b: lambda: _accepted(b),
    "match": _match_recipe,
    "setLastPrice": lambda b: lambda: b.setLastPrice(INSTRUMENT, 999.0),
    "rest": _rest_recipe,
    "trader": lambda b: lambda: b.trader(4242),
    "next_priority": lambda b: b.next_priority,
    "next_trade_id": lambda b: b.next_trade_id,
    "next_seq": lambda b: b.next_seq,
    "emit": _emit_recipe,
    "close": lambda b: b.close,
    "recording": lambda b: lambda: b.recording,
    "clipPrice": lambda b: lambda: b.clipPrice(100.017),
    "quantize": lambda b: lambda: b.quantize(100.017),
    "order": lambda b: lambda: b.order(1),
    "require_order": lambda b: lambda: b.require_order(1),
    "orders": lambda b: lambda: list(b.orders()),
    "book": lambda b: lambda: b.book(INSTRUMENT),
    "instruments": lambda b: lambda: list(b.instruments()),
    "snapshot": lambda b: lambda: b.snapshot(INSTRUMENT, "bid"),
    "getBestBid": lambda b: lambda: b.getBestBid(INSTRUMENT),
    "getWorstBid": lambda b: lambda: b.getWorstBid(INSTRUMENT),
    "getBestAsk": lambda b: lambda: b.getBestAsk(INSTRUMENT),
    "getWorstAsk": lambda b: lambda: b.getWorstAsk(INSTRUMENT),
    "getVolumeAtPrice": lambda b: lambda: b.getVolumeAtPrice(INSTRUMENT, "bid", 99.0),
    "getLastPrice": lambda b: lambda: b.getLastPrice(INSTRUMENT),
    "balance": lambda b: lambda: b.balance(1, CURRENCY),
    "holdings": lambda b: lambda: list(b.holdings()),
    "print": lambda b: lambda: b.print(INSTRUMENT),
}


def observe(name):
    """Run one recipe on a fresh engine and classify what it did."""
    book, sink = scenario()
    call = RECIPES[name](book)
    before = fingerprint(book)
    emitted = len(sink.events)
    raised = None
    try:
        call()
    except PyLOBError as exc:
        raised = exc
    after = fingerprint(book)
    events = sink.events[emitted:]
    changed = after != before

    if raised is not None:
        assert not events and not changed, (
            "%s raised %r after emitting %d event(s) and %s state: a refusal "
            "has to leave the engine as it found it"
            % (name, raised, len(events), "changing" if changed else "not changing")
        )
        return REFUSED
    if events and changed:
        return EMITS
    if events:
        return INJECTS
    if changed:
        return UNRECORDED
    return QUERY


def public_members():
    """Every public name `OrderBook` defines, from the class body.

    `vars` rather than `dir`, so `object`'s members stay out of it and a name
    inherited from nowhere cannot be missed.
    """
    return {name for name in vars(OrderBook) if not name.startswith("_")}


def test_the_recipe_table_covers_the_public_surface():
    """A new public member has no recipe, so it fails here first."""
    callables = public_members() - CONSTANTS
    assert callables == set(RECIPES), (
        "public members without a recipe: %r; recipes for members that no "
        "longer exist: %r"
        % (sorted(callables - set(RECIPES)), sorted(set(RECIPES) - callables))
    )
    assert set(SURFACE) == set(RECIPES), (
        "unclassified: %r; classified but unreachable: %r"
        % (sorted(set(RECIPES) - set(SURFACE)), sorted(set(SURFACE) - set(RECIPES)))
    )
    assert public_members() & CONSTANTS == CONSTANTS, (
        "a public class constant this test tracks has gone: %r"
        % sorted(CONSTANTS - public_members())
    )
    attributes = {name for name in vars(build()) if not name.startswith("_")}
    assert attributes == PUBLIC_ATTRIBUTES, (
        "the public instance attributes changed: %r. A public attribute is "
        "assigned rather than called, so no guard can catch a caller writing "
        "to one -- adding one is a decision, not an implementation detail."
        % sorted(attributes ^ PUBLIC_ATTRIBUTES)
    )


@pytest.mark.parametrize("name", sorted(SURFACE))
def test_every_public_method_is_classified(name):
    """What each member does, observed, against what this file declares.

    This is the drift catcher. A mutation that stops emitting, a query that
    starts mutating, and a refusal that half-applies each fail here with the
    member named -- which is the whole class lob-9fu belongs to, rather than
    its three instances.
    """
    expected, reason = SURFACE[name]
    assert reason, "every classification needs a reason"
    assert observe(name) == expected, (
        "OrderBook.%s is classified %r (%s) but does not behave that way"
        % (name, expected, reason)
    )


def test_unrecorded_members_say_so_in_their_docstrings():
    """The classification above is a test file; a caller reads the docstring."""
    for name in sorted(NOT_REPLAY_COHERENT):
        doc = getattr(OrderBook, name).__doc__ or ""
        assert "Not replay-coherent" in doc, (
            "OrderBook.%s changes state a replay cannot re-issue and its "
            "docstring does not say so" % name
        )
    for name in sorted(set(SURFACE) - NOT_REPLAY_COHERENT):
        member = getattr(OrderBook, name)
        doc = (
            member.__doc__ if not isinstance(member, property) else member.fget.__doc__
        ) or ""
        assert "Not replay-coherent" not in doc, (
            "OrderBook.%s carries the marker but is not listed as one" % name
        )


def test_a_refused_member_explains_itself():
    """`setLastPrice` is a name kept in order to say why, so it has to."""
    for name, (kind, _) in SURFACE.items():
        if kind != REFUSED:
            continue
        doc = getattr(OrderBook, name).__doc__ or ""
        assert "Refused" in doc, "OrderBook.%s raises and does not say why" % name


# --------------------------------------------------------------------------
# round-trip evidence, and the sinkless path
# --------------------------------------------------------------------------


def exercise(book):
    """A session over the paths lob-9fu found, refusals and all.

    Instruments and traders are the replay suite's, so its `capture` and its
    comparison apply unchanged. The refusals are part of the session on
    purpose: a refused call that perturbed the engine or the stream would show
    up as a replay divergence here, which is the property `observe` asserts
    per call and this one asserts end to end.
    """
    symbol, other = replay_suite.SYMBOLS
    orders = []
    for i in range(3):
        order, _ = book.submit(
            tid=1,
            instrument=symbol,
            side="ask",
            order_type="limit",
            qty=5,
            price=100.0 + i,
        )
        orders.append(order)
    book.submit(
        tid=2, instrument=symbol, side="bid", order_type="limit", qty=7, price=100.5
    )

    # The currency change is recorded, so a replay re-denominates where this
    # session did. The withdrawal is not a currency change and is refused.
    book.configure_instrument(symbol, "CHF")
    with pytest.raises(InvalidOrder):
        book.configure_instrument(symbol, None)
    with pytest.raises(InvalidOrder):
        book.setLastPrice(symbol, 999.0)
    with pytest.raises(UnknownOrder):
        book.match(ghost_order())
    with pytest.raises(UnknownOrder):
        book.rest(ghost_order())

    book.submit(
        tid=3, instrument=symbol, side="bid", order_type="limit", qty=6, price=101.0
    )
    book.submit(
        tid=2, instrument=other, side="bid", order_type="limit", qty=4, price=50.0
    )
    # Partly filled, and its remainder cancelled by the engine: an IOC cancel
    # is output, so the replay has to re-derive it rather than re-issue it.
    book.submit(tid=4, instrument=other, side="ask", order_type="market", qty=6)
    book.submit(
        tid=4, instrument=other, side="ask", order_type="limit", qty=3, price=51.0
    )
    # A repricing modify: the order leaves the book, crosses as a taker, and
    # what survives goes back in.
    book.modifyOrder(orders[-1].idNum, dict(side="ask", qty=None, price=99.0))
    doomed, _ = book.submit(
        tid=5, instrument=symbol, side="bid", order_type="limit", qty=3, price=98.0
    )
    book.cancelOrder(None, doomed.idNum)
    book.submit(
        tid=5, instrument=symbol, side="bid", order_type="limit", qty=4, price=98.5
    )
    return book


def test_the_previously_unrecorded_paths_round_trip(tmp_path):
    """A session over the lob-9fu paths replays to the same end state.

    `test_replay.capture` and `assert_same_end_state` are the established
    comparison -- the book queue by queue, every order's accounting, the whole
    ledger, and each instrument's last-trade price.
    """
    path = tmp_path / "roundtrip.db"
    book = exercise(replay_suite.build_recorded(path))
    original = replay_suite.capture(book)
    book.close()

    # No hole in the stream: a refused call that had taken a `seq` would show
    # up here (and `read_events` would refuse the log outright).
    seqs = [event.seq for event in read_events(path)]
    assert seqs == list(range(len(seqs)))

    replayed, trades = replay_suite.replayed_from(path)

    replay_suite.assert_same_end_state(original, replay_suite.capture(replayed))
    assert trades, "a round trip over an empty session proves nothing"
    assert any(price is not None for price in original.last_price.values())
    assert original.balances, "the session moved no balances"
    # Both currencies are in the ledger: the reconfiguration was recorded and
    # replayed at the same point in the stream, not folded into one currency.
    assert {"USD", "CHF"} <= {symbol for _, symbol in original.balances}


def test_the_refusals_and_guards_build_no_event_when_sinkless():
    """ADR-0002: a sinkless engine constructs no event, refusals included.

    Same tripwire as `test_sink_equality.test_a_sinkless_engine_builds_no_
    event`: `OrderBook` has no `__slots__`, so assigning `emit` shadows the
    bound method for this instance and every internal `self.emit(...)` finds
    it. `SessionStarted` is built in the constructor, before the tripwire goes
    on, and is the one event a sinkless engine ever builds.
    """
    book = OrderBook(tick_size=replay_suite.TICK, sink=None)
    assert not book.recording
    built = []
    book.emit = built.append
    for symbol, currency in replay_suite.INSTRUMENTS:
        book.configure_instrument(symbol, currency)
    for tid in (1, 2, 3, 4, 5):
        book.configure_trader(tid, name="t%d" % tid, commission_min=1.0)
    exercise(book)

    assert built == [], (
        "a sinkless engine constructed %d events; ADR-0002's throughput "
        "figure assumes it constructs none" % len(built)
    )
