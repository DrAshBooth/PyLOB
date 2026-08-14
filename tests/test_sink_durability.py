"""Durability: the sink cannot lose an acknowledged event silently (lob-c2k).

The `SQLiteSink` docstring makes a promise -- "losing data silently is the one
outcome worse than losing the session" -- and the 2026-08 engine review
(`docs/engine-review-2026-08.md`) falsified it four ways. This suite is the
regression barrier for each, and for the reader-side checks that make a loss
visible in the file rather than only in an exception nobody was there to see.

The verified defects, and the test that holds each down:

    a failed `flush()` forgot its error, so `close()` returned cleanly over an
    empty file                          `test_a_failed_flush_is_remembered_*`

    a poison event dropped its whole 512-event batch with nothing on disk
    marking the loss -- the surviving `seq` range was contiguous, so a
    count-versus-range check passed     `test_a_poison_event_costs_only_itself`
                                        `test_a_lost_batch_is_visible_*`

    `close()` re-raised once and `consume` after `close` discarded everything
                                        `test_close_reraises_every_time`
                                        `test_consume_after_close_raises`

    a killed process left a contiguous prefix that read as a complete shorter
    session, because nothing was attempted, nothing failed, and nothing said
    the session had ended            `test_a_killed_session_is_identifiable_*`
                                        `test_a_cleanly_closed_log_is_not_*`

The last one is the case the other three structurally cannot catch: there is
no failure to record, so detection has to come from the *absence* of a mark
that only a deliberate `close` writes. It is also the one where the data is
good -- a prefix of real committed events -- which is why the suite asserts
that it reads as suspect and not as corrupt.

Two properties are asserted here rather than argued, because the fix touched
the flush path that both rest on:

    the projections are exactly the fold of the log, under the same error
    interleavings the review used       `test_the_projections_are_the_fold_*`

    `buffer_size` is a performance knob and nothing else
                                        `test_buffer_size_does_not_change_*`

How a write is made to fail
---------------------------

Two ways, and the difference between them is the point.

A **poison event** is one the schema refuses: a commission of NaN, which
Python's sqlite3 binds as NULL against a `NOT NULL` column. It is the review's
own repro, it fails identically on every attempt, and it is the case where
losing exactly one event is the best available outcome.

An **injected I/O failure** (`FailingConnection`) refuses a fixed number of
write statements and then stops. It stands in for a full disk or a locked
database: nothing is wrong with the events, so a batch that fails this way
should lose nothing at all once it is re-attempted -- and when the budget is
large enough to cover every retry, it is how a whole batch is made to vanish
without hand-editing the file.

Both reach through to `sink._conn` or `sink._buffer`. That is deliberate: the
defects are in the failure path, and the only way to a failure path that does
not involve waiting for a real disk to fill is to arrange the failure.
"""

from __future__ import annotations

import logging
import math
import random
import shutil
import sqlite3
from contextlib import suppress

import pytest
from PyLOB.engine import InvalidOrder, OrderBook
from PyLOB.events import (
    STREAM_VERSION,
    ClosableEventSink,
    EventSink,
    InstrumentConfigured,
    SessionStarted,
    TraderConfigured,
    close_sink,
)
from PyLOB.sinks import ListSink
from PyLOB.sinks.sqlite import (
    DEFAULT_BUFFER_SIZE,
    SCHEMA_VERSION,
    EventLogError,
    IncompleteLogError,
    SQLiteSink,
    check_log,
    read_events,
)

TICK = 0.01
INSTRUMENT = "FAKE"
CURRENCY = "USD"


# --------------------------------------------------------------------------
# hand-built streams
# --------------------------------------------------------------------------


def session(seq=0, stream_version=STREAM_VERSION):
    """The `SessionStarted` every stream opens with."""
    return SessionStarted(
        seq=seq, timestamp=0.0, tick_size=TICK, stream_version=stream_version
    )


def trader(seq, tid=None, commission_min=0.0):
    """A `TraderConfigured`. `commission_min=nan` makes it unwritable.

    NaN binds as NULL, and `trader.commission_min` is `NOT NULL`, so the
    projection insert raises `IntegrityError` while the `event` row itself
    would have been perfectly writable -- which is why the whole transaction
    has to roll back, and why the loss is a loss of the event.
    """
    return TraderConfigured(
        seq=seq,
        timestamp=0.0,
        tid=seq if tid is None else tid,
        name="t%d" % seq,
        allow_self_matching=False,
        commission_min=commission_min,
        commission_max_percnt=0.0,
        commission_per_unit=0.0,
    )


def poison(seq):
    """An event this schema cannot store."""
    return trader(seq, commission_min=float("nan"))


def stream(count, poison_at=()):
    """`SessionStarted` then `count - 1` traders, poisoned at `poison_at`."""
    events = [session()]
    events.extend(
        poison(seq) if seq in poison_at else trader(seq) for seq in range(1, count)
    )
    return events


# --------------------------------------------------------------------------
# a recorded engine session
# --------------------------------------------------------------------------


def build(db_path, buffer_size=DEFAULT_BUFFER_SIZE, currency=CURRENCY, meta=None):
    """A configured engine recording to `db_path`, and its sink."""
    sink = SQLiteSink(db_path, buffer_size=buffer_size, meta=meta)
    book = OrderBook(tick_size=TICK, sink=sink)
    book.configure_instrument(INSTRUMENT, currency)
    for tid in range(1, 5):
        book.configure_trader(
            tid,
            name="t%d" % tid,
            commission_min=1.0,
            commission_max_percnt=1.0,
            commission_per_unit=0.01,
        )
    return book, sink


def workload(book, rng, n_ops=400):
    """Passive and crossing limits, markets and cancels, against one book."""
    resting = []
    for _ in range(n_ops):
        roll = rng.random()
        side = rng.choice(("bid", "ask"))
        if roll < 0.15 and resting:
            idNum, order_side = resting.pop(rng.randrange(len(resting)))
            # The order may have filled since it was noted down. That refusal
            # is expected; nothing else is, so only that one is swallowed.
            with suppress(InvalidOrder):
                book.cancelOrder(order_side, idNum)
            continue
        if roll < 0.3:
            book.submit(
                tid=rng.randint(1, 4),
                instrument=INSTRUMENT,
                side=side,
                order_type="market",
                qty=rng.randint(1, 8),
            )
            continue
        # Passive below the touch on the bid and above it on the ask; crossing
        # the other way. Both, so the run makes trades as well as depth.
        offset = rng.uniform(-1.0, 1.0)
        price = round(100.0 + (offset if side == "bid" else -offset), 2)
        order, _ = book.submit(
            tid=rng.randint(1, 4),
            instrument=INSTRUMENT,
            side=side,
            order_type="limit",
            qty=rng.randint(1, 10),
            price=price,
        )
        resting.append((order.idNum, side))


