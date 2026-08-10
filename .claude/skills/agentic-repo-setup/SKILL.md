---
name: agentic-repo-setup
description: Sets up a repository for agentic development. Creates or amends CLAUDE.md and wires up OpenSpec planning, an ADR decision log, a ./verify contract script, Beads issue tracking, and a brain/ directory. Run once per repo. Installs tooling and writes files.
disable-model-invocation: true
user-invocable: true
---

# Agentic repo setup

Gives a repository four things coding agents need: a planning system, a durable decision log, a deterministic definition of done, and a work queue.

**This skill runs only when the user invokes it with `/agentic-repo-setup`.** It is never auto-triggered. It installs global tooling, writes files at the repo root, and depends on a user interview in Phase 1, so the timing is the user's to choose. If it appears relevant to the conversation but has not been invoked, say so and stop; do not read this file and run the phases manually.

Run the phases in order. Each has a halt condition. The phases are ordered by dependency, not by importance, so do not reorder them: `./verify`, `bd remember`, and `brain/` all depend on what comes out of Phase 1.

## Three rules that govern every phase

**Never clobber.** Every file this skill touches may already exist. Amend in place, or back up to `<file>.bak` and say so. Never overwrite a file the user has written.

**Never commit.** Stage nothing, commit nothing, push nothing. Show the user what changed and let them commit. This applies especially after `bd init`, which writes files without announcing them.

**Stop at Phase 8.** Do not continue past it under any circumstance. See that phase for the full list of what "stop" excludes.

---

## Phase 0: Survey

Before touching anything, establish what is already here so later phases amend rather than replace.

```bash
git rev-parse --show-toplevel   # confirm repo root; run everything from here
git status --porcelain          # working tree state
ls -la CLAUDE.md AGENTS.md verify 2>/dev/null
ls -d openspec .beads .claude brain docs/adr 2>/dev/null
command -v openspec bd 2>/dev/null
```

Report a short table of what exists and what is missing. If the working tree is dirty, say so and ask whether to proceed. A dirty tree makes the Phase 5 review meaningless because the user cannot tell your changes from theirs.

**Halt.** Wait for the user to confirm before Phase 1.

---

## Phase 1: Grill the user on intent

Everything downstream depends on this. Do not skip it, and do not substitute your own inference from reading the code.

Check whether the grilling skill is available. It is Matt Pocock's skill, not part of OpenSpec, so it may not be installed:

```bash
ls ~/.claude/skills/grilling*/SKILL.md 2>/dev/null
```

If missing, give the user the install command and wait:

```bash
npx skills add mattpocock/skills --skill "grilling" -g -a claude-code -y
```

Invoke `/grilling` and ask it to interrogate the user on what they are trying to achieve with this repo. You need concrete answers to at least these, because later phases consume them directly:

- What does this repo do, and who or what consumes it?
- What does "working" mean? What would you check before believing a change is safe?
- What is explicitly out of scope?
- What constraints are already fixed (language, runtime, deploy target, data store)?
- What would be expensive to reverse six months from now?

Keep the transcript. Phase 3 needs the "working" answer, Phase 6 needs the run commands, and Phase 7 needs the reversibility answers.

**Halt.** Do not proceed until the grilling session concludes and the user confirms the summary is accurate.

---

## Phase 2: OpenSpec for planning

OpenSpec owns the planning phase. Install if missing, then initialise:

```bash
command -v openspec || npm install -g @fission-ai/openspec@latest
openspec init
```

Populate `openspec/project.md` from the Phase 1 transcript. This file is the project's standing constraints, the things every future change proposal must respect. Keep it to the fixed constraints and conventions, not aspirations.

If the repo already has `openspec/`, leave it alone and only fill gaps in `project.md`.

Report what was created. Do not write any change proposals.

---

## Phase 3: The ./verify contract

`./verify` is a single executable at the repo root that runs every check defining "done". Exit 0 means done. Any non-zero exit means not done. This is the contract all future agent work is graded against.

### Naming collision

Claude Code ships a built-in `/verify` skill that infers its own build-and-run recipe. A repo script called `./verify` is a different thing. Put an explicit disambiguation line in CLAUDE.md (Phase 6) so the built-in defers to the script instead of improvising.

### Propose before writing

