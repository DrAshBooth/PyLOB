"""Regression tests for GitHub issue #8 -- two independent `fulfilled` bugs.

Issue #8 (pinned at `c0dd932`) reports two separate ways a resting order can
be matched for more than its stated size, both of them mistakes in how the
cumulative-executed quantity is written. Both were diagnosed in the 2013 SQL
engine ADR-0003 has since retired:

* **Finding 1** -- the `trade_insert` trigger applied a new trade's quantity
  with `where idNum in (new.bid_order, new.ask_order)`, but those columns held
  internal row ids. The credit therefore landed on whichever rows happened to
  carry those numbers in their *`idNum`* column -- nobody, or the wrong orders
  entirely.
* **Finding 2** -- the reprice path started from `qtyToExec = quote["qty"]`,
  and `modifyOrder` never put `fulfilled` into the dict it passed there, so a
  reprice-triggered cross could execute the order's whole raw `qty` instead of
  its true remainder `qty - fulfilled`.

Every test below asserts the issue's **expected post-fix** numbers, and each
docstring records the buggy value the issue's author measured against the
unfixed engine.

Why the scenarios outlived the engine that had the bugs
-------------------------------------------------------

The two findings are mistakes in *fill accounting*, not in SQL: any engine can
credit a fill to the wrong order, and any engine can size a cross from an
order's raw quantity instead of its remainder. So these scenarios are written
against the engine-neutral adapter the acceptance suites use --
`book.limit(...)`, `order.fulfilled`, `book.trades()` -- and not against any
engine's own API. What the issue measured is quantities, and the adapter
reports quantities.

Finding 2's mechanism survives that translation whole: a partially filled
order repriced across the book is a thing any engine can get wrong, and the
numbers the issue measured are the numbers to assert.

Finding 1's does not. `idNum` diverging from an internal row id was a schema
concept of the retired engine; this one has no second identifier to diverge
from, since `_orders` is keyed on `idNum` and a `Trade` names its two orders
by `idNum`. The scenario is kept rather than dropped, because what the
ratified contract took from Finding 1 is not the trigger's `WHERE` clause but
the invariant it broke -- `order-matching`, "Fills are credited to the orders
that traded", explicitly "regardless of any difference between an order's
public identifier (`idNum`) and its internal row identity". So the setup is
kept verbatim, decoy identifiers and all, and the question asked of it is the
neutral one: *did this fill land on the two orders that traded, and on nobody
else?* Here that arrangement is one the engine has no way to be fooled by, and
the assertion is the contract's rather than the trigger's -- a scenario that
passes by construction, which is the honest translation and not a weakened
one.

Both repros use one trader crossing itself, exactly as the issue does: it is
the smallest way to exercise both sides of a match.
"""

import pytest

# The engine-neutral adapter surface lives in the acceptance conftest. A
# conftest applies to its own directory and below, so this file cannot inherit
# its fixtures and imports the builder instead -- `tests/` is on `sys.path`
# while this module is imported, which makes `acceptance` an importable
# namespace package.
from acceptance.conftest import build_inmemory

TID = 1


@pytest.fixture
def book(tmp_path):
    """An empty book with trader 1 allowed to match itself.

    The fixture the issue's repros needed -- one trader crossing itself is the
    smallest way to exercise both sides of a match.
    """
    adapter = build_inmemory(
        tmp_path / "issue8.db", traders=(TID,), self_matching=(TID,)
    )
    yield adapter
    adapter.close()


def place(book, side, qty, price, idNum=None, timestamp=None):
    """Submit a limit order for `TID` and return its `OrderRef`.

    Passing `idNum` selects the replay path -- the code path an engine offers
    for a caller-supplied `idNum`/`timestamp`, and the path that made `idNum`
    diverge from the row id `order_id` in Finding 1. Omitting it uses the
    default path, where the unfixed engine's private counter happened to track
    `order_id` 1:1.
    """
    return book.limit(side, qty, price, tid=TID, idNum=idNum, timestamp=timestamp)


def state(order):
    """`(qty, fulfilled)` as the engine holds them right now."""
    return (order.qty, order.fulfilled)


