"""Workloads are seeded, deterministic, and versioned by name.

The benchmarking spec's first requirement: a workload is fully determined by
(name, seed), so the same pair produces the identical order stream on every
run and machine. That is what makes two measurements comparable, and it is
what lets the differential harness drive an engine and a reference matcher on
byte-identical input.

The second thing tested here has no scenario in the spec but is the reason the
generator carries checksums: **a name is a promise about a composition.** A
baseline says "the engine did N orders/sec on `mixed-v1`", and that sentence
is true only while `mixed-v1` means what it meant when N was recorded. Editing
the mix in place would leave every recorded baseline describing a workload
that no longer exists, and nothing would say so. So the canonical stream's
checksum is pinned, and these tests fail loudly when it moves -- the intended
response being a new name, not a new constant.
"""

import pytest
from PyLOB.bench import workloads


def test_same_seed_produces_the_identical_stream():
    """The spec's scenario: generate `mixed-v1` twice at seed 42, get one stream."""
    first = workloads.generate("mixed-v1", 42)
    second = workloads.generate("mixed-v1", 42)

    assert first == second
    assert workloads.checksum(first) == workloads.checksum(second)


def test_the_stream_is_identical_across_sizes_of_the_same_prefix_seed():
    """A different size is a different stream, not a truncation of one.

    Worth pinning because it is the tempting assumption: the kinds are drawn
    from a multiset sized to the request and then shuffled, so asking for
    2,000 orders does not give the first 2,000 of the 20,000-order run. Two
    order counts are therefore two workloads, which is why the order count is
    part of the baseline key.
    """
    small = workloads.generate("mixed-v1", 42, 2_000, verify=False)
    large = workloads.generate("mixed-v1", 42, 20_000)

    assert len(small) == 2_000
    assert small != large[:2_000]


def test_different_seeds_produce_different_streams():
    a = workloads.generate("mixed-v1", 42)
    b = workloads.generate("mixed-v1", 43, verify=False)

    assert workloads.checksum(a) != workloads.checksum(b)


def test_seeds_do_not_change_the_composition():
    """The seed varies the order and the prices, never the 70/20/10 mix.

    Drawing each kind from `rng.random()` would make the split a per-seed
    accident and quietly turn the seed into a parameter of the throughput
    number. Building a fixed multiset and shuffling it is what keeps four
    seeds measuring one workload.
    """
    for seed in (1, 42, 1234, 99999):
        ops = workloads.generate("mixed-v1", seed, verify=seed == 42)
        assert workloads.composition(ops) == {
            "passive": 14_000,
            "crossing": 4_000,
            "market": 2_000,
        }


def test_mixed_v1_is_seventy_twenty_ten():
    """The shape the trial measurement used, read off the stream itself."""
    ops = workloads.generate("mixed-v1", 42)
    counts = workloads.composition(ops)
    total = len(ops)

    assert counts["passive"] / total == pytest.approx(0.70)
    assert counts["crossing"] / total == pytest.approx(0.20)
    assert counts["market"] / total == pytest.approx(0.10)
    assert sum(counts.values()) == total


def test_market_orders_carry_no_price_and_limits_do():
    ops = workloads.generate("mixed-v1", 42)

    for op in ops:
        if op.order_type == "market":
            assert op.price is None
        else:
            assert op.price is not None and op.price > 0


def test_orders_are_within_the_workloads_declared_shape():
    """Traders, sides and the price band are what `mixed-v1` says they are."""
    spec = workloads.WORKLOADS["mixed-v1"]
    ops = workloads.generate("mixed-v1", 42)

    assert {op.side for op in ops} == {"bid", "ask"}
    assert {op.tid for op in ops} <= set(range(1, spec.traders + 1))
    assert all(op.qty >= 1 for op in ops)
    prices = [op.price for op in ops if op.price is not None]
    # The mid walks inside [97.50, 102.50] and passive orders sit up to 120
    # ticks off it, so nothing may fall outside that band widened by the depth.
    assert min(prices) >= 97.50 - 1.20
    assert max(prices) <= 102.50 + 1.20


def test_the_canonical_stream_checksum_is_pinned():
    """Changing the composition must require changing the name.

    If this fails, `mixed-v1` no longer generates what its recorded baselines
    were measured against. The fix is `mixed-v2`, not a new number here.
    """
    spec = workloads.WORKLOADS["mixed-v1"]
    ops = workloads.generate("mixed-v1", workloads.CANONICAL_SEED, verify=False)

    assert workloads.checksum(ops) == spec.checksum


def test_generate_refuses_a_stream_that_does_not_match_its_name(monkeypatch):
    """The pin is enforced at run time, not only by this test file."""
    spec = workloads.WORKLOADS["mixed-v1"]
    monkeypatch.setitem(
        workloads.WORKLOADS, "mixed-v1", spec._replace(checksum=spec.checksum + 1)
    )

    with pytest.raises(ValueError, match="composition has changed"):
        workloads.generate("mixed-v1", workloads.CANONICAL_SEED)


def test_the_checksum_notices_a_reordered_stream():
    """A fingerprint that ignored order would not pin a workload at all."""
    ops = workloads.generate("mixed-v1", 42)
    swapped = list(ops)
    swapped[0], swapped[1] = swapped[1], swapped[0]

    assert workloads.checksum(swapped) != workloads.checksum(ops)


def test_unknown_workload_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="mixed-v1"):
        workloads.generate("no-such-workload", 42)


def test_a_non_positive_order_count_is_refused():
    with pytest.raises(ValueError, match="positive"):
        workloads.generate("mixed-v1", 42, 0)
