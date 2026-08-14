"""What provenance is allowed to remember, and what it must ask again.

A capture spawns processes -- `git`, `sysctl`, `pmset` -- and the suite takes
twenty of them, so the module memoises. Memoising provenance is a sharper
knife than memoising most things: the module's entire job is to describe the
machine a number was taken on, and a remembered answer that has since stopped
being true does not merely go stale, it explains a surprising number with
something that did not happen.

So the tests here are two-sided, and the second side is the one that matters:

**Identity may be remembered.** The chip does not change under a running
process, and neither does the identity of the code the process imported. Those
answers are taken once.

**State may not be.** Power source, load average and a dirty working tree are
readings of a moment, and the moment is the measurement's. `dirty` is the
sharpest of the three: it exists to say *the sha does not describe this code*,
and `baselines.quiet_machine_complaints` refuses to record a baseline on the
strength of it. A remembered `False` would disarm that check on precisely the
runs it was written for -- and this repo is routinely edited by more than one
process at once, so the flag genuinely does flip mid-run.

Nothing here asserts a duration. The saving is measured, not tested: a test
that failed when a machine was busy would be the false regression ADR-0005
exists to stop.
"""

import pytest
from PyLOB.bench import provenance


@pytest.fixture(autouse=True)
def cold():
    """A module that has remembered nothing, before and after each test.

    Before, so a test's first call is genuinely its first. After, so the fakes
    below cannot be left behind for the real suite to record a baseline from --
    the memo is process-wide, which is the point of it and also its only
    hazard.
    """
    provenance._PROCESS_FACTS.clear()
    yield
    provenance._PROCESS_FACTS.clear()


class FakeGit:
    """A stand-in for `_git` that counts what it was asked and can change answer."""

    def __init__(self, **answers):
        self.answers = answers
        self.asked = []

    def __call__(self, *args):
        self.asked.append(args[0])
        return self.answers.get(args[0])

    def count(self, subcommand):
        return self.asked.count(subcommand)


def test_the_commit_identity_is_asked_for_once_per_process(monkeypatch):
    """Two captures, one `rev-parse`.

    Roughly 22 ms of a 50 ms capture was two `git` invocations answering
    "which commit is this?" -- a question whose answer was fixed before the
    process started.
    """
    git = FakeGit(**{"rev-parse": "abc1234", "status": ""})
    monkeypatch.setattr(provenance, "_git", git)

    first = provenance._commit()
    second = provenance._commit()

    assert first["sha"] == second["sha"] == "abc1234"
    assert git.count("rev-parse") == 2, "one for the sha, one for the branch, once"


def test_the_dirty_flag_is_taken_fresh_at_every_capture(monkeypatch):
    """The tree moves under a running process; the flag has to follow it.

    Recorded provenance says "this measurement was taken against a tree that
    did not match its sha". That is a claim about the moment of measurement,
    not about the process, and here the tree is dirtied between two captures
    exactly as a concurrent editor would dirty it.
    """
    git = FakeGit(**{"rev-parse": "abc1234", "status": ""})
    monkeypatch.setattr(provenance, "_git", git)

    clean = provenance._commit()
    git.answers["status"] = " M src/PyLOB/engine.py"
    dirtied = provenance._commit()

    assert clean["dirty"] is False
    assert dirtied["dirty"] is True, "a remembered clean tree would be a false alibi"
    assert dirtied["sha"] == clean["sha"], "the sha is still the one from before"


def test_a_git_call_that_failed_is_retried_rather_than_remembered(monkeypatch):
    """A momentary failure must not become this process's permanent answer.

    `_git` returns None both when there is no git and when a call lost a race
    -- an index lock held by another process in the same checkout, a timeout.
    Keeping the second kind would stamp `"sha": null` on every baseline the
    process went on to record, which is worse than the spawn it saved.
    """
    git = FakeGit(**{"status": ""})
    monkeypatch.setattr(provenance, "_git", git)

    assert provenance._commit()["sha"] is None

    git.answers["rev-parse"] = "abc1234"

    assert provenance._commit()["sha"] == "abc1234"


def test_the_hardware_is_described_once(monkeypatch):
    """CPU brand and cluster topology: asked once, however often they are read.

    Four `sysctl` spawns per capture, and `_core_counts` is read twice per
    measurement -- once for the report and once to decide whether a core-class
    probe is even meaningful.
    """
    asked = []

    def fake_sysctl(key):
        asked.append(key)
        return {
            "machdep.cpu.brand_string": "Fake M1",
            "hw.nperflevels": "2",
            "hw.perflevel0.logicalcpu": "4",
            "hw.perflevel1.logicalcpu": "4",
        }.get(key)

    monkeypatch.setattr(provenance, "_sysctl", fake_sysctl)

    brands = [provenance._cpu_brand() for _ in range(3)]
    counts = [provenance._core_counts() for _ in range(3)]

    assert brands == ["Fake M1"] * 3
    assert [count["performance"] for count in counts] == [4, 4, 4]
    assert sorted(asked) == sorted(set(asked)), "asked the same key twice"


def test_core_counts_are_not_a_dict_shared_between_captures(monkeypatch):
    """Every caller gets its own.

    The counts land in a provenance record that its owner is free to edit --
    `runner.measure` adds fields to the dict `capture` returns. Handing out one
    memoised dict would let one run's record rewrite the next one's.
    """
    monkeypatch.setattr(provenance, "_sysctl", lambda key: None)

    first = provenance._core_counts()
    first["logical"] = "tampered"

    assert provenance._core_counts()["logical"] != "tampered"


def test_the_machine_state_is_read_again_for_every_capture(monkeypatch):
    """Power source and load average are asked once per capture, not once ever.

    ADR-0005 names both as things that explain a surprising number: a laptop
    moved to battery mid-suite, a machine that got busy. Remembering either
    would report the conditions of the first measurement for all of them.
    """
    calls = {"power": 0, "load": 0}

    def counted(key, value):
        def call():
            calls[key] += 1
            return value

        return call

    monkeypatch.setattr(provenance, "_power_source", counted("power", "ac"))
    monkeypatch.setattr(provenance, "_loadavg", counted("load", [1.0, 1.0, 1.0]))

    provenance.capture()
    provenance.capture()

    assert calls == {"power": 2, "load": 2}


def test_a_second_capture_still_reports_every_field():
    """The memo is an optimisation, so the second answer looks like the first.

    Same shape, same identity, and the state fields present rather than
    dropped -- a cache that quietly changed the record's shape after the first
    call would be found by whichever consumer read the second one.
    """
    first = provenance.capture()
    second = provenance.capture()

    assert set(first) == set(second)
    assert first["commit"]["sha"] == second["commit"]["sha"]
    assert first["cpu"] == second["cpu"]
    assert first["cores"] == second["cores"]
    assert second["power_source"] in ("ac", "battery", "unknown")
    assert set(second["commit"]) == {"sha", "branch", "dirty"}
