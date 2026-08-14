"""What was true of the machine while the number was being taken.

ADR-0005: "Every run records provenance -- machine, CPU brand, core counts,
Python version, commit, load average, power source -- so a surprising number
can be explained rather than merely disbelieved."

Calibration corrects for a uniformly slower machine. Provenance is for
everything it cannot correct for, and its job is to make the failure modes
*legible*. Three of them have already bitten this project:

- **Power source.** On a laptop, battery versus mains changes the sustained
  clock. ADR-0005 names it explicitly.
- **Core class.** An M1 has four performance and four efficiency cores. A
  single-threaded run that lands on an efficiency core reads far slow for
  reasons entirely unconnected to the code -- measured here at 3.16 GHz
  against 1.04 GHz, which is worse than the ~40% the ADR quotes.
- **Load.** The ADR records a spread of 49k to 114k orders/sec for one
  workload inside a single interleaved loop, with load average between 1.9
  and 325.
- **Interpreter build.** Not a machine property at all, and the one thing
  calibration cannot correct for: the judged quantity is a ratio of two Python
  programs and two CPython builds do not agree on it. `interpreter()` records
  the identity and `baselines.compare` refuses across a mismatch rather than
  scaling one build's number by another's.

Everything in this module is best-effort and degrades to `None` rather than
raising. A benchmark that refused to run because it could not read a battery
would be a worse tool than one that says "power source: unknown".

Asking for a performance core
-----------------------------

`request_performance_core` sets the thread's Quality of Service class to
`USER_INTERACTIVE`, which on Apple silicon is a request for a P-core; the
runner does this by default. ADR-0005 rejected pinning as *the whole answer*
-- it makes the measurement stricter rather than portable -- while saying the
harness should do it and report it. So it is a request, recorded in the
provenance either way, and it does not fail anything if the scheduler ignores
it.

Detecting which core class we got
---------------------------------

There is no public API for "which core am I on". What there is, on Darwin, is
`proc_pid_rusage(RUSAGE_INFO_V4)`, which reports the *cycles* elapsed against
the process. Cycles divided by CPU seconds is the effective clock, and on a
heterogeneous CPU the P and E clusters are far enough apart to tell apart.

The threshold is not hardcoded, because hardcoding 2.06 GHz would be a fact
about one chip. Instead `measure_core_class` takes a short reference sample
under `BACKGROUND` QoS -- which Darwin confines to the efficiency cluster --
and classifies the run's clock against the E-cluster clock it just measured on
this machine. That is roughly 40 ms of extra work and it is done *after* the
timed region, so it cannot perturb what it is describing.

If any part of that is unavailable -- not Darwin, not heterogeneous, the
`ctypes` call fails, the numbers are implausible -- the answer is `None` and
the report says the core class is unknown. That is the "without heroics" line:
this is one `ctypes` signature and one plausibility check, and it gives up
rather than trying harder.

What is remembered and what is re-read
--------------------------------------

A capture used to cost about 50 ms, nearly all of it spawning processes:
three `git` calls, four `sysctl`s and one `pmset`. The suite captures twenty
times, so it spent over a second re-asking questions whose answers had not
moved.

The line is not "what is expensive". It is *what can change while this process
runs*:

- **Remembered for the life of the process.** The CPU brand string and the
  core-count topology -- no process is handed a different chip halfway
  through -- and HEAD's short sha and branch. The sha answers *which code is
  this*, and the code running in this process was read off disk when it was
  imported; a commit landing in the checkout afterwards (routine when several
  agents share a worktree) does not retroactively change what got imported.
- **Read again on every capture.** Power source, load average, and whether the
  working tree is dirty. Those describe the machine and the tree *at the
  moment of the measurement*, which is the entire reason ADR-0005 asks for
  them. Remembering one would not make provenance faster so much as make it
  lie: a cached battery reading reports mains power for a run taken on
  battery, and a cached clean tree reports that the sha describes code it does
  not. Stale provenance is worse than slow provenance, because it explains a
  surprising number with something that was not true.

The efficiency-cluster reference in `measure_core_class` is a *measurement* of
this machine now, not a constant about it, so it is taken fresh too.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, TypeVar

__all__ = [
    "CoreClock",
    "capture",
    "interpreter",
    "measure_core_class",
    "repo_root",
    "request_performance_core",
]

_DARWIN: Final = sys.platform == "darwin"

_T = TypeVar("_T")

#: Answers to the questions above that cannot change while this process runs.
#: Written only by `_remembered`; a test that wants a cold module clears it.
_PROCESS_FACTS: dict[str, Any] = {}

#: `qos_class_t` values from <sys/qos.h>.
_QOS_USER_INTERACTIVE: Final = 0x21
_QOS_DEFAULT: Final = 0x15
_QOS_BACKGROUND: Final = 0x09

#: `struct rusage_info_v4` is a 16-byte uuid followed by 35 uint64 fields.
#: `ri_cycles` is the 31st of them.
_RUSAGE_INFO_V4: Final = 4
_RI_FIELDS: Final = 35
_RI_CYCLES: Final = 30


class _RUsageInfoV4(ctypes.Structure):
    _fields_ = (
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_fields", ctypes.c_uint64 * _RI_FIELDS),
    )


def _libc() -> ctypes.CDLL | None:
    if not _DARWIN:
        return None
    try:
        name = ctypes.util.find_library("c")
        return ctypes.CDLL(name, use_errno=True) if name else None
    except OSError:
        return None


_LIBC: Final = _libc()


def _remembered(key: str, compute: Callable[[], _T]) -> _T:
    """Call `compute` once per process -- but keep only a successful answer.

    Everything in this module degrades to `None`, and it does so for two
    reasons that look identical from here: the tool is absent (a permanent
    answer) or this particular call did not work (a `git` index lock held by
    another process in the same checkout, a timeout, a spawn that lost a race
    with the machine's load). So `None` is never kept. The permanent case pays
    for one cheap retry per capture, and the transient case does not get frozen
    into every provenance record this process goes on to write -- which is the
    failure that would matter, since a baseline is recorded from one.
    """
    if key in _PROCESS_FACTS:
        return _PROCESS_FACTS[key]
    value = compute()
    if value is not None:
        _PROCESS_FACTS[key] = value
    return value


def _sysctl(key: str) -> str | None:
    """One `sysctl -n` value, or None if the key or the tool is missing."""
    if not _DARWIN:
        return None
    try:
        out = subprocess.run(
            ["sysctl", "-n", key], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


def _static_sysctl(key: str) -> str | None:
    """`_sysctl` for a key that describes the hardware rather than its state.

    Remembered, because the hardware is. Anything whose value moves while the
    machine runs -- load, power, thermals -- must call `_sysctl` directly and
    pay for its spawn every time.
    """
    return _remembered("sysctl:%s" % key, lambda: _sysctl(key))


def _cpu_brand() -> str | None:
    """The processor's brand string: a fact about the chip, so asked once.

    Only the `sysctl` is remembered. The `/proc/cpuinfo` fallback below reads a
    virtual file rather than spawning anything, and the cost this is about is
    the spawn.
    """
    brand = _static_sysctl("machdep.cpu.brand_string")
    if brand:
        return brand
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def _core_counts() -> dict[str, int | None]:
    """Performance and efficiency logical core counts where they are knowable.

    Darwin names its clusters `hw.perflevelN`, level 0 being the fastest. On a
    homogeneous CPU there is one level and both counts are the total, which is
    the honest answer: every core is a performance core when there is only one
    kind.

    The `sysctl` answers are remembered -- a cluster layout is a property of
    the chip -- but the dict is rebuilt around them on every call, both so a
    caller that stores it in a provenance record cannot mutate the copy the
    next caller gets, and so `os.cpu_count()`, which costs nothing and is the
    one figure here a kernel could in principle change under us, stays live.
    """
    total = os.cpu_count()
    levels = _static_sysctl("hw.nperflevels")
    if levels is None:
        return {"logical": total, "performance": None, "efficiency": None}
    try:
        count = int(levels)
    except ValueError:
        return {"logical": total, "performance": None, "efficiency": None}
    if count < 2:
        return {"logical": total, "performance": total, "efficiency": 0}

    def level(index: int) -> int | None:
        raw = _static_sysctl("hw.perflevel%d.logicalcpu" % index)
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    return {"logical": total, "performance": level(0), "efficiency": level(1)}


def _power_source() -> str:
    """ "ac", "battery" or "unknown"."""
    if _DARWIN:
        try:
            out = subprocess.run(
                ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        text = out.stdout
        if "'AC Power'" in text:
            return "ac"
        if "'Battery Power'" in text:
            return "battery"
        return "unknown"
    for name in ("AC", "ACAD", "AC0", "ADP0", "ADP1"):
        path = "/sys/class/power_supply/%s/online" % name
        try:
            with open(path, encoding="ascii") as handle:
                return "ac" if handle.read().strip() == "1" else "battery"
        except OSError:
            continue
    return "unknown"


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ("git", *args), capture_output=True, text=True, timeout=10, cwd=repo_root()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def repo_root() -> Path:
    """The checkout this package was imported from: four levels above this file.

    `os.getcwd()` would name whatever directory the benchmark happened to be
    invoked from, which on a worktree-per-agent layout is routinely a different
    checkout from the one that was imported. Every consumer of "where is the
    repository" asks here -- provenance's `git` calls and `__main__`'s default
    baselines path -- so the two cannot count directories differently.
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def _commit() -> dict[str, Any]:
    """Which commit this is, and whether the tree still matches it.

    `sha` and `branch` are remembered. They answer "which code is this?", and
    the code in this process was read off disk at import: a commit landing in
    the checkout while the process runs does not change what was imported, so
    the first answer is the one that describes the measurement, and it is two
    `git` spawns at ~11 ms each -- most of the cost of a capture.

    `dirty` is not remembered, and must not be. It is not a property of HEAD
    but a reading of the working tree at the moment of the measurement, and
    trees move under running processes: an editor saves, a build writes, an
    agent in the next worktree over edits a shared file. Its whole job is to
    say *do not trust the sha* -- `baselines.quiet_machine_complaints` refuses
    to record a baseline from a dirty tree on the strength of it -- so a
    remembered `False` would quietly disarm the check it exists to arm, on
    exactly the runs where it matters. It stays live, at one `git status` per
    capture.
    """
    sha = _remembered("commit:sha", lambda: _git("rev-parse", "--short", "HEAD"))
    branch = _remembered(
        "commit:branch", lambda: _git("rev-parse", "--abbrev-ref", "HEAD")
    )
    status = _git("status", "--porcelain")
    return {
        "sha": sha,
        "branch": branch,
        "dirty": None if status is None else bool(status),
    }


def _loadavg() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def request_performance_core() -> str | None:
    """Ask the scheduler for a performance core. Returns the QoS class set.

    A request, not a guarantee, and a no-op off Darwin. ADR-0005 wants this
    done and reported rather than relied on.
    """
    return "user-interactive" if _set_qos(_QOS_USER_INTERACTIVE) else None


def _set_qos(qos: int) -> bool:
    if _LIBC is None or not hasattr(_LIBC, "pthread_set_qos_class_self_np"):
        return False
    try:
        _LIBC.pthread_set_qos_class_self_np.argtypes = [ctypes.c_uint, ctypes.c_int]
        _LIBC.pthread_set_qos_class_self_np.restype = ctypes.c_int
        return _LIBC.pthread_set_qos_class_self_np(qos, 0) == 0
    except (AttributeError, OSError, ValueError):
        return False


def _get_qos() -> int | None:
    """This thread's current QoS class, so a probe can put it back.

    Without this the efficiency-core probe would leave whatever process called
    it pinned to `USER_INTERACTIVE` -- fine for the benchmark, which asked for
    exactly that, and a surprise for a test suite that merely imported the
    module.
    """
    if _LIBC is None or not hasattr(_LIBC, "pthread_get_qos_class_np"):
        return None
    try:
        _LIBC.pthread_self.restype = ctypes.c_void_p
        _LIBC.pthread_get_qos_class_np.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_int),
        ]
        _LIBC.pthread_get_qos_class_np.restype = ctypes.c_int
        qos = ctypes.c_uint(0)
        relative = ctypes.c_int(0)
        rc = _LIBC.pthread_get_qos_class_np(
            _LIBC.pthread_self(), ctypes.byref(qos), ctypes.byref(relative)
        )
    except (AttributeError, OSError, ValueError):
        return None
    return qos.value if rc == 0 else None


