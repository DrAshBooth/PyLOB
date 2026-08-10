"""Shared pytest fixtures for the PyLOB test suite.

The package is not installed, so `src/` is prepended to `sys.path` here --
resolved from this file, never from the current working directory, so the
suite runs the same from anywhere.

Every book is backed by a fresh sqlite *file* under pytest's `tmp_path`,
built from `src/create_lob.sql`. A file (rather than `:memory:`) is how the
engine really runs, and it keeps a failing test's database on disk for
inspection. The committed `src/lob.db` is never opened, read or written.
"""

import sqlite3
import sys
from itertools import count
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PyLOB import OrderBook  # noqa: E402  (import needs the sys.path above)

SCHEMA = SRC / "create_lob.sql"

INSTRUMENT = "FAKE"
CURRENCY = "USD"
# The trader ids example.py uses.
TRADERS = tuple(range(100, 112))

# Seeding follows example.py: one transaction, traders then the instrument,
# both idempotent. Commissions are left at the schema default of 0 -- they
# feed the commission triggers only, never fill accounting, and zero keeps
# trader_balance readable in tests.
SEED_TRADER = """
insert into trader (tid, name, allow_self_matching)
values (:tid, :name, :allow_self_matching)
on conflict do nothing;"""

SEED_INSTRUMENT = """
insert into instrument (symbol, currency)
values (:symbol, :currency)
on conflict do nothing;"""


def build_book(
    db_path,
    traders=TRADERS,
    allow_self_matching=0,
    instrument=INSTRUMENT,
    currency=CURRENCY,
):
    """Create the schema at `db_path`, seed it, and return a connected OrderBook.

    The connection is reachable as `book.db`.
    """
    conn = sqlite3.connect(db_path)
    # The engine drives its own `begin transaction` / `commit`, so autocommit
    # must be left to it -- same as example.py.
    conn.isolation_level = None
    conn.executescript(SCHEMA.read_text())

    crsr = conn.cursor()
    crsr.execute("begin transaction")
    for tid in traders:
        crsr.execute(
            SEED_TRADER,
            dict(tid=tid, name=str(tid), allow_self_matching=allow_self_matching),
        )
    crsr.execute(SEED_INSTRUMENT, dict(symbol=instrument, currency=currency))
    crsr.execute("commit")

    return OrderBook(db=conn)


@pytest.fixture
def lob_factory(tmp_path):
    """Factory building independent order books, each on its own sqlite file.

    Call it as `lob_factory(traders=(1,), allow_self_matching=1)`; every call
    returns a fresh `OrderBook`. All connections are closed at teardown.
    """
    books = []
    counter = count(1)

    def make_book(**kwargs):
        book = build_book(tmp_path / ("lob%d.db" % next(counter)), **kwargs)
        books.append(book)
        return book

    yield make_book

    for book in books:
        book.db.close()


@pytest.fixture
def lob(lob_factory):
    """An empty book seeded with example.py's traders; self-matching off."""
    return lob_factory()


@pytest.fixture
def self_matching_lob(lob_factory):
    """An empty book with a single trader (tid=1) allowed to match itself.

    This is what the issue #8 repros need: one trader crossing itself is the
    smallest way to exercise both sides of a match.
    """
    return lob_factory(traders=(1,), allow_self_matching=1)
