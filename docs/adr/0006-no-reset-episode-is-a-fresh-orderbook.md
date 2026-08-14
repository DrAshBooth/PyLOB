# ADR-0006: There is no `reset()`; an episode is a fresh `OrderBook`

Status: Accepted
Date: 2026-08-14

## Context

The primary use case is episodic: an RL gym or a parameter sweep runs a great
many short simulations and needs each to start from a clean book. Every
framework in that space names the operation `reset()`, so its absence here
reads as an oversight, and the pre-retirement review found the question
answered nowhere a user would look (`lob-fcq`).

Three facts about the engine bear on it.

**The order store is never pruned.** `_orders` maps `idNum` to `Order` for the
life of the book. That is not an accident of implementation: `order-lifecycle`
requires identifiers unique "across every instrument the engine holds, for
that engine's lifetime, including across reloads of persisted state", and
`create_order` enforces it by testing membership of the store — so the store
*is* the uniqueness check. (That clause read "within the book's lifetime" when
this ADR was written; `identifiers-unique-per-engine` resolved the ambiguity
without changing the behaviour, which only strengthens the argument below —
the space a `reset()` would have to clear is engine-wide.) A filled or cancelled order also stays
addressable, because the acceptance surface asks a finished order for its
`fulfilled` and `commission` long after it has left the book.

**So a long-lived book grows without bound**, measured at ~350 bytes of
process memory per order submitted (1M orders: 356 MB; 2M: 692 MB; linear, of
which the store itself is a steady 186 B/order). At ten million orders in one
process that projects to ~3.5 GB.

**And reconstruction is cheap.** A bare `OrderBook` is 0.4 µs; one with an
instrument and twenty traders configured is 8.5 µs.

All re-measured for this ADR: Apple M1, Python 3.11.11, the `mixed-v1`
workload, load average 2–4. That machine is never quiet, so read the ratios
rather than the absolutes, as ADR-0005 requires of every figure here.

## Decision

**There is no `reset()`, and `close()` does not clear state. An episode is a
fresh `OrderBook`.**

`close()` ends a session: it flushes the sink and does nothing else. It leaves
the book, the store and the ledgers exactly as they were, because a closed
session is a finished object to be read, not a recycled one.

The pattern this leaves is the one the documentation now prescribes
(README, "Sessions and episodes"), and it is not a consolation prize. Building
a new engine per episode measured **faster** than driving every episode
through one book — ~183k against ~167k orders/sec over a hundred episodes of
ten thousand orders, with per-episode construction and teardown inside the
timed region. Two things compound there and the measurement does not separate
them: insertion into a store grown to a million entries, and a book that
carries every earlier episode's resting orders into the next episode's
matching. The second matters more, and it is not really a performance point
at all: in a reused book, episode *n* opens against episode *n-1*'s depth,
which is not what an episodic experiment means by an episode.

## Alternatives considered

- **Add `reset()`, clearing the store and the book.** Rejected, and this is
  the option the ADR exists to close. Clearing `_orders` would discard the
  membership test that makes identifiers unique for the book's lifetime, so
  either the contract breaks — a data-replay caller's supplied `idNum` could
  silently address a second order — or the counter's high-water mark is
  carried across the reset, in which case the object is not reset but merely
  emptied, and the caller gets a fresh book whose first identifier is some
  large number they cannot account for. Nor would it buy back the cost:
  releasing a million retained orders is the same deallocation work whichever
  reference drops last, so the ~84 ms per million is paid either way, and the
  ~10% that starting empty measured is given up on top.
- **Make `close()` clear state, so a closed book can be reused.** Rejected.
  It overloads "the session ended" with "the session begins again", and it
  breaks reading a finished session: `close()` is what a recorded run calls
  before its balances and its filled orders are inspected.
- **Prune finished orders from the store, keeping the counter.** Rejected as a
  contract change wearing a memory optimisation's clothes. A filled order
  answering for its `fulfilled` and `commission` is acceptance-tested surface,
  and the pruned book would answer `None` for an order the caller holds a
  receipt for. If the retention ever does become the binding constraint, the
  honest form is a proposal against `order-lifecycle`, not a quiet sweep.
- **Say nothing and let users discover the pattern.** Rejected — it is what
  the project did until now, and it leaves the obvious mistake unguarded: a
  user reaches for `reset()`, does not find one, and reuses a single book
  across episodes, which is at once the slow arrangement, the unbounded one,
  and the one that leaks state between episodes.

## Consequences

- **Episode state cannot leak between episodes**, because there is no shared
  object for it to leak through. No `reset()` can be incomplete, and no test
  is needed to prove it is not.
- **Memory is bounded by the longest single episode**, not by the length of
  the sweep.
- **A sweep that wants cross-episode identity has to build it itself.** Order
  identifiers are unique within a book, so two episodes both number from 1.
  Anything correlating orders across episodes needs an episode key of its own;
  the sink does not record one, which the clarity review filed as
  `session_meta` (`docs/clarity-review-2026-08.md`, "Also filed").
- **Teardown moves into the episode loop**, where it is proportional (1.1 ms
  for a 5,000-order engine) rather than deferred into one 84 ms-per-million
  pause at the end.
- **`reset()` is now a decision to be reversed rather than a gap to be
  filled.** A future proposal for one supersedes this ADR and has to answer
  the identity argument above.
