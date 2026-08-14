"""SQLite recording sink: the event stream as queryable history.

ADR-0001 took SQLite off the matching path and kept it as an optional
recorder. This module is that recorder. It implements `events.EventSink`
structurally -- no subclassing, no registration -- and the engine neither
imports it nor knows whether it exists.

Which table answers which question
----------------------------------

For a reader arriving from the README with a recorded `.db` and a question.
`sqlite3 session.db .schema` is the reference and stays the reference: every
table and column carries its comment inside its own `CREATE` statement, so the
schema explains itself wherever it is read. This is only the map of it.

    what became of an order          `orders`; `resting_order` for the ones
                                     still on the book, and what is left of
                                     them to trade
    what traded, and at what price   `trade`, one row per execution;
                                     `instrument.last_price` for the latest
    what a trader holds              `balance`, per trader per instrument-or-
                                     currency -- derived, never emitted
    what a trader paid in commission `trader_commission`, per currency
    what the engine actually emitted `event` -- the log itself, one JSON row
                                     per event, and the input to a replay
    whether the file can be trusted  `check_log`, which reads `session_end`
                                     and `event_loss` so that you need not

`resting_order` and `trader_commission` are views over `orders`, not tables.
Per-trade balance movements are not stored -- `balance` holds the running sum
-- but each trade's four movements are derivable from its `trade` row, whose
price, quantity and per-side commission deltas are exactly what the balance
rule below consumes.

Shape of the database
---------------------

Two layers, written in the same transaction:

**`event` -- the append-only log.** One row per event: `seq`, `kind`,
`timestamp`, `replayable`, and the whole event as JSON. This is the source of
truth and the replay input. It is deliberately *not* normalised: an event is
persisted exactly as it was emitted, so `EVENT_BY_KIND[kind](**payload)`
reconstructs it field for field (`decode_event` does precisely that). A
normalised-only schema would force replay to re-assemble events from
projections, which is lossy the moment the projections summarise anything.
`replayable` materialises `events.is_replayable` at write time -- the filter
is computed by the sanctioned function, never re-derived in SQL -- so a
replayer's read is one indexed scan.

**`session`, `instrument`, `trader`, `orders`, `trade`, `balance` -- the
projections.** The log answers "what happened"; these answer "what is the
state", which is what the `recording-sink` capability asks for: order
history, trade history, balances, commissions, and last-trade price,
queryable by SQL after the session ends. They are a fold of the log, so they
add no information -- but they turn "reconstruct the book from 200k JSON
rows" into `SELECT * FROM resting_order`.

Keeping both costs roughly one extra row-write per event. Cheap, and it buys
the two consumers what each needs without compromise: replay (lob-5rt.7)
reads the log, outcome comparison (lob-5rt.11) reads the projections.

The schema is event-shaped, not shaped like the matching schema of the SQL
engine ADR-0003 retired (design.md decision 5). Nothing here is matching
state and nothing triggers: no views that compute eligibility, no triggers
that move balances. Rows are written by Python, in one direction, after the
fact.

Buffering
---------

`consume` is called synchronously on the matching thread, so it does the
least it can: one list append. Encoding and I/O happen in `flush`, which
folds the whole buffer into per-table parameter lists and writes it in a
single transaction (design.md decision 4). Flush fires when the buffer
reaches `buffer_size` and on `close`.

Batching changes nothing about the resulting database. Statements that touch
the same key are applied in `seq` order within their table, and balance
movements accumulate through `amount = amount + excluded.amount`, executed
once per movement in order -- so a sink with `buffer_size=1` and one with
`buffer_size=100000` produce byte-identical projections. `buffer_size` is a
performance knob and nothing else.

Failure
-------

Losing data silently is the one outcome worse than losing the session, and
nothing below relies on the process living long enough to be told.

`consume` does not raise on the engine's account: the sink is optional and may
not take the engine down with it. The single exception is `consume` *after*
`close`, which raises `RuntimeError` -- there is nowhere left to record, so
accepting the event would be the silent discard this sink exists to prevent.

A batch that fails to write is not dropped whole. The transaction rolls back
and the sink re-attempts the same events one at a time, which is precisely
what a `buffer_size=1` sink would have written (see Buffering) -- so the
salvage path cannot produce a database the ordinary path could not. Whatever
writes, writes. Whatever still fails is *recorded as lost* in `event_loss`,
one row per contiguous run of missing `seq`, carrying the error that stopped
it. One poison event costs one event rather than the 511 around it, and a
failure that was merely transient costs nothing at all.

`flush` raises if and only if events were lost, and remembers the error
exactly as `consume` does. `close` re-raises it on *every* call, not merely
the first, so a session that lost something cannot end quietly however many
times it is closed.

The file says so independently, which is the part that survives the process
never reaching `close`. `check_log` -- which `read_events` runs before
yielding anything -- refuses a log whose `seq` values do not run from 0
without a gap, refuses one carrying any `event_loss` row, and refuses a
`stream_version` or `user_version` this module does not implement. A
recording that lost a batch, and a log truncated in the middle by anything
else, cannot masquerade as a complete one.

Ending, and being killed
------------------------

A process killed mid-session leaves no `event_loss` row -- nothing was
attempted, so nothing failed -- and its `seq` values run from 0 with no gap.
Everything above would call that file complete. It is not: the buffered tail
died with the process, and a shorter session is exactly what a truncated one
looks like. Somebody recording a run in order to study it afterwards would
study a prefix believing it whole.

So `close` writes a `session_end` row as its last act, after the final flush.
Its absence is the tell. A crash cannot forge one, and the failure direction
is the safe one: a session killed *during* `close`, or one whose end row
would not write, reads as unfinished rather than as finished.

That state is **suspect, not corrupt**, and `check_log` reports it as a
distinct `IncompleteLogError`. A killed run is normal, and the events in the
file are genuinely good -- every one of them was committed. What is unknown
is only how many more there were meant to be, which is why nothing here
claims a count for what the buffer took with it.

How much the tail is depends on `buffer_size`, and short episodes are the
case to know about: at the default 512, a session killed after a couple of
hundred events leaves a file with **no rows in it at all**. That is the
ordinary outcome and not a symptom of anything -- but somebody running many
small episodes, meaning to inspect whichever one goes wrong, wants a smaller
`buffer_size` or a `flush` of their own, decided before the run rather than
after it.

`session_end` also carries the log's size at the moment it was written, so
rows deleted from the *end* of a finished log -- the one edit no gap and no
marker could ever reveal -- come out as a mismatch between what the sink
recorded closing with and what is there now.

The log and the projections can never disagree: every write path -- whole
batch and salvaged single event alike -- puts both in one transaction, so
they commit together or not at all. Re-folding the recorded log into a fresh
database reproduces the projections exactly, losses included.

Durability
----------

WAL plus `synchronous = NORMAL` means a committed transaction survives the
*process* dying. It does not promise to survive an OS crash or power loss,
either of which may take the most recent commits with it -- the right trade
for analytics data the engine never reads back, but not the same claim as
"durable". WAL is also requested rather than guaranteed: some filesystems
refuse it. The resulting mode is checked, warned about when it is not `wal`,
and readable as `journal_mode`, because a durability story nobody verified is
not one.

**A recording in WAL mode is up to three files, and the `.db` alone is not
it.** Commits land in `session.db-wal` and reach `session.db` only at a
checkpoint. `close` closes the connection, which checkpoints, so a session
that ended properly is the single file it looks like, and copying it is
copying it. A session that was *killed* checkpointed only whatever SQLite
auto-checkpointed along the way, and copying its `.db` without the sidecars
fails in one of two ways depending on whether that had happened yet:

*Loudly*, if it had not. The `.db` is a 4 KB shell holding no tables at all,
and `check_log` says so and names the `-wal` (measured: 56 of 56 events left
behind).

*Silently*, if it had. The copy is a valid, contiguous, shorter log --
measured at 2168 of 2400 events -- with no `event_loss` row and no
`session_end` row, which is exactly what a genuine killed run that stopped at
2168 looks like. Nothing in the file reveals the missing 232, and no marker
this sink could write would: the `session_end` row that counts the log
travels through the same WAL, and a checkpoint copies a *prefix* of that WAL,
so a truncated copy can never carry a count for its own contents to
contradict. Verified directly -- a copy taken after `session_end` was
committed still had neither the row nor the events it counted. This is the
one way to lose recorded data that the file cannot be made to report, and it
is caused entirely outside this module, by the copy.

So copy a killed run's `session.db`, `session.db-wal` and `session.db-shm`
together, or checkpoint before copying -- `sqlite3 session.db 'PRAGMA
wal_checkpoint(TRUNCATE)'`, or just open and close the file with any SQLite
client, after which the `.db` travels alone. Copying the `.db` by itself
corrupts nothing and loses nothing that is still on the original disk: the
events stay where they were, in the sidecar that was left behind.

That is the loud outcome, and it is the likely one for a short run. A long
one may have auto-checkpointed part way, and then the lone `.db` holds the
events up to that checkpoint and none after it -- tables present, `seq`
running from 0 without a gap, no `event_loss` row, no `session_end` because
the run was killed. `check_log` calls that an incomplete log, which is true,
and cannot tell you it is *also* a partial copy: a truncated prefix of a
killed session is byte-for-byte what a shorter killed session looks like, and
no extent was ever recorded to contradict it. This is the reason to copy all
three files rather than a matter of tidiness, and it is the one way to lose
recorded events without the file saying so.

Scope
-----

The sink records; it does not referee. Foreign keys are declared for
documentation and left unenforced (SQLite's default), so a hand-built or
truncated stream still records rather than erroring on the engine's behalf.
One database per session -- `seq` is the log's primary key, so a second
session pointed at a written file cannot write an event: the first flush
fails, every event of it fails again on salvage, and the whole batch is
recorded as lost rather than half-merged into somebody else's history.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from itertools import groupby
from typing import Any

from ..events import (
    EVENT_BY_KIND,
    STREAM_VERSION,
    Accepted,
    Cancelled,
    CancelReason,
    Event,
    Filled,
    InstrumentConfigured,
    Modified,
    OrderType,
    SessionStarted,
    Side,
    TraderConfigured,
    is_replayable,
)

__all__ = [
    "SQLiteSink",
    "SCHEMA_VERSION",
    "SCHEMA",
    "EventLogError",
    "IncompleteLogError",
    "check_log",
    "decode_event",
    "read_events",
]

_log = logging.getLogger(__name__)

#: Layout version of the tables below, stored in `PRAGMA user_version`. Bumped
#: when a written database would be read wrongly by this module. Distinct from
#: `events.STREAM_VERSION`, which versions what the engine emits: the same
#: stream can be recorded by two schema versions.
#:
#: 2 added `event_loss` and `orders.currency`. 3 added `session_end`. Both
#: bumps are for the same reason: an older file has no way to say whether it
#: lost anything, or whether it was ever closed, and an absent table must not
#: be read as the good answer to a question that file cannot answer.
SCHEMA_VERSION = 3

#: Default events per transaction. Tuning only; design.md leaves the number to
#: benchmark data, and the resulting database does not depend on it.
DEFAULT_BUFFER_SIZE = 512

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    seq        INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL,
    -- The engine's clock, NOT epoch time: simulated time, or an operation
    -- counter, or whatever the caller passed -- ordering and spacing are
    -- meaningful, the absolute value is not a date. Plot against it, do not
    -- convert it. Every other `timestamp`/`_ts` column here is this same
    -- clock, carried from the event; wall clock lives only in the `_at`
    -- columns (`event_loss.recorded_at`, `session_end.recorded_at`).
    timestamp  REAL    NOT NULL,
    -- events.is_replayable, materialised at write time.
    replayable INTEGER NOT NULL,
    -- The emitted event, dataclasses.asdict -> JSON. Round-trips exactly.
    payload    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS event_kind ON event (kind);

-- Events this sink accepted and could not write, so that a reader can see the
-- hole without having watched the session die. One row per contiguous run of
-- lost `seq`; `error` is what stopped the first event of the run. Every column
-- is a value this module supplies, so recording a loss cannot itself fail on a
-- constraint -- and the table has no primary key, so it cannot fail on a
-- conflict either. Empty is the only state that means "nothing was lost".
CREATE TABLE IF NOT EXISTS event_loss (
    first_seq   INTEGER NOT NULL,
    last_seq    INTEGER NOT NULL,
    count       INTEGER NOT NULL,
    -- Wall clock at the moment of the loss, not an event timestamp: the events
    -- it describes are precisely the ones that are not here to be asked.
    recorded_at REAL    NOT NULL,
    error       TEXT    NOT NULL
);

-- Written by `close` and by nothing else, so that a log which simply stops --
-- the process killed, its buffered tail gone with it -- is not read as a
-- session that ended here. One row, or none. `event_count` and `last_seq` are
-- what the log held at that moment, which is how rows deleted from the end of
-- a finished log are caught: they leave no gap and trip no marker.
CREATE TABLE IF NOT EXISTS session_end (
    recorded_at REAL    NOT NULL,
    last_seq    INTEGER,
    event_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
    seq            INTEGER PRIMARY KEY,
    timestamp      REAL    NOT NULL,
    tick_size      REAL    NOT NULL,
    stream_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS instrument (
    symbol     TEXT PRIMARY KEY,
    currency   TEXT NOT NULL,
    -- Price of the most recent trade; NULL until one happens.
    last_price REAL
);

CREATE TABLE IF NOT EXISTS trader (
    tid                   INTEGER PRIMARY KEY,
    name                  TEXT,
    allow_self_matching   INTEGER NOT NULL,
    commission_min        REAL    NOT NULL,
    commission_max_percnt REAL    NOT NULL,
    commission_per_unit   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    -- Plural, alone among these tables, because ORDER is an SQL keyword:
    -- `SELECT * FROM order` is a syntax error, and quoting it in every query
    -- forever is a worse tax than the inconsistent name.
    idNum         INTEGER PRIMARY KEY,
    tid           INTEGER NOT NULL REFERENCES trader (tid),
    instrument    TEXT    NOT NULL REFERENCES instrument (symbol),
    -- The instrument's currency as configured when this order was accepted.
    -- Denormalised on purpose: commission is reported per currency, and the
    -- instrument's current currency is the wrong answer for an order charged
    -- before it changed. NULL when no `InstrumentConfigured` had arrived.
    currency      TEXT,
    side          TEXT    NOT NULL,
    order_type    TEXT    NOT NULL,
    -- Quantized working price; NULL for a market order.
    price         REAL,
    qty           INTEGER NOT NULL,
    -- Cumulative, as carried by the last event to touch this order.
    fulfilled     INTEGER NOT NULL DEFAULT 0,
    value         REAL    NOT NULL DEFAULT 0.0,
    commission    REAL    NOT NULL DEFAULT 0.0,
    priority      INTEGER NOT NULL,
    status        TEXT    NOT NULL,  -- open | filled | cancelled
    cancel_reason TEXT,              -- events.CancelReason, when cancelled
    accepted_seq  INTEGER NOT NULL,
    accepted_ts   REAL    NOT NULL,
    last_seq      INTEGER NOT NULL,
    last_ts       REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_book ON orders (instrument, side, status);
CREATE INDEX IF NOT EXISTS orders_tid ON orders (tid);

CREATE TABLE IF NOT EXISTS trade (
    trade_id             INTEGER PRIMARY KEY,
    seq                  INTEGER NOT NULL,
    timestamp            REAL    NOT NULL,
    instrument           TEXT    NOT NULL REFERENCES instrument (symbol),
    -- The maker's limit price, and the instrument's new last price.
    price                REAL    NOT NULL,
    qty                  INTEGER NOT NULL,
    taker_side           TEXT    NOT NULL,
    bid_idNum            INTEGER NOT NULL REFERENCES orders (idNum),
    bid_tid              INTEGER NOT NULL,
    bid_fulfilled        INTEGER NOT NULL,
    bid_value            REAL    NOT NULL,
    bid_commission       REAL    NOT NULL,
    bid_commission_delta REAL    NOT NULL,
    ask_idNum            INTEGER NOT NULL REFERENCES orders (idNum),
    ask_tid              INTEGER NOT NULL,
    ask_fulfilled        INTEGER NOT NULL,
    ask_value            REAL    NOT NULL,
    ask_commission       REAL    NOT NULL,
    ask_commission_delta REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS trade_instrument ON trade (instrument, seq);

-- Derived, never emitted: see the balance rule in PyLOB.events' docstring.
CREATE TABLE IF NOT EXISTS balance (
    tid    INTEGER NOT NULL REFERENCES trader (tid),
    -- An instrument symbol or a currency: both are trader holdings.
    symbol TEXT    NOT NULL,
    amount REAL    NOT NULL DEFAULT 0.0,
    PRIMARY KEY (tid, symbol)
);

-- What is still on the book, with the quantity still available to trade.
CREATE VIEW IF NOT EXISTS resting_order AS
    SELECT idNum, tid, instrument, side, price, qty, fulfilled,
           qty - fulfilled AS available, priority
    FROM orders
    WHERE status = 'open';

-- Commission charged per trader per currency. Per the `commissions` contract
-- the charge is a function of an order's cumulative fills, so it sums over
-- orders, not over trades. The currency is the order's own, stamped when it
-- was accepted: joining `instrument` would report every historical commission
-- under whatever currency the instrument was last configured with.
CREATE VIEW IF NOT EXISTS trader_commission AS
    SELECT tid, currency, SUM(commission) AS commission
    FROM orders
    GROUP BY tid, currency;
"""

