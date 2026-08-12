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

`consume` never raises: the sink is optional and may not take the engine down
with it. A batch that fails to write is dropped (retrying a poison batch
would grow the buffer without bound), the error is logged and remembered, and
`close` re-raises it once the tail has been written -- losing data silently
is the one outcome worse than losing the session.

If the process dies with a partial buffer, the events since the last flush
are lost and everything before them is durable. The log and the projections
can never disagree: they commit together or not at all.

Scope
-----

The sink records; it does not referee. Foreign keys are declared for
documentation and left unenforced (SQLite's default), so a hand-built or
truncated stream still records rather than erroring on the engine's behalf.
One database per session -- `seq` is the log's primary key, so pointing a
second session at a written file fails on the first flush.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from itertools import groupby
from typing import Any

from ..events import (
    EVENT_BY_KIND,
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

__all__ = ["SQLiteSink", "SCHEMA_VERSION", "SCHEMA", "decode_event", "read_events"]

_log = logging.getLogger(__name__)

#: Layout version of the tables below, stored in `PRAGMA user_version`. Bumped
#: when a written database would be read wrongly by this module. Distinct from
#: `events.STREAM_VERSION`, which versions what the engine emits: the same
#: stream can be recorded by two schema versions.
SCHEMA_VERSION = 1

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
-- orders, not over trades.
CREATE VIEW IF NOT EXISTS trader_commission AS
    SELECT o.tid AS tid, i.currency AS currency, SUM(o.commission) AS commission
    FROM orders o
    JOIN instrument i ON i.symbol = o.instrument
    GROUP BY o.tid, i.currency;
"""

_EVENT_INSERT = """
INSERT INTO event (seq, kind, timestamp, replayable, payload)
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
INSERT INTO orders (idNum, tid, instrument, side, order_type, price, qty,
                    priority, status, accepted_seq, accepted_ts,
                    last_seq, last_ts)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
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


class SQLiteSink:
    """Records an event stream into a SQLite database, in batches.

    Satisfies `events.EventSink` structurally and `events.ClosableEventSink`
    once you count `close`; use `events.close_sink` rather than calling
    `close` conditionally by hand.

        with SQLiteSink("session.db") as sink:
            book = OrderBook(sinks=[sink])
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
        self._conn.execute("PRAGMA journal_mode = WAL")
        # NORMAL survives process death (the point of a recording sink); only
        # an OS or power failure can lose a committed transaction, which is
        # the right trade for analytics data the engine does not read back.
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._check_schema_version()
        self._conn.executescript(SCHEMA)

    # -- the sink protocol -------------------------------------------------

    def consume(self, event: Event, /) -> None:
        """Record one event. Never raises; see the module docstring.

        The matching thread pays for one list append and, every `buffer_size`
        events, one transaction. Encoding happens in the flush, not here.
        """
        self._buffer.append(event)
        if len(self._buffer) >= self._buffer_size:
            try:
                self.flush()
            except Exception as exc:  # the engine must not care
                if self._error is None:
                    self._error = exc
                _log.exception("SQLiteSink dropped a batch of events")

    def close(self) -> None:
        """Flush the tail and close the database. Idempotent.

        Raises if anything was lost: the tail failing to write, or an earlier
        batch that `consume` had to drop. A partial recording that reports
        itself complete is worse than no recording.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        except Exception as exc:
            if self._error is None:
                self._error = exc
        finally:
            self._conn.close()
        if self._error is not None:
            raise self._error

    # -- writing -----------------------------------------------------------

    def flush(self) -> None:
        """Write everything buffered, in one transaction.

        Unlike `consume`, this raises: a caller who asks for a flush wants to
        know whether it happened. The buffer is cleared either way -- a batch
        that cannot be written will not be written on the next attempt
        either, and retaining it would grow the buffer without bound.
        """
        if not self._buffer:
            return
        buffered, self._buffer = self._buffer, []
        batch = self._fold(buffered)
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
            conn.execute("ROLLBACK")
            raise

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

    def __enter__(self) -> SQLiteSink:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# reading a recorded stream back
# --------------------------------------------------------------------------


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
) -> Iterator[Event]:
    """Yield a recorded stream in `seq` order.

    `source` is a database path or an open connection (the connection is left
    open; a path is closed when the iterator is exhausted or discarded).

    `replayable_only` applies the `replayable` column, which was written from
    `events.is_replayable` -- configuration events and the commands a caller
    made, without the fills and IOC cancels a replayed engine re-derives. It
    is the filter a replay wants, and it is the sanctioned rule rather than a
    second copy of it.
    """
    if isinstance(source, sqlite3.Connection):
        yield from _read_events(source, replayable_only)
        return
    conn = sqlite3.connect(os.fspath(source))
    try:
        yield from _read_events(conn, replayable_only)
    finally:
        conn.close()


def _read_events(conn: sqlite3.Connection, replayable_only: bool) -> Iterator[Event]:
    sql = "SELECT kind, payload FROM event"
    if replayable_only:
        sql += " WHERE replayable = 1"
    sql += " ORDER BY seq"
    for kind, payload in conn.execute(sql):
        yield decode_event(kind, payload)
