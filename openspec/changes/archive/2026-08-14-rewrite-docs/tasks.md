# Tasks: rewrite-docs

## 1. README

- [x] 1.1 Rewrite README.md: identity, ADR-0001 architecture summary,
      install-from-GitHub (uv + pip forms), quickstart from the new
      example.py, IOC semantics note, measured performance with benchmark
      pointer, contribution notes (OpenSpec planning-only, ./verify, ADR
      index)
- [x] 1.2 Clean example.py's stale narrative comments in the same pass

## 2. Wiki

- [x] 2.1 Clone the wiki repo; inventory existing pages; mark RBTree-era
      pages historical
- [x] 2.2 Write usage walkthrough (new engine) and sink/analytics guide
      (example SQL over a recorded session)
- [x] 2.3 Maintainer reviews wiki drafts before push (external publication)

## 3. Issue hygiene

- [x] 3.1 Draft: close #5 (docs exist), obsolete-notes for #1/#3 (code
      deleted; scenario coverage lives in the lifecycle acceptance tests)
- [x] 3.2 Maintainer approves; only then post

---

Reconciled at archive time, 2026-08-14. Beads were the execution source of
truth (`lob-968.1` .. `lob-968.8`); these boxes are checked from their closure,
not the other way round.

Two things this change did that its tasks did not anticipate, both because a
clarity review (`docs/clarity-review-2026-08.md`) landed between the plan and
the work.

**The README rewrite was a correction, not just a refresh.** It stated two
falsehoods — that no benchmark suite existed, two commits after one shipped,
and a five-item spec list when there are six — plus four more the reviewing
agent found unprompted, including citing ADR-0002 as live after ADR-0005
superseded it. The rewrite also carries what no user-facing document had said:
that one book is one session and a fresh one per episode is the intended and
measured-faster pattern.

**Issue #1's plan was wrong and was not followed.** Task 3.1 called for an
obsolete-note on the ground that the code had been deleted. It had — but the
bug was *fixed* first, by `f751b67` a month after jayd3e reported it. Saying
"the code no longer exists" would have buried the fact he was right, so the
comment says fixed. #3 is genuinely obsolete and says so; the difference
matters to the person who filed it.

All four open issues were answered and closed, leaving the repository with
none. The wiki was rewritten with every code block and query executed against
the shipped library — a harness that caught a stale figure carried over from
an earlier recording.