_EVENT_INSERT = """
INSERT INTO event (seq, kind, timestamp, replayable, payload)
VALUES (?, ?, ?, ?, ?)
"""

_LOSS_INSERT = """
INSERT INTO event_loss (first_seq, last_seq, count, recorded_at, error)
VALUES (?, ?, ?, ?, ?)
"""

_END_INSERT = """
INSERT INTO session_end (recorded_at, last_seq, event_count) VALUES (?, ?, ?)
"""

_LOG_EXTENT = "SELECT COUNT(*), MAX(seq) FROM event"

# Zero means the file has no schema at all, which is a different problem from
# having the wrong one: see `_no_tables_message`.
_TABLE_COUNT = "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"

_SESSION_UPSERT = """
INSERT INTO session (seq, timestamp, tick_size, stream_version)
VALUES (?, ?, ?, ?)
ON CONFLICT (seq) DO UPDATE SET
    timestamp = excluded.timestamp,
    tick_size = excluded.tick_size,
    stream_version = excluded.stream_version
"""

_INSTRUMENT_UPSERT = """
INSERT INTO instrument (symbol, currency) VALUES (?, ?)
ON CONFLICT (symbol) DO UPDATE SET currency = excluded.currency
"""

_LAST_PRICE_UPDATE = "UPDATE instrument SET last_price = ? WHERE symbol = ?"