def traded_against(book, order, side):
    """Total quantity of every execution naming `order` on `side`."""
    return sum(
        trade.qty
        for trade in book.trades()
        if (trade.bid if side == "bid" else trade.ask) == order.idNum
    )


def filled(trades):
    """Quantity summed over a list of executions."""
    return sum(trade.qty for trade in trades)


# --------------------------------------------------------------------------
# Finding 1 -- `trade_insert` keys its UPDATE on `idNum` instead of `order_id`
# --------------------------------------------------------------------------


def test_fill_is_credited_only_to_the_participating_orders(book):
    """Finding 1: a trade must advance `fulfilled` on its own two orders only.

    Four orders are placed on the replay path with `idNum` values deliberately
    offset from the `order_id` row-id sequence the issue reports, arranged so
    that the two resting non-participants X and Y carry the `idNum` values 3
    and 4 that the crossing pair A and B carry as `order_id`:

        X  bid 10 @ 90   idNum=3    order_id=1   (never crosses)
        Y  ask 10 @ 110  idNum=4    order_id=2   (never crosses)
        A  bid 10 @ 100  idNum=500  order_id=3
        B  ask 10 @ 100  idNum=501  order_id=4   -> trades 10 with A

    In the unfixed engine the single trade row is `(bid_order=3, ask_order=4)`,
    so the trigger's `where idNum in (3, 4)` credits X and Y -- two orders that
    never traded. Measured against unfixed code: X and Y both report
    `fulfilled=10` while the actual participants A and B both report
    `fulfilled=0`.

    An engine that keys orders on `idNum` alone has no second sequence for
    these decoys to collide with, so the arrangement is inert here and the
    assertion is `order-matching`'s plain requirement that a fill reach the
    two orders that traded and no others.
    """
    x = place(book, "bid", 10, 90, idNum=3, timestamp=1)
    y = place(book, "ask", 10, 110, idNum=4, timestamp=2)
    a = place(book, "bid", 10, 100, idNum=500, timestamp=3)
    b = place(book, "ask", 10, 100, idNum=501, timestamp=4)

    # The cross itself is not in question -- A and B do trade their 10.
    assert filled(b.trades) == 10

    # Compared as one mapping so a failure shows every misdirected credit at
    # once, not just the first one.
    states = {
        "A traded 10": state(a),
        "B traded 10": state(b),
        "X never traded": state(x),
        "Y never traded": state(y),
    }
    assert states == {
        "A traded 10": (10, 10),
        "B traded 10": (10, 10),
        "X never traded": (10, 0),
        "Y never traded": (10, 0),
    }


def test_resting_order_never_trades_more_than_its_size(book):
    """Finding 1 (issue repro 1): 12 shares executed against a 10-share order.

    A 10-share bid rests with a caller-supplied `idNum=500`. An ask for 10
    crosses it and legitimately takes all 10 -- but in the unfixed engine the
    trigger's `WHERE` matched no row (`idNum` 500/501 vs `order_id` 1/2), so
    `fulfilled` stayed at 0, the bid kept reporting a full 10 available, and a
    second ask for 2 traded against it again.

    Measured against unfixed code: `fulfilled=0` after the first ask, the
    second ask trades 2, and 12 total executes against the 10-share bid.
    """
    bid = place(book, "bid", 10, 100, idNum=500, timestamp=1)

    first = place(book, "ask", 10, 100, idNum=501, timestamp=2)
    assert filled(first.trades) == 10, "the bid genuinely had 10 available"

    fulfilled_after_first = bid.fulfilled
    second = place(book, "ask", 2, 100, idNum=502, timestamp=3)

    # Compared as one mapping so a failure reports the whole chain -- the
    # fill that went unrecorded, the second ask it wrongly enabled, and the
    # resulting over-execution.
    observed = dict(
        bid_fulfilled_after_first_ask=fulfilled_after_first,
        second_ask_filled=filled(second.trades),
        total_traded_against_the_10_share_bid=traded_against(book, bid, "bid"),
    )
    assert observed == dict(
        bid_fulfilled_after_first_ask=10,
        second_ask_filled=0,
        total_traded_against_the_10_share_bid=10,
    )


