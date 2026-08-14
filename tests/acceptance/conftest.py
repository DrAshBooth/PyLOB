"""Fixtures shared by the acceptance suites.

The engine-neutral adapter surface these fixtures hand out -- the vocabulary,
the values, the builders -- is a library and lives in `tests/harness/`, whose
package docstring documents it. This file is only the pytest wiring: a
conftest applies to its own directory and below, and nothing outside
`tests/acceptance/` can import it, so nothing but fixtures belongs in it.

Each book records to its own sqlite *file* under `tmp_path`, because
`reopen()` needs somewhere to reload from -- see `harness.inmemory`.
"""

from itertools import count

import pytest
from harness import MONEY_ABS, MONEY_REL, build_inmemory


@pytest.fixture
def engine_factory(tmp_path):
    """Factory building independent engines, each recording to its own file.

    Call it as `engine_factory(commissions=dict(min=2.5, max_pct=1,
    per_unit=0.01), self_matching=(1,), tick_size=0.05)`; every call returns a
    fresh, empty book. Accepted options:

    `traders`
        the trader ids to seed (default: 1-5 and 100-111)
    `commissions`
        the commission schedule every seeded trader gets, as
        `dict(min=..., max_pct=..., per_unit=...)` (default: all zero)
    `self_matching`
        the trader ids allowed to match their own resting orders -- an
        iterable of ids, or True for all (default: none)
    `tick_size`, `instrument`, `currency`
        the tick, and the default instrument every surface call means when it
        names none
    `instruments`
        further `(symbol, currency)` pairs to declare beside the default one,
        for a scenario that has to distinguish "for an instrument" from "for
        the book" (default: none, i.e. a one-instrument engine)

    All engines are closed at teardown.
    """
    built = []
    counter = count(1)

    def factory(**options):
        adapter = build_inmemory(
            tmp_path / ("acceptance%d.db" % next(counter)), **options
        )
        built.append(adapter)
        return adapter

    yield factory

    for adapter in built:
        adapter.close()


@pytest.fixture
def engine(engine_factory):
    """An empty book, with commissions at zero."""
    return engine_factory()


@pytest.fixture
def approx_money():
    """Compare a commission or balance within a floating-point tolerance.

    The commissions contract is explicit that the value is the exact result of
    a floating-point formula with no currency quantization, and that tests
    compare within a tolerance rather than for bit equality::

        assert order.commission == approx_money(2.5)
    """

    def approx(expected):
        return pytest.approx(expected, rel=MONEY_REL, abs=MONEY_ABS)

    return approx