def _rusage() -> int | None:
    """Cycles elapsed against this process so far, or None."""
    if _LIBC is None or not hasattr(_LIBC, "proc_pid_rusage"):
        return None
    try:
        _LIBC.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        _LIBC.proc_pid_rusage.restype = ctypes.c_int
        buffer = _RUsageInfoV4()
        rc = _LIBC.proc_pid_rusage(os.getpid(), _RUSAGE_INFO_V4, ctypes.byref(buffer))
    except (AttributeError, OSError, ValueError):
        return None
    if rc != 0:
        return None
    return int(buffer.ri_fields[_RI_CYCLES])


class CoreClock:
    """A cycles-per-CPU-second sample around a region of work.

    Used as a context manager::

        with CoreClock() as clock:
            ...work...
        clock.ghz  # None if the counters were unavailable or implausible
    """

    __slots__ = ("_start", "_cpu0", "cycles", "cpu_seconds")

    def __init__(self) -> None:
        self._start: int | None = None
        self._cpu0 = 0.0
        self.cycles: int | None = None
        self.cpu_seconds: float | None = None

    def __enter__(self) -> CoreClock:
        self._start = _rusage()
        self._cpu0 = time.process_time()
        return self

    def __exit__(self, *exc: object) -> None:
        cpu = time.process_time() - self._cpu0
        end = _rusage()
        if self._start is None or end is None:
            return
        self.cycles = end - self._start
        self.cpu_seconds = cpu

    @property
    def ghz(self) -> float | None:
        """Effective clock over the region, or None if it is not believable.

        The plausibility gate is what keeps a wrong struct offset from being
        reported as a CPU frequency: anything outside 0.2-10 GHz is a decoding
        error rather than a processor, and so is a sample too short to mean
        anything.
        """
        if not self.cycles or not self.cpu_seconds or self.cpu_seconds < 0.005:
            return None
        value = self.cycles / self.cpu_seconds / 1e9
        return value if 0.2 <= value <= 10.0 else None


