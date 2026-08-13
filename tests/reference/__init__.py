"""The differential harness's oracle: a matching engine derived from the specs.

ADR-0003 retires the legacy SQL engine and replaces it as the cross-check
oracle rather than merely deleting it. This package is the replacement.

Two modules, and the split is the point:

`matcher.py`
    the model itself. Written from `openspec/specs/*/spec.md`, citing the
    clause behind every rule, and importing *nothing* from `PyLOB` -- which
    `tests/test_differential.py` enforces by reading its imports. A flat list
    of resting orders, re-sorted on every match; no level, no heap, no cached
    volume, none of the bookkeeping the engine is being checked on.

`adapter.py`
    the model behind the acceptance suites' engine-neutral surface, so that
    `tests/test_differential.py` can drive it and `PyLOB.engine` through one
    code path. Translation only, no rules.

Keeping them apart makes the independence claim checkable instead of merely
intended: `matcher.py` may not import the engine, `adapter.py` must speak a
vocabulary shared with it, and one file cannot honour both rules.
"""

from .adapter import ReferenceAdapter, build_reference
from .matcher import (
    ASK,
    BID,
    LIMIT,
    MARKET,
    ReferenceBook,
    ReferenceError,
    ReferenceInvalid,
    ReferenceUnknown,
    commission_for,
    quantize,
)

__all__ = [
    "ASK",
    "BID",
    "LIMIT",
    "MARKET",
    "ReferenceAdapter",
    "ReferenceBook",
    "ReferenceError",
    "ReferenceInvalid",
    "ReferenceUnknown",
    "build_reference",
    "commission_for",
    "quantize",
]
