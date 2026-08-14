# Design: book-queries-name-the-instrument

## Context

`openspec/config.yaml`'s scope block says one engine holds many instruments,
every mutating and querying method names one, and a book springs into being on
first mention. The `book-queries` capability was written before that block
replaced the "single instrument, single currency" line, and two of its six
requirements still read as though there were only one book to ask.

## Decision 1: MODIFIED, not a new requirement

The alternative — one ADDED requirement, "read-side queries are scoped to one
instrument", covering both — reads tidier and is wrong for the same reason
`modify-refuses-filled-orders` gave. It leaves both original statements
standing, unamended, still saying "the highest resting bid price", and a
reader implementing best-price meets the under-determined one first. A rule
that needs a second document to be correct is the problem this change is
fixing, not the shape of its fix.

MODIFY also keeps the count of requirements stable, which matters here: the
capability's six requirements map one-to-one onto the six things the read side
answers, and a seventh about scoping would break that correspondence for a
property that belongs inside two of them.

## Decision 2: a scenario each, not a shared one

Each MODIFIED requirement gains its own scoping scenario rather than the two
sharing one. The house convention is one test per ratified scenario, and a
shared scenario would produce a single test that covers two requirements — the
exact arrangement that left these two unguarded in the first place, since
`test_snapshot_and_queries_are_scoped_to_one_instrument` is already a test
that covers them without being bound to them.

## Decision 3: the second instrument sorts before the default

Both new scenarios put the second instrument's prices where a pooled
implementation would visibly return them — inside or across the first
instrument's spread, not merely alongside it. `tests/acceptance/` already has
the pattern and the constant: `OTHER = "AAA"`, chosen to sort before `FAKE` so
an engine answering out of the wrong book answers wrong rather than
coincidentally right.

## Decision 4: leave the other four requirements alone

Last-trade price ("per instrument"), book snapshot ("for an instrument") and
both depth requirements ("for one instrument and one side") already name it.
Rewording them for consistency would be churn against a document that is
already correct.

## Decision 5: no ADR

Nothing is superseded, no option is rejected that leaves no other trace, and
nothing not-yet-written is constrained. The decision *is* the spec text, which
is the case an ADR would only duplicate — the same reasoning
`identifiers-unique-per-engine` recorded for declining one.

## Open questions

None. The behaviour is not in question, only whether the requirement that
should state it does.

## What this does not do

It does not touch the engine, the reference matcher, or any signature. It does
not address the two remaining scope questions raised alongside it during
`identifiers-unique-per-engine`: whether the sink's `orders.priority` column is
a contract, and the absence of any requirement mentioning trade identifiers.
Both are their own changes; neither is an ambiguity in existing text, which is
what this one is about.