_TRADER_UPSERT = """
INSERT INTO trader (tid, name, allow_self_matching,
                    commission_min, commission_max_percnt, commission_per_unit)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (tid) DO UPDATE SET
    name = excluded.name,
    allow_self_matching = excluded.allow_self_matching,
    commission_min = excluded.commission_min,
    commission_max_percnt = excluded.commission_max_percnt,
    commission_per_unit = excluded.commission_per_unit
"""

_ORDER_INSERT = """
INSERT INTO orders (idNum, tid, instrument, currency, side, order_type,
                    price, qty, priority, status, accepted_seq, accepted_ts,
                    last_seq, last_ts)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
"""

# `ELSE status` keeps a cancellation sticky: an order cancelled and then
# (impossibly) filled would still read as cancelled rather than flip back.
_ORDER_FILL = """
UPDATE orders SET
    fulfilled = ?, value = ?, commission = ?, last_seq = ?, last_ts = ?,
    status = CASE WHEN ? >= qty THEN 'filled' ELSE status END
WHERE idNum = ?
"""

_ORDER_CANCEL = """
UPDATE orders SET
    status = 'cancelled', cancel_reason = ?, fulfilled = ?,
    last_seq = ?, last_ts = ?
WHERE idNum = ?
"""

# A modify that clamps quantity down to the fulfilled amount completes the
# order (PyLOB.events: it emits Modified, not a cancellation), so status is
# recomputed from the new quantity rather than left alone.
_ORDER_MODIFY = """
UPDATE orders SET
    price = ?, qty = ?, fulfilled = ?, priority = ?, last_seq = ?, last_ts = ?,
    status = CASE
        WHEN status = 'cancelled' THEN 'cancelled'
        WHEN ? >= ? THEN 'filled'
        ELSE 'open'
    END
WHERE idNum = ?
"""