def _reference_efficiency_ghz() -> float | None:
    """This machine's efficiency-cluster clock, measured now.

    A short spin under `BACKGROUND` QoS, which Darwin confines to the E
    cluster. Measured rather than tabulated so the classification is a fact
    about the machine in front of us and not about the chip we assumed.

    The thread's QoS is put back exactly as it was found, so a caller that
    asked for a performance core still has one afterwards and a caller that
    asked for nothing is left alone.
    """
    previous = _get_qos()
    if not _set_qos(_QOS_BACKGROUND):
        return None
    try:
        with CoreClock() as clock:
            total = 0
            for i in range(120_000):
                total += i * 3
        return clock.ghz
    finally:
        _set_qos(previous if previous else _QOS_DEFAULT)


def measure_core_class(run_ghz: float | None) -> tuple[str | None, float | None]:
    """Classify `run_ghz` as "performance" / "efficiency", with the E reference.

    Returns `(class, efficiency_reference_ghz)`, either of which may be None
    when the machine is homogeneous or the counters are unavailable.

    The bands are deliberately wide and leave a gap: within 25% of the
    E-cluster clock is efficiency, above 1.6x it is performance, and between
    them is `None`. A measurement that falls in the gap is a throttled P-core
    or a boosted E-core, and "unknown" is the true answer -- reporting a guess
    would defeat the purpose of recording the field at all.
    """
    counts = _core_counts()
    if not counts.get("efficiency"):
        return None, None
    if run_ghz is None:
        return None, None
    reference = _reference_efficiency_ghz()
    if reference is None:
        return None, None
    if run_ghz <= reference * 1.25:
        return "efficiency", reference
    if run_ghz >= reference * 1.6:
        return "performance", reference
    return None, reference


