"""Regression tests for GitHub issue #8 -- two independent `fulfilled` bugs.

Issue #8 (pinned at `c0dd932`) reports two separate ways a resting order can
be matched for more than its stated size, both of them mistakes in how the
cumulative-executed column `trade_order.fulfilled` is written:

* **Finding 1** -- the `trade_insert` trigger (`src/create_lob.sql`) applies a
  new trade's quantity with `where idNum in (new.bid_order, new.ask_order)`,
  but `trade.bid_order`/`trade.ask_order` hold `trade_order.order_id` values.
  The credit therefore lands on whichever rows happen to carry those numbers
  in their *`idNum`* column -- nobody, or the wrong orders entirely.
* **Finding 2** -- `processMatchesDB` (`src/PyLOB/orderbook.py`) starts from
  `qtyToExec = quote["qty"]`, and `modifyOrder` never puts `fulfilled` into
  the dict it passes there, so a reprice-triggered cross may execute the
  order's whole raw `qty` instead of its true remainder `qty - fulfilled`.

Every test below asserts the issue's **expected post-fix** numbers, and each
docstring records the buggy value the issue's author measured against the
unfixed engine. These tests are red until the trigger fix and the `qtyToExec`
fix land.

Both repros use one trader crossing itself (`self_matching_lob`), exactly as
the issue does: `matches.sql`'s `(allow_self_matching=1 or trader<>:tid)`
clause requires it, and it keeps the scenarios minimal.
"""

import pytest

INSTRUMENT = "FAKE"
TID = 1


def place(book, side, qty, price, idNum=None, timestamp=None):
    """Submit a limit order and return `(trades, quote)`.

    Passing `idNum` selects `processOrder`'s `fromData=True` path -- the code
    path the engine offers for a caller-supplied `idNum`/`timestamp`, which is
    what makes `idNum` diverge from `order_id` in Finding 1. Omitting it uses
    the default path, where the private `nextQuoteID` counter happens to track
    `order_id` 1:1.
    """
    quote = dict(
        type="limit",
        side=side,
        instrument=INSTRUMENT,
        qty=qty,
        price=price,
        tid=TID,
    )
    if idNum is None:
        return book.processOrder(quote, False, False)
    quote.update(idNum=idNum, timestamp=timestamp)
    return book.processOrder(quote, True, False)


def order_state(book, order_id):
    """Return `(qty, fulfilled)` straight out of `trade_order`."""
    return book.db.execute(
        "select qty, fulfilled from trade_order where order_id=?", (order_id,)
    ).fetchone()


def traded_against(book, order_id, side):
    """Total quantity of every trade naming `order_id` on `side`."""
    column = "bid_order" if side == "bid" else "ask_order"
    (total,) = book.db.execute(
        "select coalesce(sum(qty), 0) from trade where %s=?" % column, (order_id,)
    ).fetchone()
    return total


def filled(trades):
    """Quantity summed over a `processOrder`/`modifyOrder` trade list."""
    return sum(trade[4] for trade in trades)


# --------------------------------------------------------------------------
# Finding 1 -- `trade_insert` keys its UPDATE on `idNum` instead of `order_id`
# --------------------------------------------------------------------------