# --------------------------------------------------------------------------
# reading a database back
# --------------------------------------------------------------------------

#: Every table whose content two runs of the same workload must agree on,
#: with the key that puts it in a canonical order. Sorting by key rather than
#: by rowid is what makes a comparison of two databases a comparison of their
#: content: insertion order is the thing `buffer_size` is allowed to change,
#: and the content is the thing it is not.
#:
#: `session_end` is deliberately absent: it carries a wall clock, which no two
#: runs share and which is not content. `ended()` reads the parts of it that
#: are. `session_meta` is absent for a different reason -- it is the caller's
#: provenance rather than anything the log implies, so two runs of the same
#: workload need not carry the same metadata and a re-fold of the log cannot
#: reproduce any. `meta_rows()` reads it where it is the point.
TABLES = {
    "event": "seq",
    "event_loss": "first_seq",
    "session": "seq",
    "instrument": "symbol",
    "trader": "tid",
    "orders": "idNum",
    "trade": "trade_id",
    "balance": "tid, symbol",
}

#: The projections only: what the log is folded into.
PROJECTIONS = tuple(t for t in TABLES if t not in ("event", "event_loss"))


def dump(path, tables=TABLES):
    """Every row of `tables`, keyed by table and in canonical order."""
    conn = sqlite3.connect(path)
    try:
        return {
            table: conn.execute(
                "SELECT * FROM %s ORDER BY %s" % (table, TABLES[table])
            ).fetchall()
            for table in tables
        }
    finally:
        conn.close()


def seqs(path):
    """The `seq` values actually on disk, in order."""
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT seq FROM event ORDER BY seq")]
    finally:
        conn.close()


def losses(path):
    """The `event_loss` rows: `(first_seq, last_seq, count)`."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT first_seq, last_seq, count FROM event_loss ORDER BY first_seq"
        ).fetchall()
    finally:
        conn.close()


def ended(path):
    """The `session_end` rows: `(last_seq, event_count)`. Empty means killed."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT last_seq, event_count FROM session_end ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


def meta_rows(path):
    """`session_meta` as the file holds it, with SQLite's own type for each.

    The type is read as well as the value because "values keep their types" is
    the claim: an untyped column and SQLite's dynamic typing are what make
    `meta={"seed": 42}` come back as `42` rather than as `'42'`.
    """
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT key, value, typeof(value) FROM session_meta ORDER BY key"
        ).fetchall()
    finally:
        conn.close()


def refold(source, target):
    """Feed the log at `source` into a fresh sink at `target`, event by event.

    The fold of the log, computed by the same code that wrote the original but
    with no memory of the session -- so comparing the projections either side
    of this is asking whether the projections hold anything the log does not.
    """
    events = list(read_events(source, strict=False))
    sink = SQLiteSink(target, buffer_size=1)
    for event in events:
        sink.consume(event)
    sink.close()
    return events


# --------------------------------------------------------------------------
# making a write fail
# --------------------------------------------------------------------------


class FailingConnection:
    """Wraps a connection; the next `fail_writes` `executemany` calls raise.

    `execute` is passed straight through, so BEGIN, COMMIT and ROLLBACK still
    work -- the sink's transaction handling is what is under test, not
    sqlite's. Every `_write` issues its first `executemany` unconditionally,
    so one whole write attempt costs exactly one unit of the budget: a batch
    of `n` events needs `1 + n` to fail through the batch and every one of its
    salvage retries, and leaves the `event_loss` write to succeed.
    """

    def __init__(self, conn):
        self._conn = conn
        self.fail_writes = 0

    def executemany(self, sql, rows):
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise sqlite3.OperationalError("disk I/O error (injected)")
        return self._conn.executemany(sql, rows)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def fail_next_batch(sink, size, and_the_marker=False):
    """Arrange for the next flush of `size` events to lose all of them.

    `and_the_marker` spends one more unit of the budget on the `event_loss`
    write, which is the disk that is still full when the sink tries to say so.
    """
    if not isinstance(sink._conn, FailingConnection):
        sink._conn = FailingConnection(sink._conn)
    sink._conn.fail_writes = 1 + size + bool(and_the_marker)


# --------------------------------------------------------------------------
# (a) a failed flush must not forget
# --------------------------------------------------------------------------


def test_a_failed_flush_is_remembered_and_close_reraises_it(tmp_path):
    """The review's verified repro, now ending the only way it may.

    Before the fix: `flush()` raised, `buffered` went to 0, `_error` stayed
    None, `close()` returned cleanly, and the file held zero events. Every one
    of those but the raise was a lie about what had been recorded.
    """
    path = tmp_path / "flush.db"
    sink = SQLiteSink(path, buffer_size=100)
    for event in [session(), InstrumentConfigured(1, 0.0, INSTRUMENT, CURRENCY)]:
        sink.consume(event)
    sink.consume(poison(2))

    with pytest.raises(sqlite3.IntegrityError):
        sink.flush()

    # The buffer is still cleared -- a batch that cannot be written will not
    # write on the next attempt either -- but the error is no longer forgotten.
    assert sink.buffered == 0
    assert isinstance(sink.error, sqlite3.IntegrityError)

    # And the two innocent events did not go down with the poison one.
    assert seqs(path) == [0, 1]
    assert losses(path) == [(2, 2, 1)]

    with pytest.raises(sqlite3.IntegrityError):
        sink.close()


def test_a_flush_that_loses_nothing_does_not_raise(tmp_path):
    """A transient failure costs nothing, and says nothing happened.

    `flush` raises if and only if events were lost. One refused write attempt
    against events that are themselves fine is re-attempted per event and
    every one of them lands, so there is nothing to report and nothing in
    `event_loss`.
    """
    path = tmp_path / "transient.db"
    sink = SQLiteSink(path, buffer_size=100)
    for event in stream(6):
        sink.consume(event)
    sink._conn = FailingConnection(sink._conn)
    sink._conn.fail_writes = 1  # the batch write only; the retries succeed

    sink.flush()

    assert sink.error is None
    assert seqs(path) == [0, 1, 2, 3, 4, 5]
    assert losses(path) == []
    sink.close()
    check_log(path)