_TRADE_INSERT = """
INSERT INTO trade (trade_id, seq, timestamp, instrument, price, qty, taker_side,
                   bid_idNum, bid_tid, bid_fulfilled, bid_value,
                   bid_commission, bid_commission_delta,
                   ask_idNum, ask_tid, ask_fulfilled, ask_value,
                   ask_commission, ask_commission_delta)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_BALANCE_UPSERT = """
INSERT INTO balance (tid, symbol, amount) VALUES (?, ?, ?)
ON CONFLICT (tid, symbol) DO UPDATE SET amount = amount + excluded.amount
"""

#: Event fields whose persisted value is a `StrEnum` member. JSON stores them
#: as the plain strings they already are; decoding puts the member back so a
#: replayed event is indistinguishable from an emitted one.
_ENUM_FIELDS: dict[str, type] = {
    "side": Side,
    "taker_side": Side,
    "order_type": OrderType,
    "reason": CancelReason,
}

_Params = tuple[Any, ...]
_Statement = tuple[str, _Params]


@dataclass(slots=True)
class _Batch:
    """One flush's worth of writes, folded out of the event buffer.

    Each list is a separate write stream. Streams are independent -- no two
    touch the same table -- so only the order *within* a stream matters, and
    that order is the stream's `seq` order. `orders` is the one stream whose
    statements vary by event kind, because a single order row is created by
    `Accepted` and then mutated by fills, modifies and cancels.
    """

    events: list[_Params] = field(default_factory=list)
    sessions: list[_Params] = field(default_factory=list)
    instruments: list[_Params] = field(default_factory=list)
    traders: list[_Params] = field(default_factory=list)
    orders: list[_Statement] = field(default_factory=list)
    trades: list[_Params] = field(default_factory=list)
    balances: list[_Params] = field(default_factory=list)
    last_prices: list[_Params] = field(default_factory=list)


def _runs(statements: Iterable[_Statement]) -> Iterator[tuple[str, list[_Params]]]:
    """Group consecutive statements that share SQL, preserving order.

    Turns the `orders` stream into as few `executemany` calls as its ordering
    permits: a burst of acceptances is one call, and a stretch of fills
    against the same book is another.
    """
    for sql, group in groupby(statements, key=lambda item: item[0]):
        yield sql, [params for _, params in group]


def _gaps(lost: Iterable[tuple[int, str]]) -> list[tuple[int, int, int, str]]:
    """Group lost `(seq, error)` pairs into contiguous runs of `seq`.

    Returns `(first_seq, last_seq, count, error)` -- one `event_loss` row
    each. A whole batch failing for one reason becomes a single row naming the
    range rather than 512 rows naming the same error; scattered failures stay
    separate, because a reader needs to know which `seq` are gone and not
    merely how many. `error` is the one that stopped the run's first event:
    they are almost always the same error, and the first is the one whose
    cause is not yet obscured by the ones after it.
    """
    runs: list[tuple[int, int, int, str]] = []
    for seq, message in lost:
        if runs and seq == runs[-1][1] + 1:
            first, _, count, error = runs[-1]
            runs[-1] = (first, seq, count + 1, error)
        else:
            runs.append((seq, seq, 1, message))
    return runs


class SQLiteSink:
    """Records an event stream into a SQLite database, in batches.

    Satisfies `events.EventSink` structurally and `events.ClosableEventSink`
    once you count `close`; use `events.close_sink` rather than calling
    `close` conditionally by hand.

        with SQLiteSink("session.db") as sink:
            book = OrderBook(sink=sink)
            ...

    The context manager closes (and therefore flushes) on the way out. Outside
    one, `close` is the only thing that guarantees the tail is on disk.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
    ) -> None:
        """Open `path` and create the schema if it is not already there.

        `buffer_size` is how many events accumulate before a write. It affects
        throughput, not content.
        """
        if buffer_size < 1:
            raise ValueError(f"buffer_size must be >= 1, got {buffer_size}")
        self._buffer_size = buffer_size
        self._buffer: list[Event] = []
        self._currency: dict[str, str] = {}
        self._missing_currency: set[str] = set()
        self._error: Exception | None = None
        self._closed = False

        # isolation_level=None: no implicit transactions, so a flush is one
        # explicit BEGIN/COMMIT and nothing sits half-open between flushes.
        self._conn = sqlite3.connect(os.fspath(path), isolation_level=None)
        # The pragma reports the mode it actually got, and a filesystem may
        # refuse WAL: read the answer rather than assume it.
        row = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
        self._journal_mode = str(row[0]).lower() if row else "unknown"
        if self._journal_mode != "wal":
            _log.warning(
                "SQLiteSink asked for WAL and got journal_mode=%r: a reader "
                "sharing the file may block, and the durability of a commit "
                "is whatever this mode gives",
                self._journal_mode,
            )
        # NORMAL survives process death (the point of a recording sink); an OS
        # or power failure may still lose recent commits, which is the right
        # trade for analytics data the engine does not read back.
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._check_schema_version()
        self._conn.executescript(SCHEMA)

    # -- the sink protocol -------------------------------------------------

    def consume(self, event: Event, /) -> None:
        """Record one event.

        Does not raise on a write failure -- that is `flush`'s job, and the
        engine must not care. Does raise if the sink is closed: the event
        cannot be recorded, cannot be recovered, and cannot be reported at
        `close` either, since `close` has already happened. Accepting it would
        be exactly the silent discard this sink exists to prevent.

        The matching thread pays for one list append and, every `buffer_size`
        events, one transaction. Encoding happens in the flush, not here.
        """
        if self._closed:
            raise RuntimeError(
                f"SQLiteSink is closed: it cannot record {type(event).__name__} "
                f"at seq {event.seq}"
            )
        self._buffer.append(event)
        if len(self._buffer) >= self._buffer_size:
            with suppress(Exception):  # `flush` has already remembered it
                self.flush()

    def close(self) -> None:
        """Flush the tail, stamp the log as ended, and close the database.

        Idempotent. Raises if anything was lost -- the tail failing to write,
        or an earlier batch a `consume` could not salvage -- and raises it
        again on every later call, because a second `close` that returns
        quietly is a recording reporting itself complete when it is not.

        The `session_end` row goes in after the last flush and is what
        distinguishes this file from one whose process was killed. It is
        written even when events were lost: ending deliberately and ending
        whole are separate facts, recorded separately, and a reader is owed
        both.
        """
        if not self._closed:
            self._closed = True
            try:
                with suppress(Exception):  # `flush` has already remembered it
                    self.flush()
                self._record_end()
            finally:
                self._conn.close()
        if self._error is not None:
            raise self._error

    # -- writing -----------------------------------------------------------

    def flush(self) -> None:
        """Write everything buffered, in one transaction.

        Raises if and only if events were lost -- and remembers the error, so
        that `close` raises it too however this call's exception was handled.
        A caller who asks for a flush wants to know whether it happened, and
        the answer that matters is "is anything missing", not "did the fast
        path work": a batch that failed and was then salvaged event by event
        lost nothing and returns normally.

        The buffer is cleared either way. What could not be written is
        re-attempted immediately, one event at a time, and whatever still
        fails is recorded in `event_loss`; putting it back in the buffer would
        only grow it without bound against a write that cannot succeed.
        """
        if not self._buffer:
            return
        buffered, self._buffer = self._buffer, []
        memo = dict(self._currency)
        try:
            self._write(self._fold(buffered))
        except Exception as exc:
            # The fold's currency memo is projection state, so a write that
            # did not commit must not leave it advanced: an
            # `InstrumentConfigured` that is not in the log must not go on
            # denominating later fills. Rewound here and re-applied by the
            # salvage, one committed event at a time.
            self._currency = memo
            _log.warning(
                "SQLiteSink could not write a batch of %d events (%s: %s); "
                "re-attempting it one event at a time",
                len(buffered),
                type(exc).__name__,
                exc,
            )
            if self._salvage(buffered):
                self._remember(exc)
                raise

    def _write(self, batch: _Batch) -> None:
        """Commit one folded batch: log rows and projections, or neither."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Ordered so that every row's declared parent exists first, even
            # though foreign keys are not enforced: session and configuration,
            # then orders, then what refers to them.
            conn.executemany(_SESSION_UPSERT, batch.sessions)
            conn.executemany(_INSTRUMENT_UPSERT, batch.instruments)
            conn.executemany(_TRADER_UPSERT, batch.traders)
            for sql, rows in _runs(batch.orders):
                conn.executemany(sql, rows)
            conn.executemany(_TRADE_INSERT, batch.trades)
            conn.executemany(_BALANCE_UPSERT, batch.balances)
            conn.executemany(_LAST_PRICE_UPDATE, batch.last_prices)
            conn.executemany(_EVENT_INSERT, batch.events)
            conn.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        """Undo a failed write.

        A rollback that itself fails is logged rather than raised: the write's
        own error is the one worth reporting, and masking it with the failure
        to clean up after it would hide what went wrong.
        """
        try:
            self._conn.execute("ROLLBACK")
        except Exception as exc:
            _log.error("SQLiteSink could not roll back a failed write", exc_info=exc)

    def _salvage(self, buffered: Sequence[Event]) -> int:
        """Re-attempt a failed batch one event at a time. Returns events lost.

        One transaction per event, which is what a `buffer_size=1` sink does
        for the same events -- so this cannot write a database the ordinary
        path could not, and it costs nothing on the path that works. A poison
        event fails again here and only it is lost; a batch that failed for a
        reason outside itself (a busy database, a full disk that has since
        emptied) is written in full and nothing is lost at all.

        What still fails is recorded in `event_loss` before returning, so the
        hole is on disk whether or not this process reaches `close`.
        """
        lost: list[tuple[int, str]] = []
        for event in buffered:
            memo = dict(self._currency)
            try:
                self._write(self._fold([event]))
            except Exception as exc:
                self._currency = memo
                lost.append((event.seq, f"{type(exc).__name__}: {exc}"))
        if not lost:
            _log.warning(
                "SQLiteSink salvaged all %d events of the failed batch", len(buffered)
            )
            return 0
        _log.error(
            "SQLiteSink lost %d of %d events (seq %s); recording it in event_loss",
            len(lost),
            len(buffered),
            ", ".join(str(seq) for seq, _ in lost[:10])
            + ("..." if len(lost) > 10 else ""),
        )
        self._record_loss(lost)
        return len(lost)

    def _record_loss(self, lost: Sequence[tuple[int, str]]) -> None:
        """Write the `event_loss` rows for events that could not be written.

        Best effort by necessity: the reason the events did not write may well
        stop the marker writing too. It is still attempted, because the case
        it covers -- a process that never reaches `close` -- is the case where
        nothing else will say anything.

        A marker that cannot be written is logged and not remembered: the
        caller remembers the error that lost the events, which is the more
        useful of the two and the one `close` should raise.
        """
        now = time.time()
        rows = [
            (first, last, count, now, error)
            for first, last, count, error in _gaps(lost)
        ]
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(_LOSS_INSERT, rows)
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
        except Exception as exc:
            _log.error(
                "SQLiteSink could not record the loss of seq %d-%d",
                rows[0][0],
                rows[-1][1],
                exc_info=exc,
            )

    def _record_end(self) -> None:
        """Stamp the log as deliberately ended, with its size at that moment.

        The absence of this row is how a reader tells a killed process from a
        short session, so failing to write it is worth reporting: the file
        will read as unfinished, and the caller should hear why from the
        exception rather than infer it later from the file.

        Recording the extent as well as the fact buys the only check there is
        on rows deleted from the tail of a finished log. It costs one count
        over the log -- about 10ms per 100k events, once, at the end of a
        session that took far longer than that to record. Keeping a running
        total in Python instead would save the scan, but it would make this
        column "what the sink believes it wrote" rather than "what the file
        held", and the belief is not the thing a reader needs checked.
        """
        try:
            count, last = self._conn.execute(_LOG_EXTENT).fetchone()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(_END_INSERT, (time.time(), last, count))
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
        except Exception as exc:
            _log.error(
                "SQLiteSink could not record the end of the session: the log "
                "will read as one whose process never closed it",
                exc_info=exc,
            )
            self._remember(exc)

    def _remember(self, exc: Exception) -> None:
        """Keep the first error a loss produced. `close` re-raises it.

        Storage only: the call sites log, in the detail each of them has.
        """
        if self._error is None:
            self._error = exc

    def _fold(self, buffered: Sequence[Event]) -> _Batch:
        """Turn events into parameter rows. All encoding happens here."""
        batch = _Batch()
        for event in buffered:
            batch.events.append(
                (
                    event.seq,
                    event.KIND,
                    event.timestamp,
                    int(is_replayable(event)),
                    json.dumps(asdict(event)),
                )
            )
            self._project(event, batch)
        return batch

    def _project(self, event: Event, batch: _Batch) -> None:
        """Fold one event into the state projections."""
        match event:
            case SessionStarted():
                batch.sessions.append(
                    (
                        event.seq,
                        event.timestamp,
                        event.tick_size,
                        event.stream_version,
                    )
                )
            case InstrumentConfigured():
                self._currency[event.symbol] = event.currency
                batch.instruments.append((event.symbol, event.currency))
            case TraderConfigured():
                batch.traders.append(
                    (
                        event.tid,
                        event.name,
                        int(event.allow_self_matching),
                        event.commission_min,
                        event.commission_max_percnt,
                        event.commission_per_unit,
                    )
                )
            case Accepted():
                batch.orders.append(
                    (
                        _ORDER_INSERT,
                        (
                            event.idNum,
                            event.tid,
                            event.instrument,
                            # The currency in force now, so a later
                            # reconfiguration cannot re-denominate the
                            # commission this order has already been charged.
                            self._currency.get(event.instrument),
                            event.side,
                            event.order_type,
                            event.price,
                            event.qty,
                            event.priority,
                            event.seq,
                            event.timestamp,
                            event.seq,
                            event.timestamp,
                        ),
                    )
                )
            case Filled():
                self._project_fill(event, batch)
            case Cancelled():
                batch.orders.append(
                    (
                        _ORDER_CANCEL,
                        (
                            event.reason,
                            event.fulfilled,
                            event.seq,
                            event.timestamp,
                            event.idNum,
                        ),
                    )
                )
            case Modified():
                batch.orders.append(
                    (
                        _ORDER_MODIFY,
                        (
                            event.price,
                            event.qty,
                            event.fulfilled,
                            event.priority,
                            event.seq,
                            event.timestamp,
                            event.fulfilled,
                            event.qty,
                            event.idNum,
                        ),
                    )
                )
            case _:  # pragma: no cover - a new event kind, unhandled
                raise TypeError(f"unknown event type: {type(event).__name__}")

    def _project_fill(self, event: Filled, batch: _Batch) -> None:
        """Fold one trade: the trade row, both order rows, four movements."""
        batch.trades.append(
            (
                event.trade_id,
                event.seq,
                event.timestamp,
                event.instrument,
                event.price,
                event.qty,
                event.taker_side,
                event.bid_idNum,
                event.bid_tid,
                event.bid_fulfilled,
                event.bid_value,
                event.bid_commission,
                event.bid_commission_delta,
                event.ask_idNum,
                event.ask_tid,
                event.ask_fulfilled,
                event.ask_value,
                event.ask_commission,
                event.ask_commission_delta,
            )
        )
        for idNum, fulfilled, value, commission in (
            (
                event.bid_idNum,
                event.bid_fulfilled,
                event.bid_value,
                event.bid_commission,
            ),
            (
                event.ask_idNum,
                event.ask_fulfilled,
                event.ask_value,
                event.ask_commission,
            ),
        ):
            batch.orders.append(
                (
                    _ORDER_FILL,
                    (
                        fulfilled,
                        value,
                        commission,
                        event.seq,
                        event.timestamp,
                        fulfilled,
                        idNum,
                    ),
                )
            )

        # The balance rule, verbatim from PyLOB.events' docstring -- balances
        # are not events, and this is the one derivation every consumer
        # applies:
        #     bid side (buyer):  instrument += qty
        #                        currency   -= qty * price + bid_commission_delta
        #     ask side (seller): instrument -= qty
        #                        currency   += qty * price - ask_commission_delta
        # The commission increments are the engine's own numbers (design.md
        # decision 3); the formula that produced them lives in the engine and
        # is not repeated here.
        batch.balances.append((event.bid_tid, event.instrument, float(event.qty)))
        batch.balances.append((event.ask_tid, event.instrument, -float(event.qty)))
        currency = self._currency.get(event.instrument)
        if currency is None:
            self._warn_missing_currency(event.instrument)
        else:
            traded = event.qty * event.price
            batch.balances.append(
                (event.bid_tid, currency, -(traded + event.bid_commission_delta))
            )
            batch.balances.append(
                (event.ask_tid, currency, traded - event.ask_commission_delta)
            )

        batch.last_prices.append((event.price, event.instrument))

    def _warn_missing_currency(self, instrument: str) -> None:
        """No `InstrumentConfigured` for this symbol: half a movement is lost.

        The currency leg cannot be booked without knowing the currency. The
        `event` log still holds the fill in full, so the balance is
        recoverable; the sink says so rather than inventing a currency.
        """
        if instrument in self._missing_currency:
            return
        self._missing_currency.add(instrument)
        _log.warning(
            "no InstrumentConfigured event for %r: recording the instrument leg "
            "of its trades but not the currency leg",
            instrument,
        )

    def _check_schema_version(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            self._conn.close()
            raise ValueError(
                f"database was written by schema version {version}, "
                f"this is version {SCHEMA_VERSION}"
            )

    # -- accessors ---------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """The open connection, for querying a sink that is still running.

        Reading through it sees only what has been flushed. A `:memory:`
        database exists only as long as this connection, so anything that
        wants to query after `close` needs a file.
        """
        return self._conn

    @property
    def buffered(self) -> int:
        """Events accepted but not yet written."""
        return len(self._buffer)

    @property
    def journal_mode(self) -> str:
        """The journal mode this database actually got, lowercased.

        `wal` on any filesystem that supports it, which is what the durability
        note in the module docstring assumes. Anything else was refused, was
        warned about at open time, and means that note does not apply.
        """
        return self._journal_mode

    @property
    def error(self) -> Exception | None:
        """The first error a loss produced, or None if nothing was lost.

        `close` raises this. Reading it is how a long-running session asks
        whether its recording is still whole without ending.
        """
        return self._error

    def __enter__(self) -> SQLiteSink:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# reading a recorded stream back
# --------------------------------------------------------------------------


class EventLogError(Exception):
    """A recorded log is not a complete stream this module can read.

    Raised for a hole in `seq`, for an `event_loss` row, for rows removed
    since the session closed, and for a `stream_version` or schema
    `user_version` this module does not implement -- everything, in short,
    that would otherwise let a partial recording be read as a whole one.
    """


class IncompleteLogError(EventLogError):
    """The log is a prefix: the session that wrote it never closed.

    Suspect rather than corrupt, and the difference matters. Nothing here is
    wrong -- every event in the file was committed, and the state it describes
    is real up to the cut. What is missing is whatever the buffer still held
    when the process died, and how much that was cannot be known from the
    file. A killed run is an ordinary thing to have; reading one is a
    deliberate choice rather than an error to route around:

        try:
            check_log(path)
        except IncompleteLogError:
            pass                       # a killed run; the prefix is good
        events = read_events(path, strict=False)

    The one thing this state cannot tell you is *where* the cut is, and that
    matters beyond the lost tail: a killed run's `.db` copied without its
    `-wal` is a shorter prefix again, contiguous, unmarked, and identical in
    every readable respect to a run that stopped there (see Durability). If
    the file is a copy, the sidecars are part of it.

    Subclasses `EventLogError`, so a caller who only wants whole sessions
    needs to know none of this.
    """


def check_log(source: str | os.PathLike[str] | sqlite3.Connection) -> None:
    """Raise `EventLogError` unless `source` is a complete, readable log.

    The one place that decides whether a recording can be trusted, so that
    every reader gets the same answer. `read_events` calls it; call it
    directly to ask the question without reading the stream.

    Six ways a file fails, in order of how badly. The first five are
    corruption -- something is wrong with the file. The last is only
    incompleteness, and is raised as `IncompleteLogError` for callers who
    want to tell those apart.

    **The schema is not this one.** `PRAGMA user_version` must be
    `SCHEMA_VERSION`. Checked first, because the rest of these queries assume
    this module's tables. Version 0 with no tables in the file is reported
    separately, because it is not a version mismatch at all and the honest
    diagnosis saves a session: it is what a killed run's `.db` looks like when
    it was copied without its `-wal` sidecar (see Durability), and a reader
    told "version 0 is not version 3" goes looking for an old release of this
    module instead of for the file holding their events.

    **The sink recorded a loss.** Any `event_loss` row is a loss this sink
    knew about, wherever in the stream it fell, and it is on disk without
    anybody having to reach `close`. Checked before the arithmetic below,
    because it is the same file failing with the reason attached.

    **The log has a hole.** `seq` is the log's primary key and the engine
    emits it from 0 without gaps, so a complete log has `min(seq) == 0` and
    `max(seq) == count - 1`. Anything else lost events -- and this catches the
    losses no marker could, the ones made after the fact by whatever edited,
    truncated or partially copied the file.

    **The stream is a version this module does not implement.**
    `events.STREAM_VERSION` is bumped when an older stream would replay
    *wrongly* rather than merely incompletely, which makes reading one a
    silent-wrong-answer risk, not a compatibility inconvenience. Read from the
    log's own `SessionStarted` payload rather than the `session` projection,
    and out of the raw JSON rather than a decoded event, since decoding a
    version whose fields have changed is the thing being guarded against.

    **Rows have gone since the session closed.** `session_end` recorded what
    the log held at the moment it was written, so a log that now holds less
    has been edited. Deleting from the end leaves no gap and trips no marker;
    this is the only thing that sees it.

    **The session never closed** -- `IncompleteLogError`, and the one that is
    not corruption. See that class: the file is a good prefix of a run whose
    process was killed, and reading it is a choice rather than a mistake. It
    is also the one verdict here with a blind spot, and the blind spot is
    stated in the message rather than hidden: the checks above all compare the
    file against something the file itself carries, and an unclosed log
    carries no count to compare against. A prefix truncated further -- by
    copying the `.db` away from its `-wal`, the way it happens in practice --
    is arithmetically indistinguishable from the prefix the run left. The
    extent check below is what would catch it, and it is unreachable here
    precisely because `session_end` is what is missing.

    An empty log that was closed passes: nothing was recorded and nothing is
    missing. An empty log that was *not* closed is a session killed before it
    wrote anything, and is reported as incomplete like any other prefix.
    """
    if isinstance(source, sqlite3.Connection):
        _check_log(source)
        return
    conn = sqlite3.connect(os.fspath(source))
    try:
        _check_log(conn)
    finally:
        conn.close()


def _no_tables_message(conn: sqlite3.Connection) -> str:
    """What to say about a database file that holds nothing whatsoever.

    Schema version 0 and not a single table is the absence of a schema, not an
    old one, and the overwhelmingly likeliest way a researcher produces it is
    by copying a killed run's `.db` without the `-wal` beside it -- the one
    file that then holds every event. Naming the sidecar is the difference
    between recovering the session and concluding the data is gone.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    name = os.path.basename(row[2]) if row and row[2] else "session.db"
    return (
        f"{name} holds no tables at all: nothing was ever written to this "
        f"file, and schema version 0 is the absence of a schema rather than "
        f"an older one. If this is a copy of a recording, it was copied "
        f"without its write-ahead log: an unfinished session keeps its recent "
        f"commits in {name}-wal, so the events are there and not here. Copy "
        f"{name}, {name}-wal and {name}-shm together from wherever the run "
        f"wrote them, or checkpoint before copying -- sqlite3 {name} 'PRAGMA "
        f"wal_checkpoint(TRUNCATE)' -- and this file reads normally. If there "
        f"is no {name}-wal anywhere, then this file is not a recording"
    )