Detect the toolchain (`package.json`, `pyproject.toml`, `setup.cfg`, `Cargo.toml`, `go.mod`, `Makefile`, CI config) and propose the check list to the user with the command for each. Do not write the script until they approve the list.

Detection is a starting point, not an answer. Propose only checks the repo can actually run today; a stage that fails because the tool is not configured is worse than no stage. Common shapes, to be confirmed against what is actually installed:

| Ecosystem | format | lint | types | test |
| --- | --- | --- | --- | --- |
| Python | `ruff format --check .` or `black --check .` | `ruff check .` or `flake8` | `mypy src` or `pyright` | `pytest -q` |
| Node | `prettier --check .` | `eslint .` | `tsc --noEmit` | `vitest run` or `jest` |
| Rust | `cargo fmt --check` | `cargo clippy -- -D warnings` | (in build) | `cargo test` |
| Go | `gofmt -l .` | `go vet ./...` | (in build) | `go test ./...` |

For a Python repo with a `src/` layout, run the checks against the package path rather than the repo root so vendored code, notebooks, and generated files do not enter the contract by accident.

**Halt for approval on the list of checks.**

### The 60 second budget

The target is under 60 seconds wall clock to start with. If the approved checks exceed that, do not silently drop any of them. Report the measured time and the offending stage, and ask the user which of these they want:

- Move the slow check to CI and accept a narrower definition of done
- Raise the budget
- Speed the check up (parallelism, incremental mode, scoped test selection)

Silently trimming checks to hit the budget is the worst outcome available here, because it weakens the contract invisibly.

### Script shape

```bash
#!/usr/bin/env bash
set -euo pipefail

start=$(date +%s)
fail=0

stage() {
  local name="$1"; shift
  echo "== $name"
  if ! "$@"; then
    echo "!! $name FAILED"
    fail=1
  fi
}

# === checks: do not add to this block without asking the user first ===
# stage "format" <cmd>
# stage "lint"   <cmd>
# stage "types"  <cmd>
# stage "test"   <cmd>
# === end checks ===

echo "verify: $(( $(date +%s) - start ))s"
if [ "$fail" -ne 0 ]; then
  echo "verify: FAILED"
  exit 1
fi
echo "verify: OK"
```

Run all stages and collect failures rather than bailing on the first one. An agent gets more from one run reporting three failures than from three runs reporting one each.

Then:

```bash
chmod +x verify
./verify
```

Report the actual wall clock time. If any stage fails on a repo that was already green, that is a finding worth surfacing, not something to fix silently.

### The amendment rule

Write into CLAUDE.md, verbatim: checks are never added to `./verify` without asking the user first. The value of a contract is that it does not move underneath the work.

---

## Phase 4: ADR scaffold

OpenSpec archives `design.md` with its change, so architectural rationale drops out of the read path once a change ships. ADRs live outside the change folder and stay readable by future proposals.

Create the scaffold only. Do not backfill ADRs for decisions already in the codebase.

```
docs/adr/
├── README.md          # index: one line per ADR, nothing more
└── 0000-template.md
```

Template:

```markdown
# ADR-NNNN: <short imperative title>

Status: Proposed | Accepted | Superseded by ADR-NNNN
Date: YYYY-MM-DD

## Context
What forced a decision. The constraints in play at the time.

## Decision
What was chosen, stated plainly.

## Alternatives considered
What was rejected and why. This section is the reason the ADR exists.

## Consequences
What this makes easy, what it makes hard, what it forecloses.
```

### The trigger rule

Put this in CLAUDE.md so agents apply it without being asked. Write an ADR when any of these hold:

- The decision constrains a change proposal that has not been written yet (cross-cutting, not scoped to one capability)
- The decision is a rejection. Rejections produce no spec delta and no code, so without an ADR they leave no trace at all and get re-litigated
- The decision reverses or narrows an existing ADR. Supersede rather than edit

Otherwise the rationale stays in the change's `design.md`. Writing an ADR for every change duplicates content that will then drift, and contradictory guidance is worse for an agent than sparse guidance.

Reconcile at archive time, not at proposal time. Implementation changes decisions, and the archive step is the only point where you know what was actually decided.

Keep `docs/adr/README.md` to one line per ADR. The entire point is that an agent can load the index cheaply rather than reading forty files.

---

## Phase 5: Beads

### Snapshot first

