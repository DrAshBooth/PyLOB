"""SQLite recording sink: the event stream as queryable history.

ADR-0001 took SQLite off the matching path and kept it as an optional
recorder. This module is that recorder. It implements `events.EventSink`
structurally -- no subclassing, no registration -- and the engine neither
imports it nor knows whether it exists.

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

The schema is event-shaped, not `create_lob.sql`-shaped (design.md decision
5). Nothing here is matching state and nothing triggers: no views that
compute eligibility, no triggers that move balances. Rows are written by
Python, in one direction, after the fact.

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

One loss remains undetectable from the file alone: a lost *tail*. If the
process dies with a partial buffer, those events are gone and what survives
is a complete contiguous prefix, indistinguishable from a session that ended
there. That is the price of buffering, and it is the reason a loss on the
write path leaves a marker rather than relying on the gap it makes.

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
#: 2 added `event_loss` and `orders.currency`. A version-1 file has no way to
#: say whether anything was lost, so it is refused rather than read as though
#: an absent table meant an intact recording.
SCHEMA_VERSION = 2

#: Default events per transaction. Tuning only; design.md leaves the number to
#: benchmark data, and the resulting database does not depend on it.
DEFAULT_BUFFER_SIZE = 512

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    seq        INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL,
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
        """Flush the tail and close the database. Idempotent.

        Raises if anything was lost -- the tail failing to write, or an
        earlier batch a `consume` could not salvage -- and raises it again on
        every later call, because a second `close` that returns quietly is a
        recording reporting itself complete when it is not.
        """
        if not self._closed:
            self._closed = True
            try:
                with suppress(Exception):  # `flush` has already remembered it
                    self.flush()
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

    def _remember(self, exc: Exception) -> None:
        """Keep the first error a loss produced. `close` re-raises it."""
        _log.error("SQLiteSink lost events", exc_info=exc)
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

    Raised for a hole in `seq`, for an `event_loss` row, for a `stream_version`
    or schema `user_version` this module does not implement -- everything, in
    short, that would otherwise let a partial recording be read as a whole one.
    """


def check_log(source: str | os.PathLike[str] | sqlite3.Connection) -> None:
    """Raise `EventLogError` unless `source` is a complete, readable log.

    The one place that decides whether a recording can be trusted, so that
    every reader gets the same answer. `read_events` calls it; call it
    directly to ask the question without reading the stream.

    Four ways a file fails:

    **The schema is not this one.** `PRAGMA user_version` must be
    `SCHEMA_VERSION`. Checked first, because the rest of these queries assume
    this module's tables.

    **The sink recorded a loss.** Any `event_loss` row is a loss this sink
    knew about, wherever in the stream it fell, and it is on disk without
    anybody having to reach `close`. Checked before the arithmetic below,
    because it is the same file failing with the reason attached.

    **The log has a hole.** `seq` is the log's primary key and the engine
    emits it from 0 without gaps, so a complete log has `min(seq) == 0` and
    `max(seq) == count - 1`. Anything else lost events -- and this catches the
    losses no marker could, the ones made after the fact by whatever edited,
    truncated or partially copied the file. What it cannot see is a lost
    *tail*, which is what the marker above is for.

    **The stream is a version this module does not implement.**
    `events.STREAM_VERSION` is bumped when an older stream would replay
    *wrongly* rather than merely incompletely, which makes reading one a
    silent-wrong-answer risk, not a compatibility inconvenience. Read from the
    log's own `SessionStarted` payload rather than the `session` projection,
    and out of the raw JSON rather than a decoded event, since decoding a
    version whose fields have changed is the thing being guarded against.

    An empty log passes: nothing was recorded and nothing is missing.
    """
    if isinstance(source, sqlite3.Connection):
        _check_log(source)
        return
    conn = sqlite3.connect(os.fspath(source))
    try:
        _check_log(conn)
    finally:
        conn.close()


def _check_log(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
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

    `strict` runs `check_log` before the first event and refuses an incomplete
    or unreadable log, so a replay or a comparison cannot quietly be run
    against a stream with a hole in it. Pass `strict=False` to read what
    survived a damaged log -- the loss is logged as a warning instead, and the
    events that are there are still exactly the events that were recorded.

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
    try:
        _check_log(conn)
    except EventLogError:
        if strict:
            raise
        _log.warning("reading a damaged event log", exc_info=True)
    sql = "SELECT kind, payload FROM event"
    if replayable_only:
        sql += " WHERE replayable = 1"
    sql += " ORDER BY seq"
    for kind, payload in conn.execute(sql):
        yield decode_event(kind, payload)
