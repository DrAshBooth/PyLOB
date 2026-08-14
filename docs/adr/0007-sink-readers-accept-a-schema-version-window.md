# ADR-0007: Sink readers accept a schema-version window; the writer does not

Status: Accepted
Date: 2026-08-14

## Context

`SQLiteSink` stamps `PRAGMA user_version` with `SCHEMA_VERSION` and every
reader — `check_log`, `read_events` — refuses anything else outright
(`sqlite.py:1089`, `elif version != SCHEMA_VERSION`). That has been the rule
through two bumps, 1→2 and 2→3.

The `researcher-ergonomics` change moves the schema to 4: a `session_meta`
table so a sweep's recordings can say which seed produced them, and a
`trade.currency` column so the `trade_leg` view is exact across a session that
re-denominates an instrument. Under the current rule, shipping 4 makes every
recording made before it unopenable — not degraded, not partially readable,
refused.

That cost bought something real in the earlier bumps and buys nothing here,
and the difference is what this ADR turns on. The reason those bumps had to be
hard is that an absent `event_loss` or `session_end` table is a question an old
file **cannot answer**: a reader that treats "no rows" as "no loss" reports a
salvaged run as a clean one, so refusing the file is the only honest option.
An absent `session_meta` table is not that. It means the caller supplied no
metadata, which is a true and complete answer to the question asked. An absent
`trade.currency` column costs only the `trade_leg` view, and only in the
sessions that re-denominate.

The asymmetry between reading and writing matters too. A version-3 file opened
for **writing** would acquire the new table and view from the schema DDL but
never the new column, since SQLite will not retrofit one into existing rows —
so it would present as version 4 while being structurally incapable of
answering what version 4 promises.

## Decision

Readers accept a window: `MIN_READABLE_SCHEMA_VERSION = 3` through
`SCHEMA_VERSION`. `check_log`, `read_events` and `read_meta` open any file in
that range and answer honestly about what an older file does not carry.

The writer is unchanged and stays exact. Opening an older file for writing is
still refused, for the retrofit reason above.

A version bump is therefore no longer automatically a hard cut. Whoever raises
`SCHEMA_VERSION` must now decide, and state, whether the older version stays in
the window — and the test for that is the one this ADR turns on: **can a
reader answer the new question honestly from an old file?** If absence is a
true answer, widen the window. If absence would be read as the good answer to a
question the file cannot answer, do not.

## Alternatives considered

- **Keep demanding equality.** Rejected. It is simpler and consistent with the
  two previous bumps, but it charges every existing recording a migration to
  buy a safety property that is not at stake for these two additions. The
  precedent it sets is worse than the inconsistency it avoids: it makes every
  future additive change cost a re-run, which is a standing tax on exactly the
  workflow the sink exists for.

- **Ship the additions without bumping the version.** Rejected outright. A file
  stamped 3 that contains a `session_meta` table is a file whose stamp is a
  lie, and the whole apparatus depends on the stamp being true.

- **Version each reader against the features it needs, rather than the file as
  a whole.** Rejected as more machinery than the problem has. It is the general
  form of this decision and would be the right answer if the schema grew many
  optional parts; today there is one window with two members, and a
  per-feature matrix would be a policy nobody could hold in their head while
  reading a `SELECT`.

- **Defer the decision and ship the rest of the change first.** Rejected on
  sequencing. A hard cut shipped and later softened costs users a migration
  they did not need, and the migration is the irreversible part.

## Consequences

- **Every recording made before this change goes on being readable.** That is
  the point.

- **The module now has a compatibility policy**, which it did not before. The
  rule above is the policy, and it is a rule about honesty rather than about
  version arithmetic.

- **A reader must now say what it does when a table is absent**, in each place
  it reads one, rather than relying on the version check having made absence
  impossible. That is a real cost and it lands on the readers, which is where
  it can be tested.

- **The writer stays strict, so the two halves now differ.** Anyone reading
  `sqlite.py` will meet a reader that accepts 3 and a writer that does not, and
  the reason is not self-evident from either site alone — both must cite this
  ADR.

- The `researcher-ergonomics` change is unblocked at its task 1.1 gate.
