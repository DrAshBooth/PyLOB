# Architecture review — August 2026

Read-only review of the SQL-backed implementation (post-PR-#7), covering
`src/PyLOB/orderbook.py`, `src/create_lob.sql`, and all 13 query files.
Every finding marked **[confirmed]** was reproduced against a fresh DB built
from `create_lob.sql`; repro sketches are inline. Findings feed the planning
change map (last section); nothing here changes code.

## 1. Behavior anomalies

### 1.1 Fill accounting corrupts on identifier divergence — [confirmed] (issue #8)

`trade_insert`'s fulfillment arm matches `where idNum in (new.bid_order,
new.ask_order)`, but those columns store `order_id` values. All four
trader-balance arms of the *same trigger* — and the `trade_delete` trigger —
key on `order_id`. Already planned: `fix-fulfilled-accounting`.

### 1.2 Reprice-cross re-trades already-filled quantity — [confirmed] (issue #8)

`processMatchesDB` sets `qtyToExec = quote["qty"]`, never subtracting
`fulfilled`. Already planned: `fix-fulfilled-accounting`.

### 1.3 `trade_delete` trigger is unrunnable — [confirmed]

The trigger body references `new.qty` / `new.price`, but DELETE triggers only
have `old`. Any `DELETE FROM trade` fails with `no such column: new.qty`
— the entire compensation path (un-fulfill, un-balance) has never worked.
A third defect hides behind that one: its last balance arm aliases the bid
subquery `as bid_order` while its WHERE references `ask_order`. Repro:
execute any trade, then `DELETE FROM trade` → error.

### 1.4 Restart identity collision: duplicate idNums, mass-cancel — [confirmed]

`nextQuoteID` starts at 0 per `OrderBook` instance; `idNum` has no unique
constraint. Reopening an existing DB reissues idNum 1, 2, … alongside the old
rows. `find_order` (fetchone) then resolves an idNum ambiguously, and
`cancelOrder` — an unscoped `UPDATE ... WHERE idNum=:idNum AND side=:side` —
**cancels every colliding order at once**. Repro: session 1 adds ask idNum 1;
session 2 (same DB) adds ask → also idNum 1; `cancelOrder('ask', 1)` sets
`cancel=1` on both rows.

### 1.5 Last-price state lives in Python, dies on restart — [confirmed]

`set_lastprice` persists to `instrument.lastprice`, but matching reads
`:lastprice` from the in-memory `self.lastPrice` dict, which starts empty.
After reopening a DB whose `lastprice=101.0`, market-vs-market matching
behaves as if no price ever printed. Write path and read path disagree on the
source of truth.

### 1.6 Resting market orders: top priority forever, invisible to best-price — [confirmed]

A market order's unfilled remainder rests with `price NULL`, and the book
orders NULL prices ahead of everything (`case when price is null then 0`).
Consequences, all reproduced:
- A resting market bid outranks a limit bid at 150 indefinitely.
- `getBestBid`/`getBestAsk` return the NULL price, i.e. `None` —
  indistinguishable from an empty book — while `getVolumeAtPrice` counts the
  same order's quantity.
- An incoming ask at 90 matches the resting market bid **at 90** (the
  *taker's* price) even though a 150 limit bid rests behind it: price
  formation for NULL-price makers takes `coalesce(maker, taker, lastprice)`.
- Two market orders match each other at `lastprice` (repro: after a print at
  101, resting market bid × incoming market ask trades at 101).

None of this is necessarily wrong for a research simulator — but it is
currently *undefined* rather than *decided*. Real venues treat unfilled
market-order remainders as cancel (IOC) or convert-to-limit.

### 1.7 Wrong-side modify is a silent no-op that reports success — [confirmed]

`modifyOrder(idNum, {side: <wrong side>, ...})`: `find_order` ignores side and
finds the row; `modify_order.sql` filters `side=:side`, updates 0 rows; no
error propagates and the returned quote carries the found `order_id`. Caller
believes the modify landed.

### 1.8 Non-power-of-ten ticks silently misquantize — [confirmed]

`rounder = int(math.log10(1/tick_size))` assumes decimal ticks.
`tick_size=0.05` → rounder 1 → `clipPrice(100.03)` = `100.0`
(nearest valid tick: 100.05). No error, no warning.

### 1.9 Validation failures call `sys.exit` — [confirmed]

Invalid qty/type/side terminates the host process (`SystemExit`) instead of
raising a library exception. Happens before `begin transaction`, so no dangling
transaction — but a simulation host importing PyLOB dies with it.

## 2. Invariant map — what is enforced where

| Invariant | Where enforced | Notes |
| --- | --- | --- |
| Price-time priority | `best_quotes` view + `best_quotes_order.sql` | NULL price ranked best (see 1.6); ties on `event_dt` have **no secondary sort** → replay nondeterminism when `fromData` supplies equal timestamps |
| Match eligibility (side, price cross, self-match) | `matches.sql` | self-matching gate joins `trader.allow_self_matching` |
| Quantity allocation across matches | Python loop in `processMatchesDB` | the only place `qtyToExec` lives (see 1.2) |
| Fill accounting (`fulfilled`, `fulfill_price`) | `trade_insert` trigger | see 1.1; reversal path broken (1.3) |
| Trader balances | `trade_insert` trigger (4 arms) | keyed correctly on `order_id` |
| Commission | `order_commission` + `trader_commission` triggers | recomputed on every fulfillment update; inherits any mis-attributed fill |
| Immutability (side rows, trade rows, order identity fields) | `*_lock` triggers | solid |
| Balance-row existence | `order_insert` trigger | solid |
| Transaction boundaries | Python (`begin`/`commit` via cursor) | requires `isolation_level=None`-style handling by the caller; no rollback on mid-flight exception; worked in probes but fragile by construction |
| Referential integrity | **nowhere by default** | `PRAGMA foreign_keys` commented out of the schema; `example.py` enables it per-connection, the library never does |
| idNum uniqueness | **nowhere** | see 1.4 |
| Type strictness | **nowhere** | `STRICT` commented out on every table; an int price is stored as int |

The split worth naming: **eligibility and ordering live in SQL; allocation
lives in Python; accounting lives in triggers.** Three layers must agree for a
single trade to be right. This is the central coupling the SQLite trial should
weigh — it is also why bug 1.1 could hide: the layer that wrote `bid_order`
and the layer that consumed it disagreed on what the value meant.

## 3. Coupling and hazards

- **`__init__` loads every sibling `.sql` file as an attribute.** The schema
  namespace and Python namespace are implicitly coupled by *filename*;
  `best_quotes.sql` (the query file) is loaded, shadows nothing, and is used
  nowhere — dead code that also collides with the `best_quotes` view name.
- **Dead schema**: `event`/`event_arg` tables (planned event system, unused),
  the `active` column (never set to 0; makes `order_detail`'s commission
  CASE partially dead), `trade.idNum`.
- **API contract drift**: `processOrder` returns `(trades, quote)`;
  `example.py` unpacks the second element as `idNum`. Works only because
  nobody inspects it.
- **Committed `src/lob.db`** embeds the schema (including buggy triggers) at
  whatever commit it was generated; `example.py` cannot bootstrap without it.
  Already on the change map (`fix-example-bootstrap`).
- **Docs describe the deleted implementation** (README: RBTrees, zero-deps;
  wiki likewise). Already on the change map (`rewrite-docs`).

## 4. Consequences for the change map

| Change | Adjustment |
| --- | --- |
| `fix-fulfilled-accounting` (planned) | **Extend scope decision**: 1.3 (`trade_delete`) is the same accounting family — either add "fix or drop the trade_delete trigger" to this change, or fence it as its own row. Recommend extending: a broken reversal path for the same column, same trigger file. |
| **NEW: `fix-persistence-identity`** | 1.4 + 1.5: enforce idNum uniqueness (or scope queries by active order), define restart semantics (resume counters from DB, hydrate lastPrice), make `cancelOrder` single-row. Blocks any multi-session simulation use. |
| `spec-book-queries` (planned) | Now has concrete scenarios: all four behaviors in 1.6 need a ruling (rest-at-top vs IOC vs convert-to-limit; what `getBest*` returns over a NULL-price top; taker-price formation), plus 1.7 (wrong-side modify must error) and the tie-break gap from §2 (deterministic FIFO needs a secondary sort). |
| **NEW: `harden-order-api`** | 1.8 + 1.9: exceptions instead of `sys.exit`; tick quantization that rejects or correctly rounds non-decimal ticks; document (or own) the transaction/isolation contract. Candidate to merge into the API portion of `spec-book-queries` if granularity feels too fine. |
| `modernize-packaging` (planned) | Add dead-code removal (best_quotes.sql query file, event tables, active column) **only if** the maintainer wants cleanup bundled; otherwise a separate `remove-dead-schema` row. Cleanup shrinks the surface the SQLite trial has to defend. |
| `benchmark-harness` (planned) | §2's three-layer split is the thing to benchmark *against*: an in-memory reference book collapses eligibility/allocation/accounting into one layer. No scope change, but the invariant map is input to its design. |
| `rewrite-docs` (planned) | Unchanged; §1.6 rulings must land first or the docs will describe undefined behavior. |

Severity, if the beads need priorities: 1.1/1.2 (planned) and 1.4 are
correctness-critical; 1.3, 1.5, 1.7 are correctness bugs on less-traveled
paths; 1.6 is a decision, not a bug, but blocks specs; 1.8/1.9 are API
hardening; the rest is hygiene.
