"""The event vocabulary the matching engine emits, and the sink protocol.

ADR-0001 moved matching in-memory and demoted SQLite to an optional recording
sink. This module is the seam between the two: the engine emits events, sinks
consume them. Nothing here imports the engine, and nothing here touches a
database -- the whole point is that a sink can be absent.

Three rules shape every decision below.

**No SQL in the matching path.** An event therefore carries everything a sink
needs to record it. A sink never calls back into the engine to ask a question,
so no event may be a bare notification ("order 7 filled -- go look it up").
That is a rule about the events, and it rests on a rule about sinks: `consume`
runs *inside* the operation it describes, so a sink with a question asks it
mid-update and is answered accordingly. `EventSink` states the contract and
what breaking it costs.

**Sinks are optional and may not change outcomes.** Matching reads nothing
from this module: `seq` is a position in the emitted stream, never a sort key.
The engine's *priority* counter (`Accepted.priority`) is a separate counter
that advances whether or not events are constructed, so an engine running with
no sink matches identically to one running with four. Attaching a sink can
only add writes.

**The log defines the session.** Restart and replay semantics come from the
event stream, not from reopening a live database (ADR-0001, Consequences).
That is why the stream opens with configuration events: tick size, instrument
currency, and trader commission schedules are part of the record, because a
fresh engine cannot reproduce balances or commissions without them.

The vocabulary
--------------

Configuration (low frequency, session setup, re-emitted when config changes):

    SessionStarted        tick size and stream version
    InstrumentConfigured  symbol -> currency
    TraderConfigured      a trader's commission schedule and self-match flag

Lifecycle (the four the `recording-sink` capability mandates):

    Accepted   an order entered the engine (before any matching it causes)
    Filled     one trade, both sides, with post-trade accounting for each
    Cancelled  an order left the book -- by request, or as an IOC remainder
    Modified   a resting order's price or quantity changed

Emission order
--------------

Events are emitted in the order the transitions occurred, each with the next
`seq`. Within the processing of one submission:

    Accepted, then the Filled events it caused, in match order, then (for a
    market order with an unfilled remainder) Cancelled.

Within the processing of one modification:

    Modified, then any Filled events the new price caused.

A submission rejected by validation (bad quantity, unknown side, duplicate
identifier) emits *nothing*: it raises, and the book is unchanged, so there is
no transition to record.

An order leaving the book because it is fully filled emits no separate event;
`fulfilled == qty` on the last `Filled` says so. Likewise a modify that clamps
quantity down to the already-fulfilled amount emits `Modified` with the
clamped quantity, not a cancellation.

Where commissions and balances compute
--------------------------------------

**In the core, not the sink** (design.md decision 3). Online PnL is a research
feature; putting a required feature behind an optional component would mean a
sinkless engine could not answer `order.commission`. So `Filled` carries the
engine's computed numbers -- cumulative fill value, cumulative commission, and
the increment charged for this trade -- and the sink records them rather than
re-deriving them. There is exactly one implementation of the capped
per-unit-with-floor formula, in the engine, and the sink cannot drift from it.

Balances are *not* separate events: four movements per trade would triple the
event rate to carry no information the fill does not already have. A sink (or
a replayer) derives them arithmetically, with the instrument's currency taken
from `InstrumentConfigured`:

    bid side (buyer):  instrument += qty
                       currency   -= qty * price + bid_commission_delta
    ask side (seller): instrument -= qty
                       currency   += qty * price - ask_commission_delta

That is the whole rule, and it is stated here so every consumer applies the
same one. It reproduces the worked example in the `trader-balances` spec: a
buy of 5 @ 101 with 2.5 commission on each side leaves the buyer FAKE +5 /
USD -507.5 and the seller FAKE -5 / USD +502.5.

Replay
------

A persisted stream replays into a fresh engine by re-issuing the *replayable*
events in `seq` order -- `is_replayable` is the filter. Configuration events
are re-applied as configuration; `Accepted`, `Modified`, and `Cancelled` with
reason `REQUESTED` are re-issued as engine calls on the data-replay path, each
carrying its original identifier and timestamp. `Filled` and `Cancelled` with
reason `IOC_REMAINDER` are engine *output*: re-issuing them would double-cancel
or double-book. The replayed engine re-derives them, which is what makes
replay a check on determinism and not merely a restore.

Counters are restored from the stream, not from a sink's side table: the next
order identifier is `max(idNum) + 1` over the stream, the next trade
identifier `max(trade_id) + 1`, so an order submitted after a reload cannot
collide with one from before it (`order-lifecycle`: identifiers are unique
"including across reloads of persisted state").

Notes for the engine implementer
--------------------------------

- `timestamp` is recorded data, never a sort key. On the data-replay path it
  is the value the caller supplied; two orders may carry the same one. Events
  the engine generates while processing a submission (fills, IOC cancels)
  carry that submission's timestamp.
- `Accepted.price` is the *quantized* working price -- the tick has already
  been applied -- so a replay reproduces prices exactly even before it reads
  the tick size. It is `None` for market orders, which never rest.
- `priority` is the matching sort key: orders match by (price, priority).
  `Accepted.priority` stamps arrival; `Modified.priority` is the new stamp,
  re-issued when `reprioritized` is true (price change or quantity increase)
  and unchanged when it is false (pure quantity decrease).
- Emit `Accepted` before matching, so a sink sees the order exist before it
  sees it trade.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, Final, Protocol, runtime_checkable

__all__ = [
    "STREAM_VERSION",
    "Side",
    "OrderType",
    "CancelReason",
    "SessionStarted",
    "InstrumentConfigured",
    "TraderConfigured",
    "Accepted",
    "Filled",
    "Cancelled",
    "Modified",
    "Event",
    "EVENT_TYPES",
    "EVENT_BY_KIND",
    "is_replayable",
    "EventSink",
    "ClosableEventSink",
    "close_sink",
]

#: Version of the event stream format. Bumped when a change to the events
#: below would make an older persisted stream replay wrongly rather than
#: merely incompletely. A sink records it; a replayer refuses what it does not
#: understand.
STREAM_VERSION: Final = 1


class Side(StrEnum):
    """Which side of the book. `str` values, so `event.side == "bid"` holds."""

    BID = "bid"
    ASK = "ask"


class OrderType(StrEnum):
    """Limit orders rest; market orders are immediate-or-cancel and never do."""

    LIMIT = "limit"
    MARKET = "market"


class CancelReason(StrEnum):
    """Why an order left the book.

    The distinction is load-bearing for replay: `REQUESTED` is an input to
    re-issue, `IOC_REMAINDER` is an output the engine re-derives. Adding a
    member is a change to the stream format.
    """

    #: The caller asked for it (`cancelOrder`).
    REQUESTED = "requested"
    #: A market order's unfilled remainder, cancelled by the engine.
    IOC_REMAINDER = "ioc_remainder"


# --------------------------------------------------------------------------
# configuration events
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionStarted:
    """The engine was constructed. First event of every stream.

    A sink writes a session/meta row; a replayer builds its fresh engine with
    this `tick_size` so quantization matches, and refuses a `stream_version`
    it does not implement.
    """

    KIND: ClassVar[str] = "session_started"

    seq: int
    timestamp: float
    tick_size: float
    stream_version: int = STREAM_VERSION


@dataclass(frozen=True, slots=True)
class InstrumentConfigured:
    """An instrument's denominating currency was declared.

    A sink needs it to book the currency leg of every trade and the commission
    debit; without it a fill is only half a balance movement.

    `currency` is a currency and never `None`. An instrument may have no
    currency -- that is what one nobody configured has, and a sink says so
    rather than inventing one -- but that state is the absence of this event,
    not a value it can carry. The engine therefore refuses to withdraw a
    currency instead of emitting a withdrawal (`engine.configure_instrument`;
    lob-9fu): admitting `None` here would widen the wire format, oblige every
    sink to unlearn a currency it has already stamped on orders, and buy a
    library that converts nothing between currencies (`config.yaml`: no FX)
    nothing at all.
    """

    KIND: ClassVar[str] = "instrument_configured"

    seq: int
    timestamp: float
    symbol: str
    currency: str


@dataclass(frozen=True, slots=True)
class TraderConfigured:
    """A trader's commission schedule and self-matching flag were set.

    Re-emitted whenever they change; last one before a given `seq` wins. The
    schedule is recorded even though the engine charges the commission itself,
    because a replayed engine has to charge the same one.
    """

    KIND: ClassVar[str] = "trader_configured"

    seq: int
    timestamp: float
    tid: int
    name: str
    allow_self_matching: bool
    commission_min: float
    #: `percnt`, sic. It is a field name in the wire format, and a persisted
    #: stream decodes by keyword (`EVENT_BY_KIND[kind](**payload)`), so
    #: correcting the spelling would invalidate every recorded session.
    commission_max_percnt: float
    commission_per_unit: float


# --------------------------------------------------------------------------
# lifecycle events
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Accepted:
    """An order passed validation and entered the engine, before any matching.

    Emitted for every accepted order, including market orders that never rest
    and orders that fill immediately and in full. A sink writes the order row;
    a replayer re-submits it on the data-replay path with this `idNum` and
    `timestamp`.
    """

    KIND: ClassVar[str] = "accepted"

    seq: int
    timestamp: float
    idNum: int
    tid: int
    instrument: str
    side: Side
    order_type: OrderType
    #: Quantized working price; `None` for a market order.
    price: float | None
    qty: int
    #: Arrival stamp. Matching order is (price, priority).
    priority: int


@dataclass(frozen=True, slots=True)
class Filled:
    """One trade. Emitted per execution, after the taker's `Accepted`.

    Carries both sides in full: identifiers, owners, and each side's
    post-trade cumulative accounting, so a sink can update its order rows,
    trade log, commission totals, and balances from this event alone.

    `price` is the maker's limit price (`order-matching`: trades price at the
    maker) and is also the instrument's new last-trade price.

    The `*_fulfilled` and `*_value` fields are cumulative over the order's
    life, not per-trade: `*_fulfilled == qty` means that order is done. So is
    `*_commission`, which is recomputed from cumulative (Q, V) per the
    `commissions` contract; `*_commission_delta` is the increment actually
    charged for this trade, and is what a balance movement uses.
    """

    KIND: ClassVar[str] = "filled"

    seq: int
    timestamp: float
    instrument: str
    trade_id: int
    price: float
    qty: int
    #: Which side was the aggressor. The other side was resting.
    taker_side: Side

    bid_idNum: int
    bid_tid: int
    bid_fulfilled: int
    bid_value: float
    bid_commission: float
    bid_commission_delta: float

    ask_idNum: int
    ask_tid: int
    ask_fulfilled: int
    ask_value: float
    ask_commission: float
    ask_commission_delta: float


@dataclass(frozen=True, slots=True)
class Cancelled:
    """An order left the book without being fully filled.

    Covers both a caller's `cancelOrder` and a market order's unfilled
    remainder; `reason` tells them apart, and a replayer re-issues only the
    former. `fulfilled` and `remaining` sum to the order's stated quantity, so
    a sink can close the order row without consulting its own history.

    A partially filled order keeps the commission already charged on the
    filled part (`commissions`: cancelling does not refund), which is why no
    commission field appears here -- there is no movement to record.
    """

    KIND: ClassVar[str] = "cancelled"

    seq: int
    timestamp: float
    idNum: int
    tid: int
    instrument: str
    side: Side
    #: The order's working price; `None` for a market-order remainder.
    price: float | None
    fulfilled: int
    #: Quantity that never traded -- what the cancellation removed.
    remaining: int
    reason: CancelReason


@dataclass(frozen=True, slots=True)
class Modified:
    """A resting order's price or quantity changed.

    Emitted after the change is applied and before any fills the new price
    causes, so a sink applying events in order never sees a fill against a
    stale price. `price`/`qty` are the new values (quantity already clamped up
    to `fulfilled` where the caller asked for less), `prev_price`/`prev_qty`
    the old ones -- a sink gets the diff without keeping its own copy.

    `reprioritized` records the `order-lifecycle` priority rule: a price
    change or a quantity increase sends the order to the back of its level and
    takes a new `priority`; a pure quantity decrease keeps both its place and
    its old stamp.
    """

    KIND: ClassVar[str] = "modified"

    seq: int
    timestamp: float
    idNum: int
    tid: int
    instrument: str
    side: Side
    price: float | None
    qty: int
    fulfilled: int
    prev_price: float | None
    prev_qty: int
    priority: int
    reprioritized: bool


# --------------------------------------------------------------------------
# the stream
# --------------------------------------------------------------------------

#: Any event. A union rather than a base class: the dataclasses stay flat and
#: cheap, and a consumer dispatches with `match event: case Filled(): ...`,
#: which type checkers can verify is exhaustive.
Event = (
    SessionStarted
    | InstrumentConfigured
    | TraderConfigured
    | Accepted
    | Filled
    | Cancelled
    | Modified
)

EVENT_TYPES: Final[tuple[type, ...]] = (
    SessionStarted,
    InstrumentConfigured,
    TraderConfigured,
    Accepted,
    Filled,
    Cancelled,
    Modified,
)

#: `KIND` -> event class, for decoding a persisted stream back into events.
#: `KIND` is the wire name and is stable across renames of the Python class.
EVENT_BY_KIND: Final[Mapping[str, type]] = MappingProxyType(
    {event_type.KIND: event_type for event_type in EVENT_TYPES}
)


def is_replayable(event: Event) -> bool:
    """Is this event an input to re-issue, or output the engine re-derives?

    True for configuration events and for the commands a caller made
    (`Accepted`, `Modified`, a requested `Cancelled`). False for `Filled` and
    for an IOC remainder cancellation: those are consequences, and a replayed
    engine produces them itself. Feeding them back in would double-count.
    """
    if isinstance(event, Filled):
        return False
    if isinstance(event, Cancelled):
        return event.reason == CancelReason.REQUESTED
    return True


# --------------------------------------------------------------------------
# the sink protocol
# --------------------------------------------------------------------------


@runtime_checkable
class EventSink(Protocol):
    """What the engine requires of a sink: one method, structurally.

    A `Protocol`, not a base class -- nothing registers, nothing subclasses,
    and a sink can be a three-line class in a test. `consume` is called
    synchronously, in `seq` order, on the thread that is matching, so an
    implementation that does real I/O per event should buffer: "off the hot
    path" is the sink's job, not the engine's (design.md decision 4).

    It gets every event exactly once.

    **A sink records what it is handed. It does not read the engine, call back
    into it, or raise.** *Synchronously* above means from inside the operation
    being recorded rather than after it, so `consume` runs while the engine's
    structures are part-way through an update. All three are prohibitions with
    nothing enforcing them, and each costs something different.
    `recording-sink`, "A sink observes the stream and does not act on the
    engine", ratifies the first two and the absence of enforcement: a sink
    "SHALL NOT call into the engine from `consume` -- neither a query nor a
    mutation", and the engine "SHALL NOT detect, refuse, or compensate" for
    one that does. Raising is this module's own prohibition; no requirement
    describes what the engine is left holding afterwards.

    *Reading* returns an answer that is not merely stale but
    self-contradictory. A match walk lifts the level it is working off the
    best-price heap and leaves it in the book, so a sink that asks during a
    `Filled` sees `getBestAsk` return `None` for a side whose `getWorstAsk`,
    `snapshot` and `getVolumeAtPrice` all report a resting order at that price
    -- the disagreement `book-queries` requires never to happen. The engine
    keeps that promise everywhere a caller of its own methods can stand, and a
    sink is standing somewhere else.

    *Calling back* corrupts the walk in progress. Cancelling a maker the walk
    has stepped over -- one of the taker's own, held in place by the
    self-matching gate -- shortens the prefix the walk's cursor is rebuilt
    over, so it resumes past a maker the taker was entitled to trade with. The
    taker then rests against the liquidity it should have taken: a book
    crossed between two traders, permanently, with every event describing a
    session in which nothing went wrong.

    *Raising* aborts the operation from the middle. The fills already executed
    stay executed and settled and their `Filled` events stay in the stream,
    and the taker is left answering `resting` while no level holds it
    (`engine.Order.resting`). The book's own structures survive it; the
    submission does not.

    A sink that needs engine state folds it out of the stream instead, which
    is what the stream is for ("Replay", above): every event carries what a
    consumer needs to maintain its own view, exactly so that no sink has to
    ask.
    """

    def consume(self, event: Event, /) -> None:
        """Record one event."""
        ...


@runtime_checkable
class ClosableEventSink(EventSink, Protocol):
    """A sink that needs a flush at end of session, such as a buffered writer.

    `close` is deliberately not part of `EventSink`: requiring it would make
    the trivial sinks (append to a list) carry a no-op method. Use
    `close_sink` rather than testing for the method by hand.

    Unlike `consume`, `close` runs *between* operations -- `OrderBook.close`
    is a call in its own right -- so a sink that wants to read the engine at
    end of session may do it here, where the book is settled and the reads
    `EventSink` warns about return what every other caller sees.
    """

    def close(self) -> None:
        """Flush anything buffered and release resources. Idempotent."""
        ...


def close_sink(sink: EventSink) -> None:
    """Close `sink` if it has a `close`; do nothing if it does not.

    The one sanctioned way to end a sink's life, so "close is optional" is a
    written contract rather than a convention each caller reinvents.

    The test is `isinstance` against `ClosableEventSink`, which is what makes
    that protocol the definition of "closable" rather than a docstring
    describing a `getattr` somewhere else. A `runtime_checkable` protocol
    checks for the attribute and not its signature, so `callable` still runs:
    a sink whose `close` is a bool satisfies the isinstance and is not a sink
    that can be closed.
    """
    if isinstance(sink, ClosableEventSink):
        close = getattr(sink, "close", None)
        if callable(close):
            close()