def interpreter() -> dict[str, Any]:
    """Which Python this is, in enough detail to tell two builds apart.

    Not decoration. The benchmark's judged quantity -- orders processed per
    calibration pass -- is a ratio of two Python programs, and the ratio is not
    the same on two interpreters: the same deliberate engine defect has been
    seen to earn opposite verdicts on two supported CPython builds, because a
    build's own tuning moves the engine's cost and the calibration's cost by
    different amounts. Normalisation corrects for a slower *machine*; nothing
    here corrects for a different *interpreter*.

    So the interpreter is recorded with every baseline and `baselines.compare`
    refuses across a mismatch (`baselines.interpreter_label` renders it), which
    is the honest thing rather than the complete one: making the quantity
    portable across builds is a larger piece of work than making the harness
    admit it is not.

    `build` is `platform.python_build()`, which distinguishes a python.org
    3.11.11 from a Homebrew one built on another day -- exactly the pair whose
    difference is invisible in the version number.
    """
    build = platform.python_build()
    return {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "build": " ".join(part for part in build if part),
        # Reported, never compared: a compiler string is informative about a
        # surprising number and too volatile to gate on.
        "compiler": platform.python_compiler() or None,
    }


def capture(machine_label: str | None = None) -> dict[str, Any]:
    """Everything about the machine that a surprising number might need.

    `machine_label` overrides the hostname, for anyone who would rather not
    commit theirs to a baselines file.
    """
    counts = _core_counts()
    return {
        "machine": machine_label or platform.node() or "unknown",
        "platform": platform.platform(),
        "arch": platform.machine(),
        "cpu": _cpu_brand(),
        "cores": counts,
        "python": "%s %s"
        % (platform.python_implementation(), platform.python_version()),
        #: The structured form, which `baselines.compare` refuses to cross.
        #: `python` above stays as the human-readable line in the report.
        "interpreter": interpreter(),
        "power_source": _power_source(),
        "loadavg": _loadavg(),
        "commit": _commit(),
    }
