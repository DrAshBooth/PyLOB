"""The acceptance suites' engine-neutral vocabulary, importable from `tests/`.

`tests/acceptance/conftest.py` defines the values every engine adapter speaks
-- `OrderState`, `BookEntry`, `Trade`, `OrderRef`, the `UNSET` sentinel, the
money tolerance -- and the builders that put an engine behind that surface.
It is a *conftest* for the directory below this one, so pytest never makes it
importable from here.

This module loads it by path, exactly once, under a module name of its own,
and re-exports what a test outside `tests/acceptance/` needs. Two details are
load-bearing:

* **By path, not by import.** `tests/acceptance/` is not a package, so there
  is no `acceptance.conftest` to import.
* **Under a name of its own.** pytest holds the acceptance conftest as
  `conftest` (or `acceptance.conftest`); a second copy under the same name
  would collide in `sys.modules`, and two copies of `BookEntry` would then be
  two different classes.

One loader in one place also means one copy of those classes: a `BookEntry`
built by the reference adapter is the same class as one built by the engine
adapter, and `is`-comparisons and `isinstance` checks hold across them.
"""

import importlib.util
from pathlib import Path

_ACCEPTANCE = Path(__file__).resolve().parent / "acceptance" / "conftest.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "pylob_acceptance_surface", _ACCEPTANCE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The acceptance conftest as a module. Anything not re-exported below is
#: still reachable through it.
SURFACE = _load()

# -- the values an adapter returns -----------------------------------------
OrderState = SURFACE.OrderState
BookEntry = SURFACE.BookEntry
Trade = SURFACE.Trade
OrderRef = SURFACE.OrderRef
UNSET = SURFACE.UNSET

# -- how a book is configured ----------------------------------------------
Commissions = SURFACE.Commissions
NO_COMMISSION = SURFACE.NO_COMMISSION
INSTRUMENT = SURFACE.INSTRUMENT
CURRENCY = SURFACE.CURRENCY
DEFAULT_TICK = SURFACE.DEFAULT_TICK

# -- how money is compared -------------------------------------------------
MONEY_REL = SURFACE.MONEY_REL
MONEY_ABS = SURFACE.MONEY_ABS

# -- the engines behind the surface ----------------------------------------
build_inmemory = SURFACE.build_inmemory

# -- helpers a builder needs -----------------------------------------------
as_commissions = SURFACE._as_commissions
self_matching_set = SURFACE._self_matching_set