`bd init` writes files including AGENTS.md and possibly `.claude/` entries. In a repo that already has these, that is a clobber. Snapshot before running.

Use a fresh temp directory per run and keep the path in a variable. A fixed path such as `/tmp/pre-bd` silently mixes snapshots across repos and across reruns, which makes the restore in the review step untrustworthy:

```bash
PRE_BD="$(mktemp -d "${TMPDIR:-/tmp}/pre-bd.XXXXXX")"
echo "snapshot: $PRE_BD"
for f in AGENTS.md CLAUDE.md .claude; do
  [ -e "$f" ] && cp -R "$f" "$PRE_BD/"
done
git status --porcelain > "$PRE_BD/status.txt"
```

Report `$PRE_BD` to the user. Shells do not persist between tool calls, so record the literal path rather than relying on the variable surviving to the review step.

### Install and init

```bash
command -v bd || brew install beads        # macOS / Linuxbrew
bd init
```

Without Homebrew, install from the project's releases: https://github.com/gastownhall/beads

### Show everything, then halt

Show the user:

- Every file created or modified, from `git status --porcelain` diffed against the snapshot
- The **full contents** of AGENTS.md, not a summary
- The full contents of anything written under `.claude/`
- Anything `bd init` overwrote, with the snapshot available to restore from

**Halt.** Do not commit. Do not proceed until the user has reviewed this and said to continue.

### bd remember: operational facts only

After approval, record exactly three lines. These are the facts every session needs before it can do anything, and nothing else:

```bash
bd remember "verify: run ./verify from the repo root; exit 0 is the definition of done"
bd remember "run locally: <exact command from Phase 1>"
bd remember "entry point: <path from Phase 1>"
```

Nothing about goals, roadmap, strategy, architecture, or intent. Those belong in `openspec/project.md`, `docs/adr/`, and `brain/` respectively. Beads compacts old memory over time, so anything durable stored here degrades by design.

**Create no beads.**

---

## Phase 6: CLAUDE.md

Write or amend now, once, with real paths that exist. If CLAUDE.md already exists, add the missing sections and leave everything else untouched.

Keep it short. A bloated CLAUDE.md is a known failure mode: when real rules sit among speculative ones the model weighs them equally and follows none.

```markdown
@AGENTS.md

## Planning

Planning happens in OpenSpec. Do not start implementation from a chat message.
Propose a change (`openspec/changes/<id>/`), get it reviewed, then implement.
Standing project constraints live in `openspec/project.md`.

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
```

Import AGENTS.md with `@AGENTS.md` rather than copying the bd block into CLAUDE.md. Claude Code reads CLAUDE.md and not AGENTS.md, so the import is what makes bd's own instructions visible, and duplicating them creates two copies that drift.

**Do not reference `brain/` anywhere in CLAUDE.md.** See the next phase.

---

## Phase 7: brain/architecture.md

Draft `brain/architecture.md` from the conversation so far: the decisions made and why, weighted toward the ones that would be expensive to reverse.

Constraints:

- **One page maximum.** If it runs longer, cut, do not continue onto a second page
- **Not referenced from CLAUDE.md, AGENTS.md, or any auto-loaded file.** This is pulled on demand. Referencing it defeats the purpose by dragging it into every session's context
- **The user will edit it.** Write it as a draft to be corrected, not a finished artefact. Flag anything you inferred rather than heard
- **Cite ADR IDs, do not restate them.** When a decision has an ADR, link the ID and move on. Duplicating the reasoning here creates the drift this split exists to avoid

Suggested shape:

```markdown
# Architecture

## What this is
Two or three sentences.

## Decisions that would be expensive to reverse
For each: the decision, why, and what reversing it would cost.

## Decisions that are cheap to revisit
Briefly. Useful mainly so nobody treats them as settled.

## Known unknowns
What we have not decided yet and are deliberately deferring.
```

---

## Phase 8: Stop

Report what was created and modified, grouped by phase, and tell the user nothing has been committed.

Then stop. Specifically, do not:

- Create beads or an epic
- Write an OpenSpec change proposal
- Plan the next phase of work
- Backfill ADRs for existing decisions
- Suggest improvements to `./verify`, CLAUDE.md, the ADR scaffold, `brain/`, or anything else set up here
- Start implementing anything

The user drives what happens next. The whole value of an explicit stop is that the setup can be reviewed before anything is built on top of it.
