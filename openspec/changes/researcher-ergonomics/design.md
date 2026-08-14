# Design: researcher-ergonomics

## Context

See `proposal.md` for the four findings and their citations. The binding
constraints on how they are satisfied:

- `openspec/config.yaml` keeps the public API unless it proves a limiter of
  performance or clarity; changing it needs an ADR. Everything here is
  additive, and the decisions below are largely about *staying* additive.
- ADR-0001 / ADR-0002: sinks are optional and off the hot path, and a sinkless
  engine constructs no event. Nothing here may put work on the matching path.
- `events.py` is a ratified contract and is on the review's must-not-break
  list. Two of the four findings could have been solved by widening it; both
  are solved without it.
- `docs/adr/README.md` was read. No existing ADR constrains these additions.
  One decision *may* need a new ADR, and it is the open question below rather
  than something this change takes on its own authority.

## Goals / Non-Goals

**Goals:**
- The four surfaces a researcher reaches for first — ladder, cancel, modify,
  recorded provenance — reachable from `help(PyLOB)` and the README, without
  reading `engine.py`.
- Every addition either provably agrees with an existing query, or is provably
  the same operation under a different spelling. No second source of truth.
- Each task independently landable, so the beads this converts into do not
  block each other for bookkeeping reasons.

**Non-Goals:**
- No change to matching, to the event vocabulary, or to `STREAM_VERSION`.
- No deprecation of the legacy spellings; no aliasing, no warnings.
- No new capability. Nothing here crosses instruments or currencies, gates on
  balances, or adds a portfolio view — all of those are new capability per
  `config.yaml` and would need their own proposal.

## Decisions

### 1. `depth` returns plain `(price, volume)` pairs, best price first

`depth(instrument, side, levels=None) -> tuple[tuple[float, int], ...]`.

The volume is `PriceLevel.volume`, which the book already maintains
incrementally (`engine.py:611-626`), so a ladder costs the sort and nothing
else — no walk over orders. Ordering is `BookSide.levels()`' ordering, which is
matching-priority ordering, which is `snapshot`'s ordering: one rule, three
queries.

*Why pairs rather than a NamedTuple:* ADR-0004 established NamedTuple as the
house answer for `Trade`, but that was a twelve-field record on the hot path
where attribute access is what a caller wants. A level has two fields, its
names are its position, and `prices, volumes = zip(*ladder)` is the plotting
call. A named type would also add an export to `PyLOB.__all__` for a
convenience. Reversible: widening a pair to a NamedTuple is source-compatible
in the direction that matters.

*Why `levels` is bounded and positive:* `levels=None` means the whole ladder;
`levels=n` means the best `n`. A non-positive `n` raises rather than returning
empty, because "the best zero levels" and "the whole ladder" are the two things
a caller could have meant by `0` and neither is worth guessing at. The
implementation may use `heapq.nsmallest` on the price keys rather than sorting
every level; that is a performance note, not a contract.

*Deliberately unspecified:* naming an instrument the book has never heard
creates its book and reads as empty (`engine.py:1837-1841`). That is already
true of every read-side query and is documented in code; `depth` follows it.
Writing it into the `book-queries` spec through a `depth` scenario would ratify
a behaviour this change did not decide, so it stays out of the delta.

### 2. `cancel`/`modify` take an `int` identifier first, everything else keyword-only

```python
def cancel(self, idNum: int, *, side=None, timestamp=None) -> Order
def modify(self, idNum: int, *, qty=None, price=None, side=None,
           timestamp=None) -> tuple[Order, list[Trade]]
```

Both delegate to `cancelOrder`/`modifyOrder`. That is the whole implementation
and it is the point: one validation path, one emission path, one place where
the `order-lifecycle` rules live. A companion that reimplemented the clamp rule
or the reprioritization rule would be a second contract to keep in step, and
the review's finding is about the cost of second copies.

`modify` returns `(order, trades)`, mirroring `submit`, where `modifyOrder`
returns `(trades, orderUpdate)` — the legacy shape, in which the second element
is the dict the caller passed in. Matching `submit` is the ergonomics fix; the
old shape stays exactly as it is for the old name.

