# Clarity review — 2026-08-14

> **Status, 2026-08-14.** Four of the five headline changes have landed; the
> first has not, and it is the one the review ranks highest.
>
> - **1, ship `replay()`** — **not done.** `PyLOB` still exports no `replay`,
>   no `cancel`/`modify`, no `depth()`, no `session_meta` and no `trade_leg`,
>   and the only executable replays remain the two private copies in the test
>   suite. These are public API additions, so they go through OpenSpec, and
>   that proposal is not yet written. The finding stands as put.
> - **2, the front door** — done. The README has a benchmarking section that
>   names and runs the harness, an eight-line quickstart run verbatim from a
>   clean venv, the sixth spec it used to omit, and the sessions-and-episodes
>   paragraph. `example.py` says what it teaches and in what order. Both now
>   record that ADR-0002 is superseded by ADR-0005, and quote 439 orders/sec
>   as a historical origin rather than a live figure.
> - **3 and 4, the docstrings** — done, in one documentation-only pass whose
>   ASTs are identical to the previous file's once docstrings are stripped.
>   The three traps moved to where a reader meets them; the 32 bead-id
>   citations were cut to the load-bearing remainder and are now explained by
>   one glossary paragraph; the 3.1x/4.5x contradiction collapsed to
>   ADR-0004. The ~700-line target below was **declined with
>   reasoning** rather than met: it was a whole-repo estimate, and what is
>   left in `engine.py` is per-method contract — spec citations, refusal
>   reasons, replay-coherence markers — whose removal would cost contract
>   rather than archaeology. The protected list survived intact.
> - **5, inspection docs and the WAL trap** — done, and the trap turned out to
>   have a second face the review had not seen: a *checkpointed* lone `.db` is
>   a contiguous prefix of a killed run and is provably indistinguishable from
>   a shorter killed run, so no check inside the file can catch it. Detection
>   being impossible, the error message carries the warning instead.
>
> From "Also filed": `ListSink` shipped, `brain/architecture.md` is bannered,
> and the `quantize`/`clipPrice` alias is flipped so the modern name is the
> definition. The P3 group is being worked through.
>
> One item has since reversed. §2 and the reading map describe a wiki the
> README disavowed; the wiki was rewritten for this engine on 2026-08-14, and
> the README and `pyproject.toml` point at it again. Its `Implementation` page
> is the part kept as history. Nothing below is edited — the findings are the
> record of what was found — and only this note is new.

Requested by the maintainer before the baseline recording run, with a stated
focus: clarity, readability, redundancy, and use by researchers. Five parallel
lenses — a researcher's empirical first day, engine readability, redundancy
and weight, documentation coherence, and the record-then-inspect analytics
workflow — synthesized into one ranked list. Distinct from the two adversarial
correctness reviews (`docs/engine-review-2026-08.md` and the retirement
verification), whose ground this review deliberately did not re-litigate.
Bead: `lob-gv6`. Every P1 was re-verified by the orchestrator.

## Headline

The code is in excellent shape and its in-file documentation is unusually
good — every lens independently praised the error messages (15 of 17
researcher mistakes produced self-correcting messages), the sink's
self-describing schema, and the engine's cost table. The real problems
cluster in three places:

1. **A ratified core workflow — replay — has no shipped entry point.** The
   spec promises it, `events.py` describes it in prose, and the only
   executable versions are two near-identical private copies in the test
   suite that have already drifted apart. The researcher lens's hand-written
   attempt contained a latent bug, which is the finding in miniature: every
   researcher writes copy #3, wrong.
2. **The front door tells two demonstrable falsehoods.** The README denies a
   benchmark suite exists two commits after one shipped; `example.py` opens
   with a 2013 header sending readers to the wiki the README itself disavows,
   under a stale account name.
3. **Prose that addresses past reviewers instead of the next reader.**
   `engine.py` carries 32 unresolvable bead-id citations and narrates one
   incident seven times; its composition is 809 lines of code to 994 of
   docstring. The substance is load-bearing; the archaeology is not.

