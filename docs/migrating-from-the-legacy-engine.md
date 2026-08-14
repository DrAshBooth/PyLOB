# Migrating from the legacy SQL engine

For code written against the SQL engine that PyLOB shipped until
[ADR-0003](adr/0003-retire-the-legacy-sql-engine.md) retired it — the one whose
`OrderBook.__init__` took a `sqlite3` connection. Every name that engine
exposed and `openspec/config.yaml` protects is still here (`processOrder`,
`cancelOrder`, `modifyOrder`, `getVolumeAtPrice`, `getBest*`/`getWorst*`,
`print`), so a port is a matter of what those calls now *return*, *write back*
and *refuse* — not of rewriting the call sites.

Nothing below is a deprecation or a plan. It is what the shipped engine does
today, checked against both implementations; the retired one can still be read
with `git show 42a7118^:src/PyLOB/orderbook.py` (the commit before "Retire the
legacy SQL engine"), which is where every "used to" here comes from.

Read this once and then use the docstrings: `OrderBook.processOrder`,
`cancelOrder` and `modifyOrder` each state their own contract.

## The five-minute version

```python
from PyLOB import OrderBook

lob = OrderBook(tick_size=0.01)          # no database, no connection
lob.configure_instrument("FAKE", "USD")  # was a row in the instrument table
lob.configure_trader(100, name="100", commission_min=2.5)  # was a trader row

quote = dict(tid=100, instrument="FAKE", side="bid", type="limit",
             qty=5, price=99.0)
trades, quote = lob.processOrder(quote)  # same call, same quote keys

quote["idNum"]      # still the identifier cancelOrder/modifyOrder take
quote["timestamp"]  # now a float
quote["price"]      # the quantized working price; None for a market order
quote["order_id"]   # KeyError: there is no database and no rowid
```

The engine holds its own state, so there is no connection to open, no schema
to build, and no rows to insert. Traders, instruments, balances and
commissions are `configure_trader`, `configure_instrument`, `balance()` and
`holdings()`; a recorded session is an optional `SQLiteSink` passed as
`OrderBook(sink=...)`, off the hot path and never read back by the engine.

`OrderBook(connection)` now raises `InvalidOrder` complaining about the tick
size, because the first parameter is `tick_size`.

## What `processOrder` returns

Both engines return `(trades, quote)`. The trades are the change.

| | legacy | now |
| --- | --- | --- |
| element type | plain 5-tuple | `Trade`, a 10-field `NamedTuple` |
| fields | `(bid_order, ask_order, time, price, qty)` | `(trade_id, timestamp, instrument, price, qty, taker_side, bid_idNum, bid_tid, ask_idNum, ask_tid)` |
| the two identifiers | SQLite **rowids** of the order rows | **idNums** — the identifiers `cancelOrder` and `modifyOrder` take |

`Trade` is a tuple ([ADR-0004](adr/0004-trade-is-a-namedtuple.md)), so it
unpacks, indexes and compares equal to a plain tuple of its fields — and its
field *order* is public surface. The arity is what saves a port: legacy
five-way unpacking raises `ValueError: too many values to unpack (expected 5)`
rather than quietly handing back the wrong column. Read fields by name.

`taker_side` says which side aggressed, and `Side` is a `StrEnum`, so
`trade.taker_side == "bid"` holds. `trade.taker_idNum` and `trade.maker_idNum`
resolve the pair for you. `price` is always the **maker's** limit; the legacy
engine priced a fill against a resting NULL-price market order at the *taker's*
price instead.

There is no trade log to query afterwards. The engine keeps `getLastPrice`
only; attach a `SQLiteSink` for the history (`src/PyLOB/sinks/sqlite.py`).

## What comes back in the quote

Written back, as before: `idNum`, `timestamp`, `price` — the identifier, the
engine's stamp, and the *quantized working price* rather than the one asked
for. The quote you passed is the object returned, mutated in place.

Gone:

- **`order_id`** — a rowid in a database that no longer exists. `idNum` is the
  only identifier now, and it is the one every other call takes.
- **`lastprice`** — `getLastPrice(instrument)` answers it, and it is no longer
  smuggled through the quote on every submission.

On the `fromData` path the legacy engine required `timestamp` in the quote (and
`idNum`, or its insert failed). So does this one: a `fromData` quote missing
either — absent, or present as `None` — raises `InvalidOrder` naming it. The
flag says the quote's identity comes from the data, and a path that invents the
identity it was handed the flag to reproduce gives back a run that no longer
traces to its source, silently. A quote with no identity of its own is an
ordinary submission and `fromData=False` is the call for it; the flag is per
call, so a feed mixing rows that carry an identifier with rows that do not is
two calls rather than one.

> **Stability note.** Nothing ratifies this refusal —
> `openspec/specs/order-lifecycle` does not mention `fromData`. It restores
> what the legacy engine required, so no ported caller meets it, and it was
> chosen over the alternative of documenting the silent assignment as
> deliberate (`lob-0mv`). The engine that replaced the legacy one assigned
> both quietly until this landed, so code written against *that* behaviour —
> not against legacy — is the only code it breaks.

## Timestamps are floats

The legacy clock was an `int` starting at 0 and incremented by one per
operation. It is a `float` now — seeded by `OrderBook(timestamp=...)`, default
`0.0`, and still advanced by one per operation — so `quote["timestamp"]` is
`1.0` where it used to be `1`. The values still compare equal; their `repr`,
their JSON encoding and any identity check do not.

A supplied stamp is stored as given, and — the part that bites — *zero is a
value now*. Legacy `cancelOrder(side, idNum, time=0)` tested `if time:`, so 0
meant "no time supplied, tick the clock". Here `time=0` sets the clock to 0.
Only `None` means "not supplied", for `cancelOrder`, `modifyOrder` and
`submit`/`processOrder` alike.

## `modifyOrder` does not touch your dict

The legacy call wrote `idNum`, `timestamp`, `type`, `order_id`, `instrument`
and `fulfilled` into the `orderUpdate` you passed, and quantized its `price` in
place. This one reads it and returns it unchanged (the same object, so
`returned is orderUpdate`). A caller that read results back out of the update
dict — `orderUpdate["fulfilled"]` was the common one — asks the order instead:

```python
order = lob.order(idNum)      # None if no such order; require_order raises
order.fulfilled, order.remaining, order.price, order.commission
```

Three keys are required — `side`, `qty`, `price` — and a missing one raises
`InvalidOrder` naming it. `None` means *leave that one alone*; it emphatically
does not mean "become a market order", which is how the legacy engine read
`price=None` while keeping the order's stored limit.

Two more behavioural changes in the same call: an unknown `idNum` raises
`UnknownOrder` where legacy silently did nothing, and a reprice always
re-crosses the book. Legacy re-matched only when its `betterPrice` test said
the new price was more aggressive.

## Refusals that used to be silent

The legacy engine's cancel was one `UPDATE ... where idNum=? and side=?`.
Nothing matching meant nothing happened, and the caller was told nothing.

| what you ask | legacy | now |
| --- | --- | --- |
| cancel an unknown `idNum` | matched no row, no error | `UnknownOrder` |
| cancel with the wrong `side` | matched no row, no error | `InvalidOrder` |
| cancel an already-cancelled order | re-set the flag, no error | `InvalidOrder` |
| cancel a fully filled order | flagged a finished row, no error | `InvalidOrder` |
| cancel a market order after submission | removed its resting remainder | `InvalidOrder`: the engine cancelled it at submission |
| modify an unknown `idNum` | matched no row, no error | `UnknownOrder` |
| modify a fully filled order | raised its quantity and put it back in the book | `InvalidOrder` |
| `getVolumeAtPrice` with a side that is not `"bid"`/`"ask"` | `0`, indistinguishable from no volume | `InvalidOrder` naming the valid sides |

`cancelOrder` also returns the `Order` now (legacy returned `None`), and takes
`side=None` to address an order by identifier alone.

The market-order row is the one that surprises a port, and it is two changes at
once: the remainder no longer rests (below), and cancelling it is therefore a
cancel of something already cancelled. Check `order.cancelled` and
`order.cancel_reason` rather than cancelling defensively.

> **Stability note.** Only the unknown-identifier refusal is ratified.
> `openspec/specs/order-lifecycle` requires that "cancel or modify is called
> with an identifier no order has" raise; cancel's other two terminal refusals
> — already cancelled, nothing left to cancel — are current engine behaviour
> that no spec states. `tests/reference/matcher.py` says so where it
> implements them, and whether to write them into the spec is left open in
> `openspec/changes/modify-refuses-filled-orders/design.md`. Depend on the
> ratified one; treat the other two as behaviour that could still be revisited
> by the maintainer.

## Bad input raises instead of exiting the process

`processOrder` used to call `sys.exit()` on a quantity `<= 0`, an unknown
`type` and an unknown `side` — it terminated the host simulation. Every
refusal is now an exception:

- `PyLOBError` is the base of everything this library raises.
- `InvalidOrder` is a `PyLOBError` **and** a `ValueError`, for the caller who
  wraps bad input in `except ValueError` without reading anything.
- `UnknownOrder` is a `PyLOBError` and a `LookupError`.
- `DuplicateOrderID` is an `InvalidOrder`.

Newly refused, all of them accepted or ignored before:

| quote | legacy | now |
| --- | --- | --- |
| missing `tid`/`instrument`/`side`/`type`/`qty` | `KeyError` from inside the engine | `InvalidOrder` naming the missing field |
| `type="limit"` with no `price` | stored with a NULL price and crossed like a market order | `InvalidOrder`: a limit order needs a price |
| `type="market"` carrying a `price` | quantized it and matched against it — a market order capped like a limit | `InvalidOrder` |
| `price` of `nan`, `inf`, or ~1e40 | whatever `round(price, digits)` returned, stored unchecked | `InvalidOrder` |
| `qty` of `0` or `-1` | `sys.exit` | `InvalidOrder` |
| `qty` of `1.5`, `True`, or above 2\*\*53 | unchecked | `InvalidOrder` |
| `fromData` re-using an `idNum` the book has seen | accepted; two orders answered to one identifier | `DuplicateOrderID` |

The missing-key row is the newest of these (lob-49r): `KeyError` is neither a
`PyLOBError` nor a `ValueError`, so a caller who had wrapped `processOrder` in
the two exceptions this library documents caught nothing at all.

Nothing moves when a submission is refused — no order, no event, no clock tick,
no identifier consumed — so a caught `InvalidOrder` leaves a book you can carry
on using.

## The price grid

`clipPrice` is still here, as another name for `quantize`, and it now answers
differently. Legacy computed `round(price, log10(1/tick))`: a digit count, not
a grid, which treated 0.25, 1 and 5 as the same tick and put 100.03 on a 0.05
tick at **100.0**. The grid is real now — divide by the tick, round half-even,
multiply back, in `decimal` — so the same call gives **100.05**.

On the default 0.0001 tick, ordinary decimal prices are unaffected. If your
simulation ran on a non-decimal tick (0.05, 0.25, 5), its resting prices will
differ from the legacy run's, and that is the fix rather than a regression:
`order-lifecycle` requires the nearest multiple of the tick for any positive
tick.

The tick is fixed for a book's life, so a different grid means a new
`OrderBook`, not a setter.

## Market orders no longer rest

Legacy left a market order's unfilled remainder in the book with a NULL price,
where it outranked every limit order indefinitely, was invisible to
`getBestBid`/`getBestAsk` (which reported `None` over it, indistinguishable
from an empty book), traded against incoming orders at the *taker's* price, and
matched other market orders at the last printed price. All four were
reproduced in [the review of that engine](architecture-review-2026-08.md),
§1.6.

`order-lifecycle` settled it: a market order is immediate-or-cancel and never
rests. The remainder is cancelled with `cancel_reason="ioc_remainder"`, and a
simulation that leaned on resting market orders for liquidity will fill less
than it used to.

## Names

| legacy | now |
| --- | --- |
| `OrderBook(db, tick_size=0.0001)` | `OrderBook(tick_size=0.0001, sink=None, timestamp=0.0)` |
| `book.db` | gone — no database on the matching path ([ADR-0001](adr/0001-inmemory-matching-sqlite-sink.md)) |
| `book.tickSize`, `book.rounder` | `book.tick_size` (the digit count has no successor) |
| `book.lastPrice[symbol]` | `getLastPrice(symbol)`; assignment is refused by `setLastPrice`, which says why |
| `book.lastTick`, `book.lastTimestamp` | gone, unused by the legacy engine too |
| `book.nextQuoteID` | gone — identifiers are assigned by `create_order` and stay unique for the book's lifetime |
| `updateTime()` | gone — the clock advances once per operation, or takes the `time=`/`timestamp=` you pass |
| `clipPrice(price)` | kept, and the same function object as `quantize(price)`; write `quantize` in new code |
| `getPrice(instrument, side, direction)` | `getBestBid`/`getBestAsk`/`getWorstBid`/`getWorstAsk`, or `snapshot(instrument, side)` for the whole queue |
| `orderGetSide(idNum)` | `book.order(idNum).side` (`order` returns `None` for an unknown identifier; `require_order` raises) |
| `betterPrice(side, price, comparedPrice)` | gone with the matching path it served; no replacement |
| `processMatchesDB(...)` | internal; `submit`/`processOrder` is the whole operation |
| `print(instrument)` | unchanged, and still both prints and returns the string — but its trades section is one `last price:` line, since no trade log is kept |
| `valid_sides = ("ask", "bid")`, `valid_types = ("market", "limit")` | still class attributes, holding the same strings in the enums' order: `("bid", "ask")` and `("limit", "market")` |

`submit(tid, instrument, side, order_type, qty, price)` is the same operation
as `processOrder` without the dict, and returns `(order, trades)`. New code
should prefer it: the `Order` it hands back answers for `fulfilled`,
`remaining`, `resting` and `commission` for the rest of the session, which is
what the quote's write-back keys were standing in for.

## Where the rest is written

- `src/example.py` — the walkthrough, run on every `./verify`, including the
  dict-quote API and a recorded session replayed into a fresh book.
- `openspec/specs/order-lifecycle/spec.md` — the ratified contract behind most
  of the refusals above.
- [ADR-0003](adr/0003-retire-the-legacy-sql-engine.md) — why there is one
  engine, and what was lost with the other.
- [architecture-review-2026-08.md](architecture-review-2026-08.md) — the review
  of the legacy engine, if you need to know whether behaviour you relied on was
  a decision or a defect.