*Omission means "leave alone", absence of both means error.* `modifyOrder`
requires all three of `side`/`qty`/`price` to be present in the dict and reads
`None` as "leave that one alone" (`engine.py:1481-1490`). The keyword form
cannot distinguish "absent" from "None", so it takes omission as "leave alone"
— and then `modify(idNum)` would emit a `Modified` event that changes nothing,
advancing the clock and adding a row to every recorded log. So naming neither
`qty` nor `price` raises. That preserves the spirit of the dict form's
"a missing key is an `InvalidOrder` naming it, never a `KeyError`".

*Why not `Order | int` (as bead `lob-6zw` sketches it):* an `Order` handed back
by `submit` is a live engine object, and the natural implementation
(`require_order(order.idNum)`) would silently act on a *different* order when
the object came from another `OrderBook` — the same class of quiet misbinding
this change exists to remove. It can be added safely later behind an identity
check (`self.order(o.idNum) is o`, else raise); it is left out of the first
landing so the surface is one type, and the maintainer keeps the call.

*Why the old parameter name is not changed:* `cancelOrder(..., time=)` and
`modifyOrder(..., time=)` keep `time=`. Renaming a keyword parameter breaks
callers that pass it by name, which is a change to the protected API and would
need an ADR to buy consistency the new names already provide.

### 3. Session metadata lives in the sink, not in the event stream

`SQLiteSink(path, *, meta: Mapping[str, str|int|float|bool] | None = None)`
writes a `session_meta(key, value)` table in the opening transaction.

*Rejected alternative — `OrderBook(..., meta=...)` carried on
`SessionStarted`.* It reads better (one place to say it, every sink gets it)
and it would not have needed a `STREAM_VERSION` bump by that constant's own
rule: adding a defaulted field makes an older stream replay *incompletely*, not
*wrongly* (`events.py:156-160`). The hazard is the other direction. A stream
written with the extra field and read by an older PyLOB fails inside
`decode_event`, which does `EVENT_BY_KIND[kind](**payload)`
(`sqlite.py:1358-1369`) — an unexpected keyword is a `TypeError` from a
dataclass constructor, not the `EventLogError` the module raises for every
other unreadable file. Making that failure clean would mean bumping
`STREAM_VERSION`, and `check_log` refuses any stream version it does not
implement (`sqlite.py:1310-1320`), so **every session ever recorded would stop
being readable** — an enormous price for provenance. The sink-side table costs
none of it and leaves `events.py` untouched.

The conceptual line holds up independently: the event stream is what the
*engine* did, and metadata is what the *experimenter* wants to remember about
the run. A `ListSink` user already has their variables in scope; the fifty-file
sweep is a file problem.

*Written at open, not at close.* A short episode killed before the sink's first
flush leaves a file with no rows in it at all (`sqlite.py:139-145`) — the
ordinary outcome for a run under `buffer_size` events. Writing metadata in the
opening transaction means that file still says which seed it was, which is
precisely the run you want to identify. `read_meta` therefore does not call
`check_log`: an incomplete log's metadata is exactly as trustworthy as a
complete one's, because it was committed before any of the events were.

*Values keep their types.* One row per key, `value` with no declared type,
leaning on SQLite's dynamic typing, so `meta={"seed": 42}` reads back `42` and
not `"42"`. Key/value rows rather than one JSON blob because scanning fifty
files for `WHERE key='seed'` is the use case, and because the column comments
inside the `CREATE` statement are what makes `.schema` self-describing — the
property the review's praise list protects.

### 4. `trade_leg` is a view, and `trade` gains a `currency` column

The view unpivots each `trade` row into the four movements the balance rule
defines (`events.py:81-85`), which the sink applies verbatim
(`sqlite.py:1043-1065`):

```
bid side (buyer):  instrument += qty
                   currency   -= qty * price + bid_commission_delta
ask side (seller): instrument -= qty
                   currency   += qty * price - ask_commission_delta
```

*The column is not optional, and this is the finding the review did not have.*
The sink books the currency leg using `self._currency.get(instrument)` — the
currency **in force when the fill was projected**. `orders.currency` is the
currency stamped **when the order was accepted** (`sqlite.py:356-360`), and
`configure_instrument` is re-callable: "naming a different currency
re-denominates the legs of every *later* trade" (`engine.py:1140-1142`). The
two disagree for any order that rests across a re-denomination, and they can
disagree with each other for the two sides of a single trade. So a `trade_leg`
that joins `orders` for its currency reproduces `balance` to 1e-9 in every
session that never re-denominates — which is every session anyone has tested —
and is silently wrong in the one that does. Recording the settled currency on
the `trade` row makes the view exact by construction, needing no join at all,
and makes the `trade` table honest about what it settled in. It is the same
principle `orders.currency` and the `trader_commission` view already apply
(`sqlite.py:421-425`): stamp the currency where it was used.