def test_fill_is_credited_only_to_the_participating_orders(self_matching_lob):
    """Finding 1: a trade must advance `fulfilled` on its own two orders only.

    Four orders are placed on the `fromData=True` path with `idNum` values
    deliberately offset from `order_id`, arranged so that the two resting
    non-participants X and Y carry the `idNum` values 3 and 4 that the
    crossing pair A and B carry as `order_id`:

        X  bid 10 @ 90   idNum=3    order_id=1   (never crosses)
        Y  ask 10 @ 110  idNum=4    order_id=2   (never crosses)
        A  bid 10 @ 100  idNum=500  order_id=3
        B  ask 10 @ 100  idNum=501  order_id=4   -> trades 10 with A

    The single trade row is `(bid_order=3, ask_order=4)`, so the trigger's
    `where idNum in (3, 4)` credits X and Y -- two orders that never traded.
    Measured against unfixed code: X and Y both report `fulfilled=10` while
    the actual participants A and B both report `fulfilled=0`.
    """
    book = self_matching_lob
    _, x = place(book, "bid", 10, 90, idNum=3, timestamp=1)
    _, y = place(book, "ask", 10, 110, idNum=4, timestamp=2)
    _, a = place(book, "bid", 10, 100, idNum=500, timestamp=3)
    trades, b = place(book, "ask", 10, 100, idNum=501, timestamp=4)

    # The cross itself is not in question -- A and B do trade their 10.
    assert filled(trades) == 10

    # Compared as one mapping so a failure shows every misdirected credit at
    # once, not just the first one.
    states = {
        "A traded 10": order_state(book, a["order_id"]),
        "B traded 10": order_state(book, b["order_id"]),
        "X never traded": order_state(book, x["order_id"]),
        "Y never traded": order_state(book, y["order_id"]),
    }
    assert states == {
        "A traded 10": (10, 10),
        "B traded 10": (10, 10),
        "X never traded": (10, 0),
        "Y never traded": (10, 0),
    }


def test_resting_order_never_trades_more_than_its_size(self_matching_lob):
    """Finding 1 (issue repro 1): 12 shares executed against a 10-share order.

    A 10-share bid rests with a caller-supplied `idNum=500`. An ask for 10
    crosses it and legitimately takes all 10 -- but because the trigger's
    `WHERE` matched no row (`idNum` 500/501 vs `order_id` 1/2), `fulfilled`
    stays at 0, the bid keeps reporting a full 10 available in `best_quotes`,
    and a second ask for 2 trades against it again.

    Measured against unfixed code: `fulfilled=0` after the first ask, the
    second ask trades 2, and 12 total executes against the 10-share bid.
    """
    book = self_matching_lob
    _, bid = place(book, "bid", 10, 100, idNum=500, timestamp=1)

    first, _ = place(book, "ask", 10, 100, idNum=501, timestamp=2)
    assert filled(first) == 10, "the bid genuinely had 10 available"

    fulfilled_after_first = order_state(book, bid["order_id"])[1]
    second, _ = place(book, "ask", 2, 100, idNum=502, timestamp=3)

    # Compared as one mapping so a failure reports the whole chain -- the
    # fill that went unrecorded, the second ask it wrongly enabled, and the
    # resulting over-execution.
    observed = dict(
        bid_fulfilled_after_first_ask=fulfilled_after_first,
        second_ask_filled=filled(second),
        total_traded_against_the_10_share_bid=traded_against(
            book, bid["order_id"], "bid"
        ),
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
def reprice_scenario(self_matching_lob):
    """Issue repro 2, run once, with the state captured at each checkpoint.

        A  bid 10 @ 100          -- rests
           ask  4 @ 100          -- partially fills A; A.fulfilled=4, left 6
        C  ask 20 @ 101          -- rests above A, does not cross yet
        A  repriced to bid @ 105 -- now crosses C; may only take its own 6
        E  bid 12 @ 101          -- fresh, unrelated; C should fill it whole

    Placed on the default `fromData=False` path, so `idNum` tracks `order_id`
    and Finding 1 is not a factor here.
    """
    book = self_matching_lob
    _, a = place(book, "bid", 10, 100)
    place(book, "ask", 4, 100)
    _, c = place(book, "ask", 20, 101)

    before = order_state(book, a["order_id"])

    reprice_trades, _ = book.modifyOrder(
        a["idNum"], dict(side="bid", qty=10, price=105, tid=TID)
    )
    a_after = order_state(book, a["order_id"])
    c_after = order_state(book, c["order_id"])

    e_trades, _ = place(book, "bid", 12, 101)

    return dict(
        book=book,
        a_before=before,
        reprice_trades=reprice_trades,
        a_after=a_after,
        c_after=c_after,
        e_trades=e_trades,
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
