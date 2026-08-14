"""Replay: a recorded event stream re-issued into a fresh engine.

`recording-sink` ratifies the promise this module keeps -- "WHEN a persisted
session's events are replayed into a fresh engine, THEN the reconstructed book
snapshot and last-trade price equal the original session's end state" -- and
`events` (the "Replay" section) states the rule that makes it true. This is
that rule, executable, in one place.

**Commands in, consequences re-derived.** `events.is_replayable` is the filter
and the only filter: configuration events and the commands a caller made
(`Accepted`, `Modified`, a `REQUESTED` `Cancelled`) are re-issued; `Filled`
and an IOC remainder's `Cancelled` are engine *output*, and feeding them back
would double-book. The replayed engine produces them again for itself, which
is what makes a replay a check on determinism rather than a restore.

**Events in, not a path.** `replay` takes any iterable of events, so it drags
in no database. A caller holding a recorded `.db` composes it with the sink's
reader; a caller holding a `ListSink`'s list passes the list::

    from PyLOB import replay
    from PyLOB.sinks.sqlite import read_events

    book, trades = replay(read_events("session.db"))

That composition is the reason this module lives beside the engine rather than
under `PyLOB.sinks`: `PyLOB.sinks` imports `sqlite3` at import time, and
`import PyLOB` must not (`PyLOB.__init__`; ADR-0001, ADR-0002). Replay needs
the engine and the vocabulary, and nothing else.

**Nothing here is a second decoder.** The events arrive already decoded --
`sinks.sqlite.decode_event` is the one decoder -- and the replayability rule is
called, not copied. Applying `is_replayable` here rather than relying on the
caller is what lets an unfiltered stream (a `ListSink`'s, which holds the fills
too) and a filtered one (`read_events(..., replayable_only=True)`, which
applies the column the sink materialised from the same function) both be
correct inputs.
"""

from __future__ import annotations

from collections.abc import Iterable

from .engine import OrderBook, PyLOBError, Trade
from .events import (
    STREAM_VERSION,
    Accepted,
    Cancelled,
    Event,
    EventSink,
    InstrumentConfigured,
    Modified,
    SessionStarted,
    TraderConfigured,
    is_replayable,
)

__all__ = ["replay", "ReplayError"]


class ReplayError(PyLOBError, ValueError):
    """The stream cannot be replayed: it is not one session this version reads.

    Raised before any engine call, so a refused replay leaves no half-built
    book behind. It is about the *stream* -- no opening `SessionStarted`, a
    second one, a `stream_version` this release does not implement. An order
    the engine itself refuses still raises `InvalidOrder` and means something
    else entirely: that the log and the engine disagree about matching.

    `sinks.sqlite.EventLogError` is the file-level counterpart -- that the
    recording is damaged or unfinished. This one is about a stream that read
    back perfectly and still is not a session.
    """


def replay(
    events: Iterable[Event], *, sink: EventSink | None = None
) -> tuple[OrderBook, list[Trade]]:
    """Re-issue `events` into a fresh engine; return it and the trades it made.

    `events` is any iterable of decoded events in `seq` order -- what
    `sinks.sqlite.read_events` yields for a recorded `.db`, or what a
    `sinks.ListSink` collected in memory. Whether the fills are still in it
    does not matter: `events.is_replayable` is applied here.

    The opening `SessionStarted` builds the engine, with the tick size the
    original used, so quantization matches. `sink`, if given, is attached to
    that engine -- which makes the replay itself a recorded session, and its
    stream comparable event for event with the one it was built from.

    Returns `(book, trades)`. The trades are the executions the *rebuilt*
    engine derived, in the order it derived them: the sequence a caller of the
    original session saw, arrived at again rather than read back.

    Raises `ReplayError` if the stream does not open with a `SessionStarted`,
    if it carries a second one (two sessions' events, which would mix two
    books together), or if its `stream_version` is not the one this release
    implements. Note that a generator argument is consumed as it goes, so a
    stream refused part-way has already been partly read.
    """
    book: OrderBook | None = None
    trades: list[Trade] = []

    for event in events:
        if not is_replayable(event):
            continue

        if isinstance(event, SessionStarted):
            if book is not None:
                raise ReplayError(
                    "a second SessionStarted at seq %d: one stream is one "
                    "session, and replaying two into one engine would mix "
                    "two books together" % (event.seq,)
                )
            if event.stream_version != STREAM_VERSION:
                raise ReplayError(
                    "the stream records stream_version %r, and this PyLOB "
                    "implements %d: an older stream replays wrongly rather "
                    "than incompletely, which is what the version is for"
                    % (event.stream_version, STREAM_VERSION)
                )
            book = OrderBook(tick_size=event.tick_size, sink=sink)
            continue

        if book is None:
            raise ReplayError(
                "the stream opens with %s at seq %d, not SessionStarted: "
                "without it there is no tick size to build an engine with"
                % (type(event).__name__, event.seq)
            )

        # Dispatch on the event classes rather than on `KIND` strings: the
        # union is closed (`events.Event`), so a type checker can see that
        # every member is handled and a new one would not be.
        match event:
            case InstrumentConfigured():
                book.configure_instrument(event.symbol, event.currency)
            case TraderConfigured():
                book.configure_trader(
                    event.tid,
                    name=event.name,
                    allow_self_matching=event.allow_self_matching,
                    commission_min=event.commission_min,
                    commission_max_percnt=event.commission_max_percnt,
                    commission_per_unit=event.commission_per_unit,
                )
            case Accepted():
                # The identifier and timestamp are re-used, not reassigned:
                # this is the data-replay path, and it is also what re-seeds
                # the identifier counter past every order in the log.
                _, made = book.submit(
                    tid=event.tid,
                    instrument=event.instrument,
                    side=event.side,
                    order_type=event.order_type,
                    qty=event.qty,
                    price=event.price,
                    idNum=event.idNum,
                    timestamp=event.timestamp,
                )
                trades.extend(made)
            case Modified():
                # `side`, `qty` and `price` are the whole of `orderUpdate`
                # (`OrderBook.modifyOrder`). `Modified` also carries the
                # owner, but a modification cannot change one, and the engine
                # reads the order's own `tid` when it re-emits.
                made, _ = book.modifyOrder(
                    idNum=event.idNum,
                    orderUpdate=dict(side=event.side, qty=event.qty, price=event.price),
                    time=event.timestamp,
                )
                trades.extend(made)
            case Cancelled():
                # Named, both of them: `cancelOrder` takes the side first, and
                # an identifier passed positionally would silently become one.
                book.cancelOrder(
                    side=event.side, idNum=event.idNum, time=event.timestamp
                )
            case _:  # pragma: no cover - a replayable kind with no re-issue
                raise ReplayError(
                    "no way to re-issue %s at seq %d, which "
                    "`events.is_replayable` says is an input"
                    % (type(event).__name__, event.seq)
                )

    if book is None:
        raise ReplayError(
            "the stream is empty: a replay needs at least the SessionStarted "
            "every recorded session opens with"
        )
    return book, trades
