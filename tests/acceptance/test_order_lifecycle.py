"""Acceptance tests for the order-lifecycle contract.

Contract: `openspec/changes/spec-book-queries/specs/order-lifecycle/spec.md`.
Every test here names the requirement and scenario it encodes, and asserts the
*target* behaviour -- what an engine has to do, not what any engine does today.
Known legacy divergences are recorded with `@pytest.mark.engine_xfail` (see
`conftest.py`), never with a branch on the engine.

Fixtures come from `tests/acceptance/conftest.py`: `engine` is an empty book on
the engine under test, and the suite is run once per registered engine.
"""

import pytest


def test_trades_price_at_the_maker(engine):
    """Market orders are immediate-or-cancel / Trades price at the maker."""
    engine.limit("ask", 3, 101, tid=100)
    engine.limit("ask", 2, 102, tid=101)

    taker = engine.market("bid", 5, tid=102)

    assert [(trade.price, trade.qty) for trade in taker.trades] == [(101, 3), (102, 2)]


@pytest.mark.engine_xfail(
    "legacy",
    "lob-0bl: the legacy engine rests a market order's remainder instead of "
    "cancelling it",
)
def test_market_remainder_is_cancelled(engine):
    """Market orders are immediate-or-cancel / Remainder is cancelled when liquidity runs out."""
    engine.limit("ask", 3, 101, tid=100)
    engine.limit("ask", 2, 102, tid=101)

    taker = engine.market("bid", 8, tid=102)

    assert taker.fulfilled == 5
    assert taker.cancelled
    assert not taker.resting
    assert engine.snapshot("bid") == ()
