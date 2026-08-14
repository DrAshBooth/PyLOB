"""The acceptance suites' engine-neutral adapter surface, as a library.

The acceptance suites encode the *frozen contracts* under `openspec/specs/` --
target behaviour, not what the engine happens to do today. What is
engine-specific lives here rather than in the suites.

The seam is **the adapter**: one engine-neutral vocabulary for submitting
orders and observing the result.

operations
    `limit`, `market`, `cancel`, `modify`, `reopen`, `close`
observation
    `order_state`, `snapshot`, `trades`, `best`, `worst`, `volume_at`,
    `last_price`, `balance`

The suites never touch `processOrder`'s dict quotes or the engine's own
objects; they call `engine.limit(...)`, read `order.fulfilled`, ask for
`engine.snapshot("bid")`. That is what lets one test body describe a
*contract* rather than an implementation, and it is not hypothetical: the
spec-derived reference matcher in `tests/reference` speaks exactly this
surface, which is what makes the differential harness expressible.

Two modules:

`surface.py`
    the vocabulary itself -- the values an adapter returns, the `UNSET`
    sentinel, the configuration types, the money tolerance. Imports no
    engine.
`inmemory.py`
    `PyLOB.engine.OrderBook` behind that vocabulary, and `build_inmemory`.

Why this is a package and not the acceptance conftest
-----------------------------------------------------

It used to be the body of `tests/acceptance/conftest.py`. A conftest is
importable from the directory it governs and nowhere else, so the three
consumers outside `tests/acceptance/` reached it two different ways at
once -- `tests/test_issue8_regressions.py` through the `acceptance`
namespace package, `tests/test_differential.py` through a by-path loader --
and pytest's own copy made a third. Three module objects meant three
`BookEntry` classes, and `isinstance` across them was false.

Adapters and a builder are a library, not fixtures, so they live somewhere
importable and there is exactly one of each class again. `tests/acceptance/
conftest.py` keeps only the fixtures, and imports them from here like every
other consumer.

Each book records to its own sqlite *file* under `tmp_path`, because
`reopen()` needs somewhere to reload from -- see `InMemoryAdapter`.
"""

from .inmemory import InMemoryAdapter, build_inmemory
from .surface import (
    CURRENCY,
    DEFAULT_TICK,
    INSTRUMENT,
    MONEY_ABS,
    MONEY_REL,
    NO_COMMISSION,
    TRADERS,
    UNSET,
    BookEntry,
    Commissions,
    OrderRef,
    OrderState,
    Trade,
    as_commissions,
    self_matching_set,
)

__all__ = [
    "CURRENCY",
    "DEFAULT_TICK",
    "INSTRUMENT",
    "MONEY_ABS",
    "MONEY_REL",
    "NO_COMMISSION",
    "TRADERS",
    "UNSET",
    "BookEntry",
    "Commissions",
    "InMemoryAdapter",
    "OrderRef",
    "OrderState",
    "Trade",
    "as_commissions",
    "build_inmemory",
    "self_matching_set",
]
