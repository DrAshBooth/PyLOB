"""Event sinks: consumers of the stream `PyLOB.events` defines.

A sink is anything with `consume(event)` (`events.EventSink`, a structural
`Protocol`). Nothing here is required for matching -- ADR-0001 made
persistence optional, and an engine with no sink attached does no I/O at all.

Two implementations ship:

`SQLiteSink` turns the stream into a queryable database off the matching path.
Its module docstring (`PyLOB.sinks.sqlite`) is the reference for the recorded
schema, for reading a session back, and for what a killed run leaves behind.

`ListSink` keeps every event in a list and does nothing else. It is the
three-line sink `EventSink`'s docstring describes, shipped so that a test or a
notebook asking "what did the engine emit" does not write it a fourth time:

    from PyLOB import OrderBook
    from PyLOB.sinks import ListSink

    sink = ListSink()
    book = OrderBook(tick_size=0.01, sink=sink)
    ...
    [event.kind for event in sink.events]

It has no `close`, holds everything it is given, and is therefore for sessions
whose size you know. A long run wants `SQLiteSink`.
"""

from ..events import Event
from .sqlite import (
    SCHEMA_VERSION,
    EventLogError,
    IncompleteLogError,
    SQLiteSink,
    check_log,
    decode_event,
    read_events,
)

__all__ = [
    "SQLiteSink",
    "ListSink",
    "SCHEMA_VERSION",
    "EventLogError",
    "IncompleteLogError",
    "check_log",
    "decode_event",
    "read_events",
]


class ListSink:
    """Every event the engine emitted, in `seq` order, in `events`.

    Satisfies `events.EventSink` structurally. No encoding and no I/O: it
    answers questions about the engine rather than about a database.
    """

    __slots__ = ("events",)

    def __init__(self) -> None:
        self.events: list[Event] = []

    def consume(self, event: Event, /) -> None:
        """Record one event."""
        self.events.append(event)