## The five changes to make first

1. **Ship `replay()`** — promote the test implementation to `src`, re-point
   both test copies at it. Public API addition, so it goes through an
   OpenSpec proposal; the interim fix is a documented recipe.
2. **One truth-and-quickstart pass over README and example.py** — the
   benchmark sentence, the wiki header, an 8-line runnable quickstart, the
   missing recording-sink entry in the spec list, and a sessions-and-episodes
   paragraph (fresh `OrderBook` per episode is the intended and measured-
   faster pattern, currently documented nowhere user-facing).
3. **Three docstring traps** (~30 lines): `configure_instrument`'s
   silent-zero-balance consequence moves from its last sentence to its
   first; a hands-off warning on the mutable `Order`; a real docstring for
   `OrderBook.__init__`, the primary entry point, which currently has none.
4. **The docstring diet with a citation policy** — engine.py sheds ~700
   prose lines to ADR pointers with zero contract loss; one glossary sentence
   explains bead ids; the duplicated-and-contradictory benchmark numbers
   (3.1x here, 4.5x in ADR-0004) collapse to the ADR. Constraint: seven
   "Not replay-coherent" marker phrases are test-enforced verbatim and must
   survive any trim.
5. **Recorded-session inspection docs plus the WAL trap** — the schema
   documentation is excellent but unreachable (the README never names a
   table), and the one verified way a careful researcher loses data to a
   misdiagnosis is copying a killed run's `.db` without its `-wal` sidecar,
   which `check_log` then mislabels as a schema-version error.

## Also filed

**P2:** the modern-API asymmetry (`submit` exists; `cancel`/`modify` do not,
forcing the positional legacy calls that caused the lens's replay bug; no
`depth()` for the price ladder the README sells), bundled with `replay`,
`session_meta` (sweep recordings are anonymous — no seed or episode recorded
anywhere), and a `trade_leg` view into one researcher-ergonomics OpenSpec
proposal. A shipped `ListSink` (reinvented 3–4 times in tests). Flipping the
`quantize = clipPrice` alias so the modern name is the definition.
`brain/architecture.md` still asserts "No tests" and an unfixed issue #8 —
banner it as historical or delete it. **P3:** grouped naming glosses, small
dedups, ~45 lines of verified dead code, message polish.

## What must not be changed

The praise list, consolidated across all five lenses, so later fixes do not
break it: the engine cost table and module-docstring first half; the teaching
error messages; the sink schema's inline DDL comments (self-describing via
`.schema` — only because the comments sit inside the `CREATE` statements);
the killed-run recovery story end to end; `events.py` as a contract; the
deliberately un-DRYed `if self.recording:` sites (the ADR-0002 invariant);
the deliberately independent quantizers in `bench/calibration.py` and
`tests/reference/matcher.py` (load-bearing independence, not duplication);
the test suite's layering, in which essentially no test can fail
indistinctly; the ADR set with its honest supersessions; and zero-ceremony
engine construction (0.8 µs), which is the structural answer to the gym
workflow — no `reset()` API needed once the episode paragraph exists.

## Reading map (to land in the README)

**Researcher:** README → `src/example.py` → `help(PyLOB)`; then per need the
`sinks/sqlite.py` header (schema, recovery), the `events.py` header (balance
rule, replay contract), the `engine.py` header (internals),
`python -m PyLOB.bench --list` (performance). **Contributor:** CLAUDE.md →
`openspec/config.yaml`'s context block → `docs/adr/README.md` →
`openspec/specs/` → `tests/reference/matcher.py` and
`test_emission_coverage.py` as the executable contracts. **Historian:** the
two dated review documents and, once bannered, `brain/architecture.md`.

Estimated deletable weight across the P2/P3 items: ~250 lines of code and
~700 of docstring prose, none load-bearing, each with its risk stated in the
underlying finding.
