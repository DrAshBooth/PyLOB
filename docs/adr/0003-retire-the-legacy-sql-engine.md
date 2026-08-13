# ADR-0003: Retire the legacy SQL engine

Status: Accepted
Date: 2026-08-13

Supersedes the transition clause of [ADR-0001](0001-inmemory-matching-sqlite-sink.md).

## Context

ADR-0001 moved matching in-memory and kept the SQL engine in tree "as a
cross-check oracle during the transition", with the condition that it stay
"until the in-memory engine passes the same test suite". That condition is
satisfied: the in-memory engine passes all 46 acceptance parameterizations
with zero xfails, and every one of the 13 remaining xfails is `[legacy]`.

The pre-retirement review (`docs/engine-review-2026-08.md`) then found six P1
defects, all now fixed, and one structural risk: 45 of 99 injected mutants
would have survived retirement, because five money-and-book mutants were killed
only by the differential harness comparing against the legacy engine. That
risk is closed — the post-fix mutation run with the oracle excluded leaves
three survivors, all deliberate no-op controls.

The maintainer has decided to retire the legacy engine and carry a single
engine forward.

## Decision

**Delete the legacy SQL engine and everything that exists only to serve it.**
`PyLOB.OrderBook` is the in-memory engine and the only engine. The public API
that `openspec/config.yaml` protects — `processOrder`, `cancelOrder`,
`modifyOrder`, `getVolumeAtPrice`, `getBest*`/`getWorst*`, `print` — is kept,
because that constraint is about the API's shape, not about which engine
implements it.

**The oracle is replaced, not merely deleted.** This is the load-bearing part
of the decision and it must precede the deletion. The review wrote an
independent reference matcher from the frozen specs — a flat list re-sorted by
`(price, priority)`, sharing no code with the engine — and it agreed with the
engine over 600,000 operations. That matcher is promoted into the test suite as
the differential oracle, and the legacy comparison is retired against it rather
than into nothing.

The replacement is strictly better than what it replaces. The legacy engine was
an oracle by accident of history: it disagreed with the specs in nine known
ways, which is why the harness needed a whitelist and nine generator
constraints to avoid the disagreements. A spec-derived matcher has no such
divergences, needs no whitelist, and can be driven with inputs the legacy
harness had to exclude by construction — half-tick prices, supplied
identifiers, invalid submissions, reloads, multiple instruments.

## Consequences

**Deleted:** `src/PyLOB/orderbook.py`, `src/create_lob.sql`, the twelve
`src/PyLOB/*.sql` query files, `src/lob.db`, `src/lob.html`, the
`LegacyOrderBook` export, `LegacyAdapter` and the engine registry's legacy
entry, all 20 `engine_xfail` markers, `tests/test_lifecycle.py` (legacy-only),
and the legacy half of `tests/test_issue8_regressions.py`.

**The nine legacy defects close as won't-fix**, with the code they describe.
`lob-0bl`, `lob-crf`, `lob-ihv`, `lob-0rb`, `lob-a17`, `lob-7e7`, `lob-pn3`,
`lob-bis`, `lob-z45` are all fixed by construction in the surviving engine and
pinned by acceptance tests that no longer need a marker.

**`./verify`'s smoke stage loses its schema build.** It exists to prove
`create_lob.sql` still parses; with the file gone the stage runs `example.py`
alone. The stage list is unchanged, so the amendment rule does not apply.

**`openspec/config.yaml`'s `context:` block is rewritten.** It currently says
matching "is moving" to an in-memory engine and that the legacy engine stays
until a condition that has now passed; it also describes SQL files loaded as
attributes, which was only ever true of the deleted engine.

**Archived OpenSpec changes are not edited.** They are the historical record of
what was proposed and done at the time. This ADR is where the change of state
is recorded.

**What is genuinely lost:** an independent *implementation* — code written by
someone else, at another time, that happened to agree. The reference matcher is
written from the same specs by the same broader process that wrote the engine,
so a shared misreading of a spec would not be caught by it, where the legacy
engine might have caught it. That is a real reduction in one dimension of
assurance, accepted because the legacy engine's own nine spec divergences meant
it was already a weak oracle for exactly the cases where the specs are subtle.

## Alternatives considered

- **Keep the legacy engine indefinitely as an oracle.** Rejected. It costs the
  maintenance of a dead engine, keeps nine known-broken behaviours in shipped
  code, and forces every differential workload through nine generator
  constraints that exclude the most interesting inputs. Its value as an oracle
  was already limited to the cases where it happened to be right.
- **Delete it without replacing the oracle.** Rejected — this is the failure
  the review predicted, and the reason the retirement was gated in the first
  place.
- **Keep it but stop shipping it (test-only dependency).** Rejected as the
  worst of both: the maintenance cost stays, the nine defects stay in the
  differential harness's way, and the "is this shipped?" question gets a
  different answer in every file.
