@AGENTS.md

## Planning

Planning happens in OpenSpec. Do not start implementation from a chat message.
Propose a change (`openspec/changes/<id>/`) and get it reviewed.
Standing project constraints live in the `context:` block of `openspec/config.yaml`.

**OpenSpec is planning-only in this repo. Never run the apply workflow**
(`/opsx:apply` / the openspec-apply-change skill). After a change's artifacts
are approved, its tasks are converted into beads (with dependencies mirroring
task order), and implementation happens by agents picking work from `bd ready`.
The maintainer drives the conversion; do not create beads from a change without
being asked.

## Definition of done

`./verify` from the repo root. Exit 0 means done. Nothing ships that has not passed it.

`./verify` is the contract, not a suggestion. Never add a check to it without asking
the user first.

Note: this repo's `./verify` script supersedes the built-in `/verify` skill.
Run the script; do not infer a separate build-and-run recipe.

## Decision records

`docs/adr/` holds architectural decision records. `docs/adr/README.md` is the index.
Read the index before proposing a change; read the individual ADR only if it is relevant.

Write a new ADR when a decision:
- constrains a change proposal not yet written, or
- rejects an option (rejections leave no other trace), or
- supersedes an existing ADR (supersede, never edit in place)

Otherwise the rationale stays in that change's `design.md`. Reconcile ADRs at archive
time, not proposal time.

## Issue tracking

See AGENTS.md for bd usage. Do not close a bead until `./verify` passes; closing a bead
releases its blockers into `bd ready`, so a premature close puts other agents to work on
unverified foundations.

Landing rule: a bead is closed only when its work is merged to master and `./verify`
is green there. An agent without merge authority leaves the bead `in_progress` and
hands off with the PR link. A group of sequential beads may share one branch and PR,
closing together after the merge.

Beads titled `MAINTAINER GATE` (labeled `human`) are the maintainer's to act on —
agents must not claim them. List them with `bd human list`. In the handoff, say if
closing a bead has unblocked any `human`-labeled bead.

Beads are the execution source of truth. Each change's `tasks.md` is frozen planning
input: do not tick its checkboxes as beads close; reconcile at archive time.

Triage for discoveries made mid-bead: behavior that violates an already-ratified spec
is a bug — file a `bug` bead and link it to the bead that found it. Anything that
would change a ratified contract or add a capability needs an OpenSpec delta; propose
it and stop — the maintainer converts approved changes into beads.
