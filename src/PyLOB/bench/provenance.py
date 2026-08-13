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
`proc_pid_rusage(RUSAGE_INFO_V4)`, which reports the process's retired
instructions and elapsed *cycles*. Cycles divided by CPU seconds is the
effective clock, and on a heterogeneous CPU the P and E clusters are far
enough apart to tell apart.

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
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import subprocess
import sys
import time
from typing import Any, Final

__all__ = [
    "CoreClock",
    "capture",
    "effective_ghz",
    "interpreter",
    "measure_core_class",
    "request_performance_core",
]

_DARWIN: Final = sys.platform == "darwin"

#: `qos_class_t` values from <sys/qos.h>.
_QOS_USER_INTERACTIVE: Final = 0x21
_QOS_DEFAULT: Final = 0x15
_QOS_BACKGROUND: Final = 0x09

#: `struct rusage_info_v4` is a 16-byte uuid followed by 35 uint64 fields.
#: `ri_instructions` and `ri_cycles` are the 30th and 31st of them.
_RUSAGE_INFO_V4: Final = 4
_RI_FIELDS: Final = 35
_RI_INSTRUCTIONS: Final = 29
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


def _cpu_brand() -> str | None:
    brand = _sysctl("machdep.cpu.brand_string")
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
    """
    total = os.cpu_count()
    levels = _sysctl("hw.nperflevels")
    if levels is None:
        return {"logical": total, "performance": None, "efficiency": None}
    try:
        count = int(levels)
    except ValueError:
        return {"logical": total, "performance": None, "efficiency": None}
    if count < 2:
        return {"logical": total, "performance": total, "efficiency": 0}

    def level(index: int) -> int | None:
        raw = _sysctl("hw.perflevel%d.logicalcpu" % index)
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
            ("git", *args), capture_output=True, text=True, timeout=10, cwd=_repo_root()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _repo_root() -> str:
    """The package's repository, so provenance is about the code being measured.

    `os.getcwd()` would report whatever directory the benchmark happened to be
    invoked from, which on a worktree-per-agent layout is routinely a different
    checkout from the one that was imported.
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def _commit() -> dict[str, Any]:
    sha = _git("rev-parse", "--short", "HEAD")
    status = _git("status", "--porcelain")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
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
    if _LIBC is None or not hasattr(_LIBC, "pthread_set_qos_class_self_np"):
        return None
    try:
        _LIBC.pthread_set_qos_class_self_np.argtypes = [ctypes.c_uint, ctypes.c_int]
        _LIBC.pthread_set_qos_class_self_np.restype = ctypes.c_int
        if _LIBC.pthread_set_qos_class_self_np(_QOS_USER_INTERACTIVE, 0) != 0:
            return None
    except (AttributeError, OSError, ValueError):
        return None
    return "user-interactive"


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


def _rusage() -> tuple[int, int] | None:
    """`(instructions, cycles)` retired by this process, or None."""
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
    return buffer.ri_fields[_RI_INSTRUCTIONS], buffer.ri_fields[_RI_CYCLES]


class CoreClock:
    """A cycles-per-CPU-second sample around a region of work.

    Used as a context manager::

        with CoreClock() as clock:
            ...work...
        clock.ghz  # None if the counters were unavailable or implausible
    """

    __slots__ = ("_start", "_cpu0", "cycles", "instructions", "cpu_seconds")

    def __init__(self) -> None:
        self._start: tuple[int, int] | None = None
        self._cpu0 = 0.0
        self.cycles: int | None = None
        self.instructions: int | None = None
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
        self.instructions = end[0] - self._start[0]
        self.cycles = end[1] - self._start[1]
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

    @property
    def ipc(self) -> float | None:
        if not self.cycles or not self.instructions:
            return None
        return self.instructions / self.cycles


def effective_ghz(work: Any) -> float | None:
    """The effective clock while running `work()`."""
    with CoreClock() as clock:
        work()
    return clock.ghz


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