# --------------------------------------------------------------------------
# Finding 2 -- `modifyOrder`'s reprice cross sizes itself from raw `qty`
# --------------------------------------------------------------------------


@pytest.fixture
def reprice_scenario(book):
    """Issue repro 2, run once, with the state captured at each checkpoint.

        A  bid 10 @ 100          -- rests
           ask  4 @ 100          -- partially fills A; A.fulfilled=4, left 6
        C  ask 20 @ 101          -- rests above A, does not cross yet
        A  repriced to bid @ 105 -- now crosses C; may only take its own 6
        E  bid 12 @ 101          -- fresh, unrelated; C should fill it whole

    Placed on the default path, where `idNum` tracked `order_id` and Finding 1
    was not a factor. The reprice restates A's quantity as its unchanged 10,
    which is the raw `qty` Finding 2 mistakes for a remainder.
    """
    a = place(book, "bid", 10, 100)
    place(book, "ask", 4, 100)
    c = place(book, "ask", 20, 101)

    before = state(a)

    reprice_trades = book.modify(a, qty=10, price=105)
    a_after = state(a)
    c_after = state(c)

    e = place(book, "bid", 12, 101)

    return dict(
        book=book,
        a_before=before,
        reprice_trades=reprice_trades,
        a_after=a_after,
        c_after=c_after,
        e_trades=e.trades,
    )


def test_partial_fill_leaves_the_expected_remainder(reprice_scenario):
    """Finding 2, precondition: A is `qty=10 fulfilled=4`, 6 truly remaining.

    This is the one line of the issue's repro-2 output that is already
    correct on unfixed code (`A qty=10 fulfilled=4 true remaining=6`); it is
    asserted so that a failure further down cannot be blamed on the setup.
    """
    assert reprice_scenario["a_before"] == (10, 4)


def test_reprice_trades_only_the_unfilled_remainder(reprice_scenario):
    """Finding 2: the reprice cross may take 6, not A's raw `qty` of 10.

    `qtyToExec = quote["qty"]` ignores the 4 already filled, so the
    self-triggered cross is let loose on the counterparty for the full 10.
    Measured against unfixed code: `[(1, 3, 4, 101.0, 10)]` -- one trade of
    10. Expected: a single trade of 6.
    """
    assert filled(reprice_scenario["reprice_trades"]) == 6


def test_repriced_order_fulfilled_never_exceeds_its_qty(reprice_scenario):
    """Finding 2: A must end at `fulfilled=10`, exactly its own stated size.

    Measured against unfixed code: `A qty=10 fulfilled=14` -- an order filled
    past its own quantity, which also makes `qty - fulfilled` negative and
    drops the row out of `best_quotes`.
    """
    qty, fulfilled = reprice_scenario["a_after"]
    assert (qty, fulfilled) == (10, 10)
    assert 0 <= fulfilled <= qty


def test_counterparty_retains_the_liquidity_the_reprice_could_not_take(
    reprice_scenario,
):
    """Finding 2: C keeps 14 of its 20 after giving A its rightful 6.

    Measured against unfixed code: `C qty=20 fulfilled=10 remaining=10` --
    4 shares of C's resting liquidity consumed by a reprice that was never
    entitled to them.
    """
    qty, fulfilled = reprice_scenario["c_after"]
    assert (qty, fulfilled) == (20, 6)
    assert qty - fulfilled == 14


def test_later_unrelated_order_fills_in_full(reprice_scenario):
    """Finding 2, downstream damage: a fresh 12-lot bid must fill completely.

    E is an ordinary new bid priced to cross C and sized to exactly what C's
    honestly-accounted remaining liquidity (14) can hand it in full.
    Measured against unfixed code: `E matched 10 of 12 requested
    (UNDER-FILLED by 2)`, because A's reprice had already taken 4 shares of C
    it was not owed.
    """
    assert filled(reprice_scenario["e_trades"]) == 12
