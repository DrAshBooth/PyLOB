# Proposal: book-queries-name-the-instrument

> **Approved by the maintainer, 2026-08-14.** Accepted as proposed. No
> behaviour change: this ratifies the scoping both implementations already
> follow and that six existing tests already enforce — none of them bound to
> these two requirements, which is the gap. Converted into beads.

## Why

Two `book-queries` requirements describe a query that takes an instrument
without ever naming one.

> Best bid SHALL be the highest resting bid price; best ask the lowest resting
> ask price; worst bid/ask the opposite extremes.

> Volume-at-price for side S at price P SHALL return the total unfulfilled
> quantity of resting S-side orders that an opposite-side order priced at P
> would be eligible to match…

Their neighbours in the same capability do name one: last-trade price is
reported "per instrument", a snapshot is taken "for an instrument", and the
ladder is answered "for one instrument and one side". So the omission is an
asymmetry inside one document, not a house style.

It is not only untidy. With `FAKE` bidding 99 and `AAA` bidding 150 in one
engine, an implementation that pooled the books and answered 150 for
`getBestBid("FAKE")` would be returning, precisely as written, "the highest
resting bid price". Nothing in either requirement or its scenarios forbids it.

What forbids it today is a *different* requirement. "Book snapshot is complete
and consistent" is explicitly per-instrument and says the snapshot "SHALL agree
with the price and volume queries taken at the same moment" — so a pooled
best-price disagrees with a scoped snapshot the moment two instruments hold
depth, and is caught. The scoping is therefore derivable, but only
transitively, and only from a requirement a reader implementing `getBestBid`
has no reason to be reading.

The tests show the same shape. A book lookup that ignores its instrument
argument fails six acceptance tests — and **not one of them binds to either of
these two requirements**:

| test | requirement it binds to |
| --- | --- |
| `test_last_price_is_reported_per_instrument` | Last-trade price (says "per instrument") |
| `test_snapshot_and_queries_are_scoped_to_one_instrument` | Book snapshot (says "for an instrument") |
| `test_cancel_needs_no_instrument` | `order-lifecycle` |
| `test_identifiers_do_not_restart_per_instrument` | `order-lifecycle` |
| `test_a_duplicate_from_another_instrument_is_rejected` | `order-lifecycle` |
| `test_the_cash_leg_moves_the_instruments_own_currency` | `trader-balances` |

Both requirements are protected entirely by neighbours. Neither carries its
own guard, because neither states the thing that would need guarding.

This is the same defect `identifiers-unique-per-engine` fixed in
`order-lifecycle`, and the same argument applies unchanged: two independent
implementations read an under-determined clause and chose correctly, with
nothing in the clause entitling them to. That change was worth ratifying for
exactly this reason, and declining this one would leave the standard applied
to one capability and not its neighbour.

## What Changes

Two MODIFIED requirements in `book-queries`. Each gains the instrument in its
statement and one scenario pinning the scope directly, so the rule is stated
and guarded where a reader implementing that query will meet it.

- **Best and worst prices** — the statement names the instrument; a new
  scenario asserts that a second instrument's resting orders do not move the
  first's best or worst price.
- **Volume at price** — the statement names the instrument; a new scenario
  asserts the same for volume-at-price.

No behaviour changes. The engine and the reference matcher both already answer
per instrument, and every query already takes the instrument as an argument.
`getBestBid`, `getBestAsk`, `getWorstBid`, `getWorstAsk`, `getVolumeAtPrice`
and their signatures are untouched.

## Impact

- Affected specs: `book-queries` (two MODIFIED requirements)
- Affected code: none
- Affected tests: two new acceptance scenarios in
  `tests/acceptance/test_book_queries.py`. The behaviour is already covered
  transitively; what is missing is a test bound to *these* requirements, which
  is the gap this change exists to close.
- Not affected: `openspec/config.yaml`'s protected API list — this is about
  what the specs say, not what the API looks like.