# --------------------------------------------------------------------------
# (b) a poison batch must not take 511 innocent events with it
# --------------------------------------------------------------------------


def test_a_poison_event_costs_only_itself(tmp_path):
    """The review's shape exactly: one bad event in a default-sized batch.

    It cost 512 events, `SessionStarted` and every configuration event among
    them. It now costs one, and the one is named on disk.
    """
    path = tmp_path / "poison.db"
    sink = SQLiteSink(path)  # the default buffer_size the review used
    assert sink.buffered == 0
    for event in stream(DEFAULT_BUFFER_SIZE, poison_at={300}):
        sink.consume(event)

    written = seqs(path)
    assert written == [seq for seq in range(DEFAULT_BUFFER_SIZE) if seq != 300]
    assert written[0] == 0, "SessionStarted survived a poison event 300 places later"
    assert losses(path) == [(300, 300, 1)]
    with pytest.raises(sqlite3.IntegrityError):
        sink.close()


def test_scattered_poison_is_recorded_as_the_runs_it_makes(tmp_path):
    """Every lost `seq` is named: adjacent ones share a row, others do not."""
    path = tmp_path / "scattered.db"
    sink = SQLiteSink(path, buffer_size=32)
    for event in stream(32, poison_at={4, 5, 6, 20}):
        sink.consume(event)

    assert losses(path) == [(4, 6, 3), (20, 20, 1)]
    assert 4 not in seqs(path) and 7 in seqs(path)
    with pytest.raises(sqlite3.IntegrityError):
        sink.close()


@pytest.mark.parametrize("where", ("first", "middle", "last"))
def test_a_lost_batch_is_visible_in_the_file_without_close(tmp_path, where):
    """A whole batch vanishes and the file says so -- with `close` never called.

    This is the finding's sharpest edge. `close()` did re-raise for a dropped
    batch, but a recording sink exists to survive the process not reaching
    `close()`, and the file recorded nothing about the loss. The review's own
    case is `where="first"`: seq 0-511 gone, the survivors *contiguous*, so a
    count-versus-range check passes and only `min(seq) != 0` gives it away.

    `where="last"` is the case no arithmetic can catch -- the survivors run
    from 0 with no gap at all -- and is why the loss marker exists rather than
    leaving detection to the shape of what is left.
    """
    path = tmp_path / ("batch-%s.db" % where)
    size = 8
    sink = SQLiteSink(path, buffer_size=size)
    batches = 3
    fail_batch = {"first": 0, "middle": 1, "last": 2}[where]
    events = stream(size * batches)

    for index, event in enumerate(events):
        if index % size == 0 and index // size == fail_batch:
            fail_next_batch(sink, size)
        sink.consume(event)

    lost = set(range(fail_batch * size, (fail_batch + 1) * size))
    survivors = seqs(path)
    assert survivors == [seq for seq in range(size * batches) if seq not in lost]
    assert losses(path) == [(min(lost), max(lost), size)]

    # Nobody has called close(). The file alone has to give the loss up, and
    # it names the range rather than merely reporting that the shape is odd.
    with pytest.raises(EventLogError) as refusal:
        check_log(path)
    assert "event_loss" in str(refusal.value)
    assert "%d-%d" % (min(lost), max(lost)) in str(refusal.value)
    with pytest.raises(EventLogError):
        list(read_events(path))

    # And what the review relied on -- that the survivors look intact -- is
    # exactly what happens for a lost first or last batch.
    if where in ("first", "last"):
        assert survivors == list(range(survivors[0], survivors[-1] + 1)), (
            "the surviving range is contiguous, so counting rows proves nothing"
        )

    # strict=False is how a damaged recording is still read for forensics.
    assert len(list(read_events(path, strict=False))) == len(survivors)


