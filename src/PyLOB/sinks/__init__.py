"""Event sinks: consumers of the stream `PyLOB.events` defines.

A sink is anything with `consume(event)` (`events.EventSink`, a structural
`Protocol`). Nothing here is required for matching -- ADR-0001 made
persistence optional, and an engine with no sink attached does no I/O at all.

The one shipped implementation is `SQLiteSink`, which turns the stream into a
queryable database off the matching path.
"""

from .sqlite import (
    SCHEMA_VERSION,
    EventLogError,
    SQLiteSink,
    check_log,
    decode_event,
    read_events,
)

__all__ = [
    "SQLiteSink",
    "SCHEMA_VERSION",
    "EventLogError",
    "check_log",
    "decode_event",
    "read_events",
]
