PyLOB
=====

A limit order book for simulation research, written in Python.

PyLOB simulates a limit-order-book exchange so that automated trading
strategies working from "Level 2" market data can be explored offline. It
operates standard price-time priority, supports market and limit orders with
add, cancel and modify, and is single-threaded. Its chief simplifying
assumption is zero latency: a quote is processed the instant it is submitted,
and every other trader can react to it before the next quote arrives.

Two behaviours are worth knowing before the first run: a market order is
immediate-or-cancel and never rests in the book, and a trade prices at the
*maker* — the resting order's limit, not the arriving one's.

Matching is in memory, in one layer. SQLite is optional and off the matching
path: attach a sink and the session's events, trades, balances and commissions
are recorded for querying afterwards; attach nothing and no event is
constructed at all. See [ADR-0001](docs/adr/0001-inmemory-matching-sqlite-sink.md)
for why, and [ADR-0003](docs/adr/0003-retire-the-legacy-sql-engine.md) for the
retirement of the SQL engine that sat behind the same API from 2023 until
2026.

Installation:
=============
PyLOB is not published on PyPI (the `pylob` name there belongs to an unrelated project). Install it from GitHub; Python 3.11 or newer is required:

    pip install "PyLOB @ git+https://github.com/DrAshBooth/PyLOB.git"

or, with [uv](https://docs.astral.sh/uv/):

    uv add "PyLOB @ git+https://github.com/DrAshBooth/PyLOB.git"

To work on PyLOB itself, clone the repo and run `uv sync`; `./verify` is the definition of done.

Requirements:
=============
Nothing beyond a standard Python 3.11+ install. PyLOB has no runtime
dependencies; the optional recording sink uses the standard library's
`sqlite3`.

Speed:
======
Measured on a 20,000-order mixed workload (70% passive, 20% crossing, 10%
market; 20 traders; commissions on):

| Configuration | orders/sec |
| --- | --- |
| No sink — the default | ~155,000 |
| `SQLiteSink` attached | ~19,000 |

The sinkless figure is the one the project's throughput target is set against;
the sink-attached figure is reported alongside it and is deliberately not
subject to that target ([ADR-0002](docs/adr/0002-throughput-target-measured-sinkless.md)).
The difference is the sink's per-event encoding, not the matching engine —
raising the sink's buffer size barely moves it.

Both are hand measurements on one contended machine, not a benchmark suite —
none exists yet. Sinkless throughput varies with the shape of the workload:
[the pre-retirement review](docs/engine-review-2026-08.md) measured 130k–307k
orders/sec across seven shapes (one-tick, sparse, cancel-heavy, modify-heavy,
monotonic, stale-churn, mixed). Read the table as an order of magnitude, not
as a promise.

Usage and semantics:
====================
`src/example.py` is the walkthrough — limit orders, crossing, partial fills,
market orders, cancel, modify, and the same run again with a recording sink
attached. It is executed on every `./verify`, so it cannot rot silently.

What the book is contractually required to do lives in `openspec/specs/`
(order lifecycle, matching, book queries, commissions, trader balances); the
acceptance suites in `tests/acceptance/` are written one test per ratified
scenario. Design decisions and their rejected alternatives are indexed in
[docs/adr/README.md](docs/adr/README.md).

The wiki has not been rewritten yet. It describes the pure-Python
red-black-tree implementation that PR #7 replaced in 2023 — two engines ago —
so treat it as historical rather than as a guide to the code you installed.

The code is open-sourced via the MIT Licence: see the LICENSE file for full text. (copied from http://opensource.org/licenses/mit-license.php)