def orphan_trades(path):
    """Trade rows naming an order with no row in `orders`."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM trade t WHERE NOT EXISTS "
            "(SELECT 1 FROM orders o WHERE o.idNum = t.bid_idNum) "
            "OR NOT EXISTS (SELECT 1 FROM orders o WHERE o.idNum = t.ask_idNum)"
        ).fetchone()[0]
    finally:
        conn.close()


def order_ids(path):
    """The identifiers the `orders` projection holds."""
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT idNum FROM orders")}
    finally:
        conn.close()


def test_a_clean_recording_has_no_orphan_projection_rows(tmp_path):
    """Nothing lost, nothing dangling: the baseline the next test departs from."""
    path = tmp_path / "whole.db"
    book, _ = build(path, buffer_size=64)
    workload(book, random.Random(11), n_ops=320)
    book.close()
    check_log(path)
    assert orphan_trades(path) == 0


def test_a_lost_batch_leaves_nothing_the_log_cannot_explain(tmp_path):
    """The review's second half: a dropped batch left the projections broken.

    Trade rows referenced orders with no row and balances were booked for
    them, because `UPDATE ... WHERE idNum = ?` matching nothing is silent.
    Losing the `Accepted` and keeping the fills it led to is *unavoidable*
    once an event is gone -- the fills are real and later events are not
    rewritten. What was wrong was that it happened with nothing on disk
    saying so.

    So the guarantee is not "no orphans". It is that the `orders` projection
    holds exactly the orders the log accepted -- no more and no fewer -- and
    that the file refuses to be read as complete. An orphan is then a visible
    consequence of a declared loss instead of a silent corruption.
    """
    path = tmp_path / "orphans.db"
    book, sink = build(path, buffer_size=64)
    rng = random.Random(11)
    workload(book, rng, n_ops=120)
    fail_next_batch(sink, 64)
    workload(book, rng, n_ops=200)
    with pytest.raises(sqlite3.OperationalError):
        book.close()

    assert losses(path), "the run did not actually lose a batch"
    assert orphan_trades(path) > 0, "the lost batch did not include an Accepted"

    accepted = {
        event.idNum
        for event in read_events(path, strict=False)
        if event.KIND == "accepted"
    }
    assert order_ids(path) == accepted, (
        "the orders projection is not exactly what the log accepted"
    )
    with pytest.raises(EventLogError, match="event_loss"):
        check_log(path)


def test_a_marker_that_cannot_be_written_still_leaves_the_hole_visible(tmp_path):
    """The marker is best effort: what stopped the events can stop it too.

    Then there are two lines of defence left, and both hold. `close` raises --
    with the error that lost the events, not the secondary one from failing to
    describe the loss -- and the log has a gap in `seq` that `check_log` finds
    without any marker to read.
    """
    path = tmp_path / "nomarker.db"
    size = 4
    sink = SQLiteSink(path, buffer_size=size)
    events = stream(size * 2)
    for index, event in enumerate(events):
        if index == size:
            fail_next_batch(sink, size, and_the_marker=True)
        sink.consume(event)

    assert losses(path) == [], "this test is pointless if the marker got written"
    assert seqs(path) == [0, 1, 2, 3]
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        sink.close()


# --------------------------------------------------------------------------
# (c) close, and life after it
# --------------------------------------------------------------------------


def test_close_reraises_every_time(tmp_path):
    """`_closed` was set before the error check, so the second close was quiet.

    A caller who closes in a `finally` and again in a context manager exit saw
    the loss once, or -- depending which ran first -- not at all.
    """
    path = tmp_path / "twice.db"
    sink = SQLiteSink(path, buffer_size=100)
    sink.consume(session())
    sink.consume(poison(1))

    errors = []
    for _ in range(3):
        with pytest.raises(sqlite3.IntegrityError) as raised:
            sink.close()
        errors.append(raised.value)
    assert errors[0] is errors[1] is errors[2], "each close reported the same loss"


def test_a_clean_close_stays_idempotent(tmp_path):
    """Closing twice with nothing lost is still a no-op, as it always was."""
    path = tmp_path / "clean.db"
    sink = SQLiteSink(path, buffer_size=100)
    for event in stream(4):
        sink.consume(event)
    sink.close()
    sink.close()
    check_log(path)
    assert seqs(path) == [0, 1, 2, 3]


def test_consume_after_close_raises(tmp_path):
    """Events after `close` used to be appended to a buffer nothing would flush.

    The engine stayed usable and every event it emitted went nowhere, with no
    error anywhere and no trace in the file. There is nothing left that could
    report this later, so it is reported now.
    """
    path = tmp_path / "after.db"
    sink = SQLiteSink(path, buffer_size=100)
    sink.consume(session())
    sink.close()

    with pytest.raises(RuntimeError, match="closed"):
        sink.consume(trader(1))
    assert sink.buffered == 0
    assert seqs(path) == [0]


def test_a_closed_sink_stops_the_engine_rather_than_swallowing_its_events(tmp_path):
    """The same rule reached through the engine, which is how it will be hit."""
    path = tmp_path / "engine-after.db"
    book, _ = build(path, buffer_size=100)
    book.close()
    with pytest.raises(RuntimeError, match="closed"):
        book.submit(
            tid=1,
            instrument=INSTRUMENT,
            side="bid",
            order_type="limit",
            qty=1,
            price=100.0,
        )


# --------------------------------------------------------------------------
# (d) a session that was killed rather than closed
# --------------------------------------------------------------------------


def killed_session(path, buffer_size=4, n_orders=20, close=False, meta=None):
    """Record `n_orders` and walk away without closing. Returns the sink.

    What a killed process leaves behind: everything flushed is committed, and
    whatever the buffer still held is gone with no trace of having existed.
    Nothing was attempted and nothing failed, so there is no `event_loss` row
    and the surviving `seq` run from 0 with no gap.
    """
    book, sink = build(path, buffer_size=buffer_size, meta=meta)
    for index in range(n_orders):
        book.submit(
            tid=1 + index % 4,
            instrument=INSTRUMENT,
            side="bid",
            order_type="limit",
            qty=1,
            price=round(100.0 - index * 0.01, 2),
        )
    if close:
        book.close()
    return sink


def test_a_killed_session_is_identifiable_as_incomplete(tmp_path):
    """The gap the two loss mechanisms structurally cannot see.

    No `event_loss` row, because nothing was ever attempted. A contiguous
    `seq` range from 0, because the events that made it are all there. So the
    file reads as a complete *shorter* session, and somebody recording a run
    to study afterwards would study a prefix believing it whole.

    Only the absence of the `session_end` row distinguishes it, and a crash
    cannot forge one.
    """
    path = tmp_path / "killed.db"
    sink = killed_session(path)

    assert sink.buffered > 0, "nothing was actually lost with the process"
    assert losses(path) == [], "a killed process leaves no loss row: it is the point"
    written = seqs(path)
    assert written == list(range(len(written))), "and the survivors look intact"

    with pytest.raises(IncompleteLogError) as refusal:
        check_log(path)
    assert "session_end" in str(refusal.value)
    with pytest.raises(IncompleteLogError):
        list(read_events(path))

    # The prefix itself is good, and reading it is one flag away.
    assert len(list(read_events(path, strict=False))) == len(written)


def test_a_killed_db_copied_without_its_wal_names_the_wal(tmp_path):
    """The one way a careful researcher loses a killed run to a misdiagnosis.

    In WAL mode a live recording is up to three files, and a killed process
    checkpoints none of them: the `.db` is an empty shell while every
    committed event sits in the `-wal` beside it. Copy the `.db` alone -- the
    obvious thing to do with something named `session.db` -- and the file that
    arrives has no schema at all, which the version check reported as "schema
    version 0 is not this module's version N". That sends a reader looking for
    an old release of this module, when what they need is a file they left
    behind, and the run is recoverable the whole time they are not looking.
    """
    live = tmp_path / "live"
    live.mkdir()
    path = live / "killed.db"
    sink = killed_session(path)
    assert (live / "killed.db-wal").exists(), "WAL mode, and nothing checkpointed"

    alone = tmp_path / "alone"
    alone.mkdir()
    copied = alone / "killed.db"
    shutil.copyfile(path, copied)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        seqs(copied)  # the copy is a shell: not one event came with it

    with pytest.raises(EventLogError) as refusal:
        check_log(copied)
    message = str(refusal.value)
    assert "is not this module's version" not in message, "not a version mismatch"
    assert "no tables" in message
    assert "killed.db-wal" in message, "the file actually holding the events"
    assert "killed.db-shm" in message

    # And the advice is good: with the sidecar, the same copy is the killed
    # run it always was, prefix and all.
    for suffix in ("-wal", "-shm"):
        shutil.copyfile(f"{path}{suffix}", f"{copied}{suffix}")
    with pytest.raises(IncompleteLogError):
        check_log(copied)
    assert len(list(read_events(copied, strict=False))) == len(seqs(path))
    assert sink.buffered > 0, "the live sink was never closed behind our backs"


def test_a_checkpointed_db_copied_alone_is_silently_shorter(tmp_path):
    """The same bad copy once SQLite has checkpointed, and it is worse.

    Face one of this trap is loud: no checkpoint has happened, the lone `.db`
    has no tables, and the message above names the `-wal`. Once SQLite has
    auto-checkpointed -- which it does on its own, around 1000 pages, so any
    session worth recording gets there -- the lone copy is instead a *valid*
    log: schema intact, `seq` contiguous from 0, no `event_loss` row, no
    `session_end` row. It is a killed run that stopped early, and there is no
    way to tell it from a killed run that really did stop there. Reproduced
    against a real auto-checkpoint at 2168 of 2400 events; the checkpoint here
    is triggered by hand so the test is deterministic and fast.

    So this test asserts a limitation, not a defence. It exists to keep the
    limitation *stated*: `check_log` reaches its extent comparison only when a
    `session_end` row is there to compare against, and that row is exactly
    what a truncated copy has lost -- the marker rides the same WAL as the
    events, and a checkpoint copies a prefix of it. Nothing the sink writes
    can survive a cut that also takes the writing. The message is the only
    place the warning can live, so the message has to carry it.
    """
    live = tmp_path / "live"
    live.mkdir()
    path = live / "killed.db"
    book, sink = build(path, buffer_size=4)
    for index in range(20):
        book.submit(
            tid=1,
            instrument=INSTRUMENT,
            side="bid",
            order_type="limit",
            qty=1,
            price=round(100.0 - index * 0.01, 2),
        )
    sink.flush()
    # What SQLite's auto-checkpoint does on its own once the WAL is big
    # enough: move the committed prefix into the `.db` and leave the rest.
    sink._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    for index in range(20, 40):
        book.submit(
            tid=1,
            instrument=INSTRUMENT,
            side="bid",
            order_type="limit",
            qty=1,
            price=round(90.0 - index * 0.01, 2),
        )
    sink.flush()

    copied = tmp_path / "copied.db"
    shutil.copyfile(path, copied)

    truncated, whole = seqs(copied), seqs(path)
    assert 0 < len(truncated) < len(whole), "the copy silently lost the tail"
    assert truncated == list(range(len(truncated))), "and is contiguous from 0"
    assert losses(copied) == [], "nothing failed, so nothing recorded a failure"
    assert ended(copied) == [], "and the run was never closed"

    # Every readable fact about the copy is a fact about a shorter killed run,
    # so this is the verdict -- the same one the whole file would earn.
    with pytest.raises(IncompleteLogError) as refusal:
        check_log(copied)
    with pytest.raises(IncompleteLogError):
        check_log(path)
    assert "-wal" in str(refusal.value), (
        "the message is the only thing that can warn about this, since the file cannot"
    )
    assert sink.buffered == 0


def test_a_cleanly_closed_log_is_not_flagged(tmp_path):
    """The same run, closed. Nothing about it is suspect."""
    path = tmp_path / "closed.db"
    sink = killed_session(path, close=True)

    assert sink.buffered == 0
    check_log(path)
    written = seqs(path)
    assert len(list(read_events(path))) == len(written)
    assert ended(path) == [(written[-1], len(written))]


def test_incomplete_is_a_distinguishable_state_not_corruption(tmp_path):
    """Suspect and corrupt have to read differently to whoever hits them.

    A killed run is normal and its data is good up to the cut; a log with a
    hole in it is not. Both refuse by default, but a caller can tell them
    apart by type -- and the recipe in `IncompleteLogError`'s docstring, for
    reading a killed run while still refusing a damaged one, is what is
    checked here.
    """
    unfinished = tmp_path / "unfinished.db"
    killed_session(unfinished)

    damaged = tmp_path / "damaged.db"
    sink = SQLiteSink(damaged, buffer_size=32)
    for event in stream(32, poison_at={7}):
        sink.consume(event)
    with pytest.raises(sqlite3.IntegrityError):
        sink.close()

    # Incompleteness is an EventLogError, so a caller who only wants whole
    # sessions needs to know nothing about the distinction.
    assert issubclass(IncompleteLogError, EventLogError)

    def read_allowing_a_killed_run(path):
        try:
            check_log(path)
        except IncompleteLogError:
            pass  # a killed run; the prefix is good
        return list(read_events(path, strict=False))

    assert read_allowing_a_killed_run(unfinished)
    with pytest.raises(EventLogError) as refusal:
        read_allowing_a_killed_run(damaged)
    assert not isinstance(refusal.value, IncompleteLogError), (
        "a damaged log must not be mistaken for a merely unfinished one"
    )


def test_a_killed_session_that_also_lost_events_reports_the_loss(tmp_path):
    """Corruption outranks incompleteness: the worse fact is the one reported.

    Both are true of this file, and the loss is the one with an error attached
    and the one that means the events are gone rather than merely unfinished.
    """
    path = tmp_path / "killed-lossy.db"
    sink = SQLiteSink(path, buffer_size=4)
    events = stream(12)
    for index, event in enumerate(events):
        if index == 4:
            fail_next_batch(sink, 4)
        sink.consume(event)
    # Killed here: no close, so both an event_loss row and no session_end row.
    assert losses(path) and ended(path) == []

    with pytest.raises(EventLogError, match="event_loss") as refusal:
        check_log(path)
    assert not isinstance(refusal.value, IncompleteLogError)


def test_the_end_row_is_written_even_when_events_were_lost(tmp_path):
    """Ending deliberately and ending whole are separate facts.

    A reader is owed both, so a lossy session that did reach `close` says so
    -- otherwise "lost a batch" and "was killed" would be indistinguishable,
    and they call for different responses.
    """
    path = tmp_path / "closed-lossy.db"
    sink = SQLiteSink(path, buffer_size=8)
    for event in stream(16, poison_at={3}):
        sink.consume(event)
    with pytest.raises(sqlite3.IntegrityError):
        sink.close()

    assert losses(path) == [(3, 3, 1)]
    assert ended(path), "a session that closed must say so even having lost events"


def test_an_unclosed_empty_log_is_incomplete(tmp_path):
    """Killed before writing anything is still killed, not an empty session.

    The ordinary outcome for short episodes at the default buffer, so the
    message has to name the buffer rather than report zero events and stop:
    the reader's next question is why the file is empty, and the answer is a
    setting they can change.
    """
    path = tmp_path / "stillborn.db"
    SQLiteSink(path, buffer_size=100).consume(session())
    with pytest.raises(IncompleteLogError) as refusal:
        check_log(path)
    assert "buffer_size" in str(refusal.value)
    assert str(DEFAULT_BUFFER_SIZE) in str(refusal.value)
    assert "seq None" not in str(refusal.value), "there is no last seq to name"


def test_rows_deleted_from_the_end_of_a_closed_log_are_caught(tmp_path):
    """The one edit no gap and no marker could ever reveal.

    Deleting from the tail of a finished log leaves `seq` contiguous from 0
    and trips nothing else. `session_end` recorded the size at close, so the
    log can be asked whether it still holds what it finished holding.
    """
    path = tmp_path / "trimmed.db"
    book, _ = build(path, buffer_size=64)
    workload(book, random.Random(3), n_ops=200)
    book.close()
    check_log(path)
    before = seqs(path)

    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM event WHERE seq > ?", (before[-5],))
        conn.commit()
    finally:
        conn.close()

    trimmed = seqs(path)
    assert trimmed == list(range(len(trimmed))), "the survivors are contiguous from 0"
    assert losses(path) == []
    with pytest.raises(EventLogError, match="edited since") as refusal:
        check_log(path)
    assert not isinstance(refusal.value, IncompleteLogError)


# --------------------------------------------------------------------------
# the reader refuses what it cannot vouch for
# --------------------------------------------------------------------------


def test_a_future_stream_version_is_refused(tmp_path):
    """`events.py` claimed a replayer refuses a version it does not implement.

    No check existed anywhere: the review set `stream_version` to 99 and the
    stream replayed happily. The claim is now true of the reader every replay
    in this repo goes through.
    """
    path = tmp_path / "future.db"
    sink = SQLiteSink(path, buffer_size=100)
    sink.consume(session(stream_version=99))
    sink.consume(trader(1))
    sink.close()

    with pytest.raises(EventLogError, match="stream_version"):
        check_log(path)
    with pytest.raises(EventLogError, match="stream_version"):
        list(read_events(path))
    with pytest.raises(EventLogError, match="stream_version"):
        list(read_events(path, replayable_only=True))


def test_the_current_stream_version_is_accepted(tmp_path):
    """The refusal is of versions this module does not implement, not of all."""
    path = tmp_path / "current.db"
    sink = SQLiteSink(path, buffer_size=100)
    sink.consume(session(stream_version=STREAM_VERSION))
    sink.close()
    check_log(path)


def test_a_truncated_log_is_refused(tmp_path):
    """The review deleted 10 mid-stream `Accepted` rows and replay said nothing.

    Nothing later referenced the missing identifiers, so no constraint and no
    decode complained -- and 17 of 20 balances came out different. `seq` is
    the log's primary key and the engine emits it from 0 without gaps, so the
    row count and the range are enough to know rows are missing, however they
    went.
    """
    path = tmp_path / "truncated.db"
    book, _ = build(path, buffer_size=64)
    workload(book, random.Random(7), n_ops=300)
    book.close()
    check_log(path)
    intact = len(seqs(path))

    conn = sqlite3.connect(path)
    try:
        victims = [
            row[0]
            for row in conn.execute(
                "SELECT seq FROM event WHERE kind = 'accepted' "
                "ORDER BY seq LIMIT 10 OFFSET 20"
            )
        ]
        conn.executemany("DELETE FROM event WHERE seq = ?", [(s,) for s in victims])
        conn.commit()
    finally:
        conn.close()
    assert len(victims) == 10

    with pytest.raises(EventLogError, match="contiguous"):
        check_log(path)
    with pytest.raises(EventLogError):
        list(read_events(path, replayable_only=True))
    assert len(list(read_events(path, strict=False))) == intact - 10


def test_a_missing_prefix_is_refused_even_though_the_rest_is_contiguous(tmp_path):
    """`min(seq) != 0` was the only tell the review found, and nothing read it."""
    path = tmp_path / "prefix.db"
    sink = SQLiteSink(path, buffer_size=100)
    for event in stream(20):
        sink.consume(event)
    sink.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM event WHERE seq < 5")
        conn.commit()
    finally:
        conn.close()

    assert seqs(path) == list(range(5, 20)), "the survivors are contiguous"
    with pytest.raises(EventLogError, match="contiguous"):
        check_log(path)


@pytest.mark.parametrize("version", (1, SCHEMA_VERSION + 1))
def test_a_foreign_schema_version_is_refused(tmp_path, version):
    """A file this module did not write cannot be vouched for either.

    A version-1 database has no `event_loss` table, so reading one would mean
    treating "this file cannot record a loss" as "this file lost nothing". A
    version this module has not been written for yet is the same problem from
    the other end: nothing here knows what it would have to check.

    Both are named outright rather than reached as `SCHEMA_VERSION - 1`, which
    is a different question: ADR-0007 decided that the readers accept a
    *window* down to `MIN_READABLE_SCHEMA_VERSION`, so the version immediately
    below this one is on its way to being read rather than refused. These two
    sit outside any window, which is what this test is about.
    """
    path = tmp_path / "old.db"
    sink = SQLiteSink(path, buffer_size=100)
    sink.consume(session())
    sink.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA user_version = %d" % version)
    finally:
        conn.close()

    with pytest.raises(EventLogError, match="schema version"):
        check_log(path)


def test_an_empty_log_is_not_a_damaged_one(tmp_path):
    """Nothing recorded and nothing missing is a complete log of nothing."""
    path = tmp_path / "empty.db"
    SQLiteSink(path).close()
    check_log(path)
    assert list(read_events(path)) == []


def test_a_healthy_recording_still_reads(tmp_path):
    """The checks must not refuse the thing they exist to protect."""
    path = tmp_path / "healthy.db"
    book, _ = build(path, buffer_size=17)
    workload(book, random.Random(5), n_ops=200)
    book.close()

    check_log(path)
    events = list(read_events(path))
    assert [event.seq for event in events] == list(range(len(events)))
    assert len(list(read_events(path, replayable_only=True))) < len(events)


# --------------------------------------------------------------------------
# the properties the fix must not have broken
# --------------------------------------------------------------------------


@pytest.mark.parametrize("interleaving", ("clean", "poison", "lost_batch"))
def test_the_projections_are_the_fold_of_the_log(tmp_path, interleaving):
    """Whatever is lost, the projections hold nothing the log does not.

    The claim the module docstring makes -- "the log and the projections can
    never disagree" -- checked the only way it can be: read the recorded log
    back, fold it into a fresh database with no memory of the session, and
    compare every projection table. A trade row for an order the log never
    accepted, or a balance the log cannot explain, shows up here as a
    difference.

    Run under the review's own error interleavings, because that is where the
    old code failed it: a dropped batch left trades referring to orders with
    no row, and balances booked for them.
    """
    path = tmp_path / ("fold-%s.db" % interleaving)
    book, sink = build(path, buffer_size=64)
    rng = random.Random(29)
    workload(book, rng, n_ops=150)

    if interleaving == "poison":
        # A poison event mid-session, in the engine's own stream.
        sink.consume(poison(10_000))
    elif interleaving == "lost_batch":
        fail_next_batch(sink, 64)

    workload(book, rng, n_ops=250)
    if interleaving == "clean":
        book.close()
    else:
        with pytest.raises(Exception):
            book.close()

    if interleaving == "clean":
        assert losses(path) == []
    else:
        assert losses(path), "the interleaving did not actually lose anything"

    recorded = dump(path, PROJECTIONS)
    refolded_path = tmp_path / ("refold-%s.db" % interleaving)
    refold(path, refolded_path)
    assert dump(refolded_path, PROJECTIONS) == recorded


@pytest.mark.parametrize("buffer_size", (1, 7, 100_000))
def test_buffer_size_does_not_change_the_database(tmp_path, buffer_size):
    """ "A performance knob and nothing else" -- re-verified after the fix.

    The review verified this at 1, 7 and 100000 before the flush path was
    touched; the salvage path is a `buffer_size=1` write of a failed batch, so
    the claim is now load-bearing for correctness and not only for tuning.
    Compared row by row in key order rather than byte by byte: page layout is
    not content, and insertion order is precisely what batching may change.
    """
    reference = tmp_path / "reference.db"
    book, _ = build(reference, buffer_size=DEFAULT_BUFFER_SIZE)
    workload(book, random.Random(97), n_ops=400)
    book.close()

    path = tmp_path / ("buffered-%d.db" % buffer_size)
    book, _ = build(path, buffer_size=buffer_size)
    workload(book, random.Random(97), n_ops=400)
    book.close()

    assert dump(path) == dump(reference)


def test_salvaging_writes_what_a_buffer_of_one_would_have(tmp_path):
    """The salvage path is not a second writer with its own opinions.

    Same events, one batch that fails and is salvaged versus a sink that never
    batched at all: the surviving rows have to match, or the failure path
    would be a way to end up with a database the ordinary path cannot produce.
    """
    events = stream(24, poison_at={9})

    salvaged = tmp_path / "salvaged.db"
    sink = SQLiteSink(salvaged, buffer_size=24)
    for event in events:
        sink.consume(event)
    with pytest.raises(sqlite3.IntegrityError):
        sink.close()

    unbatched = tmp_path / "unbatched.db"
    sink = SQLiteSink(unbatched, buffer_size=1)
    for event in events:
        sink.consume(event)
    with pytest.raises(sqlite3.IntegrityError):
        sink.close()

    assert dump(salvaged, PROJECTIONS) == dump(unbatched, PROJECTIONS)
    assert seqs(salvaged) == seqs(unbatched)
    assert losses(salvaged) == losses(unbatched)


def test_a_lost_configuration_event_stops_denominating_later_trades(tmp_path):
    """The fold's currency memo is projection state, so it rolls back too.

    An `InstrumentConfigured` that did not reach the log must not go on
    denominating fills that did, or the balances would be in a currency the
    log cannot account for -- which is the same class of defect as the orphan
    trade rows, one layer in.
    """
    path = tmp_path / "memo.db"
    sink = SQLiteSink(path, buffer_size=2)
    sink.consume(session())
    sink.consume(trader(1))  # the first batch, written cleanly
    fail_next_batch(sink, 2)
    sink.consume(InstrumentConfigured(2, 0.0, INSTRUMENT, CURRENCY))
    sink.consume(trader(3))
    assert seqs(path) == [0, 1]
    assert losses(path) == [(2, 3, 2)]
    assert sink._currency == {}, "a currency the log does not carry was kept"
    with pytest.raises(sqlite3.OperationalError):
        sink.close()


# --------------------------------------------------------------------------
# the two claims the review found overstated
# --------------------------------------------------------------------------


def test_the_journal_mode_is_checked_not_assumed(tmp_path):
    """`PRAGMA journal_mode = WAL` reports what it did; nothing read it.

    The durability story assumed WAL took. A `:memory:` database is the
    cheapest filesystem that refuses it, and stands in for the ones that do.
    """
    path = tmp_path / "wal.db"
    sink = SQLiteSink(path)
    assert sink.journal_mode == "wal"
    sink.close()

    with caplog_at_warning() as records:
        memory = SQLiteSink(":memory:")
    assert memory.journal_mode == "memory"
    assert any("journal_mode" in record for record in records), records
    memory.close()


class caplog_at_warning:
    """Collect this module's warnings, without depending on caplog's level."""

    def __enter__(self):
        self.records = []
        self.handler = _Collector(self.records)
        self.logger = logging.getLogger("PyLOB.sinks.sqlite")
        self.logger.addHandler(self.handler)
        self.previous = self.logger.level
        self.logger.setLevel(logging.WARNING)
        return self.records

    def __exit__(self, *exc_info):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.previous)
        return False


class _Collector(logging.Handler):
    def __init__(self, records):
        super().__init__(level=logging.WARNING)
        self.records = records

    def emit(self, record):
        self.records.append(record.getMessage())


def test_commission_is_reported_in_the_currency_it_was_charged_in(tmp_path):
    """`trader_commission` joined the instrument's *current* currency.

    So redenominating an instrument moved every commission ever charged on it
    into the new currency, retrospectively and silently. The order carries the
    currency it was accepted under instead.
    """
    path = tmp_path / "currency.db"
    book, _ = build(path, buffer_size=1, currency="USD")

    def crossing_pair(price):
        book.submit(
            tid=1,
            instrument=INSTRUMENT,
            side="bid",
            order_type="limit",
            qty=10,
            price=price,
        )
        book.submit(
            tid=2,
            instrument=INSTRUMENT,
            side="ask",
            order_type="limit",
            qty=10,
            price=price,
        )

    crossing_pair(100.0)
    book.configure_instrument(INSTRUMENT, "EUR")
    crossing_pair(101.0)
    book.close()

    conn = sqlite3.connect(path)
    try:
        charged = {
            (tid, currency): commission
            for tid, currency, commission in conn.execute(
                "SELECT tid, currency, commission FROM trader_commission"
            )
        }
    finally:
        conn.close()

    assert set(charged) == {(1, "USD"), (1, "EUR"), (2, "USD"), (2, "EUR")}
    assert all(value > 0 for value in charged.values()), charged
    # And the total is unmoved: the fix splits the commission by currency, it
    # does not recompute it.
    assert math.isclose(sum(charged.values()), 4.0, rel_tol=1e-9)


# --------------------------------------------------------------------------
# what the recording says about itself
# --------------------------------------------------------------------------

#: A sweep's worth of provenance: the two identifiers that tell fifty
#: otherwise identical `.db` files apart, a label, and a bool -- which SQLite
#: stores as an integer, so the round trip is asserted rather than assumed.
META = {"seed": 20260814, "episode": 7, "label": "sweep-a", "warmed_up": True}

RECORDED_META = [
    ("episode", 7, "integer"),
    ("label", "sweep-a", "text"),
    ("seed", 20260814, "integer"),
    ("warmed_up", 1, "integer"),
]


def test_metadata_survives_a_session_killed_before_its_first_flush(tmp_path):
    """The file with nothing in it still says which run it was.

    A session shorter than `buffer_size` that is killed rather than closed
    leaves a database with no events at all -- the ordinary outcome, per the
    module docstring, and precisely the run a sweep wants to identify, since
    the one that died is the one worth looking at. Metadata is committed in
    the opening transaction, before the first event, so it is the one thing in
    that file the buffer could not take with it.

    The alternative -- writing it at `close` -- would put the identifier in
    exactly the sessions that do not need it and in none of the ones that do.
    """
    path = tmp_path / "killed-meta.db"
    sink = killed_session(path, buffer_size=DEFAULT_BUFFER_SIZE, n_orders=5, meta=META)

    assert sink.buffered > 0, "nothing was actually lost with the process"
    assert seqs(path) == [], "the file with no events in it is the case that matters"
    assert meta_rows(path) == RECORDED_META

    # And it is still an unfinished log: naming itself is not a claim to be
    # complete.
    with pytest.raises(IncompleteLogError):
        check_log(path)


def test_a_metadata_carrying_session_records_the_same_stream_as_one_without(tmp_path):
    """Provenance is about the recording, so it must not reach the recording.

    Metadata deliberately does not travel in the event stream: it is what the
    experimenter wants to remember, not what the engine did, and the sink is
    where it lives so that `events.py` and `STREAM_VERSION` are untouched. The
    check is the direct one -- the same seeded workload recorded twice, once
    labelled and once not -- because a `meta` that perturbed the clock, the
    engine or the fold would produce a recording comparable with nothing.
    """
    labelled = tmp_path / "labelled.db"
    book, _ = build(labelled, buffer_size=64, meta=META)
    workload(book, random.Random(4242), n_ops=150)
    book.close()

    plain = tmp_path / "plain.db"
    book, _ = build(plain, buffer_size=64)
    workload(book, random.Random(4242), n_ops=150)
    book.close()

    assert dump(labelled) == dump(plain)

    def legs(path):
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT * FROM trade_leg ORDER BY trade_id, tid, leg"
            ).fetchall()
        finally:
            conn.close()

    assert legs(labelled) == legs(plain)
    assert legs(labelled), "the workload must have traded"

    assert meta_rows(labelled) == RECORDED_META, "the labelled run kept its labels"
    assert meta_rows(plain) == [], "and supplying none is not an error"


def test_metadata_that_cannot_be_recorded_is_refused_before_anything_opens(tmp_path):
    """A recording that cannot state what it is does not start.

    For a value SQLite refuses outright the alternative is a
    `sqlite3.InterfaceError` out of the opening write, which names neither the
    key nor the caller's line. For one it would happily store -- a `None` that
    reads back indistinguishably from a key nobody supplied -- the alternative
    is worse: a file whose provenance says something other than what was
    meant. Both are caught in front of the connection, so a refused call
    leaves no file behind either.
    """
    path = tmp_path / "badmeta.db"
    with pytest.raises(TypeError, match="values must be"):
        SQLiteSink(path, meta={"seed": [1, 2, 3]})
    with pytest.raises(TypeError, match="values must be"):
        SQLiteSink(path, meta={"seed": None})
    with pytest.raises(TypeError, match="keys must be strings"):
        SQLiteSink(path, meta={7: "seven"})
    assert not path.exists(), "the refusal happened before the file was opened"

    # An empty mapping is not metadata that failed, it is metadata nobody
    # supplied: the same state as omitting the keyword.
    sink = SQLiteSink(path, meta={})
    sink.close()
    assert meta_rows(path) == []


# --------------------------------------------------------------------------
# the other sink this package ships
# --------------------------------------------------------------------------


def test_the_shipped_list_sink_is_the_three_line_sink_events_py_promises():
    """`EventSink` described a sink that was not anywhere to be imported.

    Every suite that wants to know what the engine emitted wrote its own; the
    2026-08 clarity review counted four copies. This is that class, shipped
    once, and it has to keep being what the protocol says it is: no `close`
    (the case `close_sink` exists for), every event, in `seq` order.
    """
    sink = ListSink()
    book = OrderBook(tick_size=TICK, sink=sink)
    book.configure_instrument(INSTRUMENT, CURRENCY)
    for tid in (1, 2):
        book.configure_trader(tid, name=str(tid))
    for side in ("bid", "ask"):
        book.submit(
            tid=1 if side == "bid" else 2,
            instrument=INSTRUMENT,
            side=side,
            order_type="limit",
            qty=5,
            price=100.0,
        )

    assert isinstance(sink, EventSink)
    assert not isinstance(sink, ClosableEventSink), "no close, and none needed"
    assert [event.seq for event in sink.events] == list(range(len(sink.events)))
    kinds = [type(event).__name__ for event in sink.events]
    assert kinds[0] == "SessionStarted"
    assert "Filled" in kinds, "the crossing pair traded, and the sink saw it"

    close_sink(sink)  # a no-op on a sink with nothing to flush
    book.close()
    assert len(sink.events) == len(kinds), "and closing emitted nothing further"