`currency` is `NULL` when the instrument had no declared currency at the fill,
which is a state the engine supports — it settles the instrument leg and
nothing else (`engine.py:1823-1828`). The view emits the two instrument legs
for such a trade and no currency legs, so its aggregate still equals `balance`.

*Rejected alternative — a `trade_leg` table.* Four extra row-writes per trade,
against a sink whose whole projection layer currently costs "roughly one extra
row-write per event" (`sqlite.py:58-59`). It would buy a query the view answers
from data already on disk.

*Rejected alternative — leave it as a documented recipe.* That is what exists
now (`sqlite.py:29-33` says the movements "are derivable"), and the review's
finding is that everyone derives them again. A recipe in a docstring is also
the exact shape of the "excellent but unreachable" problem the review filed
against the schema documentation.

*Tolerance, not equality.* `balance` accumulates `amount = amount +
excluded.amount` in `seq` order per key; the view's `SUM` aggregates in an
order SQLite chooses. Float addition is not associative, so the contract is a
floating-point tolerance — the same call the `commissions` spec already makes
("acceptance tests SHALL compare within a floating-point tolerance rather than
for bit equality").

### 5. One schema landing, one version bump

`session_meta`, `trade.currency` and `trade_leg` all ship in a single revision
to `SCHEMA_VERSION = 4`.

Splitting them would bump twice, and each bump costs every user their existing
files. Worse, a partial landing is not merely inelegant: `CREATE TABLE IF NOT
EXISTS` adds a missing table but never adds a missing *column*, so a file
written by a build that had stamped version 4 without the `trade.currency`
column would be indistinguishable from a complete one and would produce a
`trade_leg` full of nulls. The schema reaches its final version-4 shape in one
landing, together with the code that populates it. `read_meta`, docs and
examples layer on top safely.

## Risks / Trade-offs

- **[The version bump strands existing recordings]** → unavoidable in the
  writer, possibly avoidable in the readers; see the open question. Mitigated
  in the meantime by the failure being loud and specific — the existing message
  names both versions (`sqlite.py:1283-1286`) — and by nothing in the repo
  needing migration.
- **[`depth` on a very wide book sorts every level to return ten]** →
  `BookSide.levels()` sorts all prices. Read side, off the hot path, and
  `heapq.nsmallest` is available if a measurement ever says it matters. Not
  specced, so it can change freely.
- **[Two spellings of cancel and modify to document]** → real, and the price of
  `config.yaml`'s API guarantee. Mitigated by making the companions delegate,
  so there is one behaviour to document twice rather than two behaviours; the
  docstring for each legacy name points at its companion and vice versa.
- **[`trade` gains a column, so `SELECT *` widens]** → named columns are
  unaffected, and any query against a version-4 file was written after this
  change by definition, since version-3 files are refused.
- **[A `modify` companion that raises on "nothing named" is stricter than the
  dict form]** → intentional, and the stricter direction. `modifyOrder({side,
  qty: None, price: None})` remains legal and remains a no-op modification.

## Open Questions

- **Should the readers accept a window of schema versions rather than one?**
  `check_log`, `read_events` and the proposed `read_meta` currently demand
  `user_version == SCHEMA_VERSION`. The two previous bumps had a reason that
  does not apply here: an absent `event_loss` or `session_end` table is a
  question an old file *cannot answer*, and must not be read as the good answer
  (`sqlite.py:266-274`). An absent `session_meta` table means "the caller
  supplied no metadata", which is a true and complete answer, and an absent
  `trade.currency` column only costs the `trade_leg` view. A reader window of
  `[3, 4]` would therefore let every existing recording go on being read, while
  the **writer** stays exact (a version-3 file opened for writing would gain
  the new table and view but never the new column, so it must keep being
  refused).

  Recommendation: take it, as `MIN_READABLE_SCHEMA_VERSION = 3` with the writer
  unchanged. But it relaxes a refusal that exists for safety reasons, it
  introduces a compatibility policy the module does not currently have, and it
  is exactly the sort of rejection-with-reasoning that leaves no other trace —
  so it is a maintainer decision and, if taken, warrants an ADR rather than a
  design note. Task 1 is the gate; task 3 is written to be correct either way.
