# Handoff: spec-commissions-balances

Both spec files are frozen. Every scenario is an executable test in
`tests/acceptance/test_commissions.py` (11 tests) and
`tests/acceptance/test_trader_balances.py` (9).

## The headline for inmemory-engine

**These 20 tests pass unchanged against the new engine, or the new engine is
wrong.** There are zero `engine_xfail` markers in either file — unlike the
lifecycle and book-query suites, the legacy engine matches these contracts
exactly. So there is no legacy divergence to reproduce and no allowance to
inherit: the bar is the bar.

**Where commission and balances are computed is your design call.** The specs
constrain observable results only. ADR-0001 keeps the behaviors and moves them
into the in-memory core; whether they live in the core, in a ledger object, or
are derived at the sink is unconstrained by these contracts. `lob-5rt.5`
("in-core ledgers: balances + cumulative-recompute commissions") is where that
gets decided.

## Two clarifications added at the maintainer gate

Both were ambiguities in the prose, both now have tests, and both were
re-measured against the running engine (commit `c2045cf`):

**The percentage cap binds ahead of the floor.** Where
`max_pct × V / 100 < min_commission`, the commission is the cap, not the floor.
`min_commission` floors the per-unit charge only, never the order's commission.
The spec's third scenario pins it — min=2.5, max_pct=1, per_unit=1.0, fill
10 @ 10 charges **1.0**, not 2.5. Measured on legacy: commission 1.0, taker USD
−101.0. This matches the IB schedule being modelled, where the
maximum-percent-of-value cap overrides the per-order minimum.

**No rounding.** Commission is the exact floating-point value of the formula
with no currency quantization step. Use `approx_money(...)` for every money
comparison. Do not add `round(x, 2)` to the new engine: it would diverge the
two engines while every test still passed, which is precisely the failure mode
the clarification exists to prevent. Confirmed on legacy at sub-cent values
(0.03535).

## Two behaviors worth implementing deliberately

**Cumulative recompute, not per-fill accrual.** Commission is a function of the
order's cumulative `(Q, V)`, recomputed as fills accrue, and the trader is
debited only the *difference*. Two fills of 3 then 2 @ 100 under
min=2.5/max_pct=1/per_unit=0.01 charge **2.5 total**, not 5.0. Verified against
a mirrored zero-commission control engine, and separately on a growing schedule
where the second fill's delta is exactly 2.0. Per-fill accrual would pass the
single-fill scenarios and fail here.

**Balances track, they do not gate.** No margin check, no sufficient-funds
check; negative balances are permitted and recorded, on both sides. Covered by
a short sale, an unfunded buy, and a follow-on order placed from an already
negative balance. `design.md` names this the most likely future bug — a
well-meaning funds check that breaks short-selling research workloads — which
is why absence is specified as a requirement rather than left as an omission.
Removing it is a spec change, not a patch.

## Worked numbers, re-measured

Trader 2 buys 5 @ 101 FAKE (USD) from trader 1, both on
min=2.5/max_pct=1/per_unit=0.01:

```
trader 1: FAKE -5.0   USD +502.5
trader 2: FAKE +5.0   USD -507.5
USD sum = -5.0
```

The USD sum is exactly the two 2.5 commissions, which independently confirms
the "four movements exactly offsetting, before commissions" reading of the
symmetry requirement.

## Self-matching

Gated per trader, not per book. A gated resting order is **skipped and remains
in the book** with `fulfilled == 0`, and matching continues past it in priority
order — it is not cancelled and not consumed. Verified as a real behavior
change on legacy: the same setup trades at 102 with a third trader when the
flag is unset, and at 101 against the trader's own ask when set.

Note the requirement is filed under `trader-balances` even though it is a
matching concern. That placement was reviewed at the gate and deliberately left
alone: it is unambiguous where it sits, and re-cutting capability boundaries at
freeze time costs more than it buys.