def _check_log(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        # An empty file is a different accident from an old file, and saying
        # "version" to somebody holding the first sends them nowhere.
        if version == 0 and conn.execute(_TABLE_COUNT).fetchone()[0] == 0:
            raise EventLogError(_no_tables_message(conn))
        raise EventLogError(
            f"database schema version {version} is not this module's "
            f"version {SCHEMA_VERSION}"
        )

    # Before the arithmetic, because a loss the sink recorded comes with the
    # error that caused it: the more specific diagnosis of the same file.
    losses = conn.execute(
        "SELECT first_seq, last_seq, count FROM event_loss ORDER BY first_seq"
    ).fetchall()
    if losses:
        ranges = ", ".join(f"{first}-{last}" for first, last, _ in losses)
        raise EventLogError(
            f"the sink recorded losing {sum(row[2] for row in losses)} event(s) "
            f"at seq {ranges}; see the event_loss table"
        )

    count, lowest, highest = conn.execute(
        "SELECT COUNT(*), MIN(seq), MAX(seq) FROM event"
    ).fetchone()
    if count and (lowest != 0 or highest != count - 1):
        raise EventLogError(
            f"the event log is not contiguous from 0: {count} rows spanning "
            f"seq {lowest}-{highest}, so {highest - lowest + 1 - count} event(s) "
            f"are missing from the middle and {lowest} from the start"
        )

    row = conn.execute(
        "SELECT payload FROM event WHERE kind = ? ORDER BY seq LIMIT 1",
        (SessionStarted.KIND,),
    ).fetchone()
    if row is not None:
        stream_version = json.loads(row[0]).get("stream_version")
        if stream_version != STREAM_VERSION:
            raise EventLogError(
                f"the log records stream_version {stream_version!r}, and this "
                f"module implements {STREAM_VERSION}"
            )

    # Last, because everything above is a file that is wrong, and this is a
    # file that is merely unfinished.
    ended = conn.execute(
        "SELECT last_seq, event_count FROM session_end ORDER BY rowid"
    ).fetchall()
    if not ended and count == 0:
        raise IncompleteLogError(
            f"the log has no session_end row and no events at all: the process "
            f"that wrote it was killed before its first flush, so everything it "
            f"had recorded went with the buffer. A session shorter than the "
            f"sink's buffer (default {DEFAULT_BUFFER_SIZE} events) ends this way "
            f"every time it is killed rather than closed; a smaller buffer_size, "
            f"or a flush of your own, is what makes a short episode survive one"
        )
    if not ended:
        raise IncompleteLogError(
            f"the log has no session_end row: the process that wrote it was "
            f"killed rather than closing it, so these {count} event(s) ending "
            f"at seq {highest} are a prefix of the session and not all of it. "
            f"Whatever was still buffered went with the process, and how much "
            f"that was is not knowable from the file. If this file is a copy, "
            f"check that it was copied with its -wal sidecar: a lone .db taken "
            f"from a run still in progress is a shorter prefix again, equally "
            f"contiguous, and reads exactly like this one (see Durability). The "
            f"events that are here were all committed: pass strict=False to "
            f"read them"
        )
    last_seq, event_count = ended[-1]
    if (event_count, last_seq) != (count, highest):
        raise EventLogError(
            f"the log was closed holding {event_count} event(s) ending at seq "
            f"{last_seq}, and now holds {count} ending at seq {highest}: it has "
            f"been edited since the session that wrote it finished"
        )


def decode_event(kind: str, payload: str) -> Event:
    """Rebuild the event a row of `event` was written from.

    Exact: `payload` holds every field, including the ones the projections
    also carry, so nothing is reconstructed from a summary.
    """
    event_type = EVENT_BY_KIND[kind]
    data = json.loads(payload)
    for name, enum_type in _ENUM_FIELDS.items():
        if name in data:
            data[name] = enum_type(data[name])
    return event_type(**data)  # type: ignore[no-any-return]


def read_events(
    source: str | os.PathLike[str] | sqlite3.Connection,
    *,
    replayable_only: bool = False,
    strict: bool = True,
) -> Iterator[Event]:
    """Yield a recorded stream in `seq` order.

    `source` is a database path or an open connection (the connection is left
    open; a path is closed when the iterator is exhausted or discarded).

    `replayable_only` applies the `replayable` column, which was written from
    `events.is_replayable` -- configuration events and the commands a caller
    made, without the fills and IOC cancels a replayed engine re-derives. It
    is the filter a replay wants, and it is the sanctioned rule rather than a
    second copy of it.

    `strict` runs `check_log` before the first event and refuses a log that is
    damaged or unfinished, so a replay or a comparison cannot quietly be run
    against a stream with a hole in it. Pass `strict=False` to read what a
    damaged or killed session left -- the problem is logged as a warning
    instead, and the events that are there are still exactly the events that
    were recorded.

    A caller who wants the prefix of a killed run but still refuses a corrupt
    file distinguishes the two by exception type; see `IncompleteLogError`.

    Note that the checks run when iteration starts, not when this is called:
    it is a generator.

    The completeness check is on the whole log, never on the filtered view.
    `replayable_only` skips fills, so the `seq` values it yields have gaps by
    design and prove nothing about what is missing.
    """
    if isinstance(source, sqlite3.Connection):
        yield from _read_events(source, replayable_only, strict)
        return
    conn = sqlite3.connect(os.fspath(source))
    try:
        yield from _read_events(conn, replayable_only, strict)
    finally:
        conn.close()


def _read_events(
    conn: sqlite3.Connection, replayable_only: bool, strict: bool
) -> Iterator[Event]:
    # No traceback on either: `strict=False` asked for this, so the reason is
    # the useful part and the stack it was raised from is noise.
    try:
        _check_log(conn)
    except IncompleteLogError as exc:
        if strict:
            raise
        _log.warning("reading the prefix a killed session left: %s", exc)
    except EventLogError as exc:
        if strict:
            raise
        _log.warning("reading a damaged event log: %s", exc)
    sql = "SELECT kind, payload FROM event"
    if replayable_only:
        sql += " WHERE replayable = 1"
    sql += " ORDER BY seq"
    for kind, payload in conn.execute(sql):
        yield decode_event(kind, payload)
