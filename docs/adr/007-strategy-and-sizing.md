# Design decision doc 007 - The trading rule, its sizing, and what a trade costs

**Status:** Proposed (Phase 4, 2026-09-01) -- awaiting ratification

## Context

Design decision doc 006 settled *when* the strategy may learn. This settles *what it does*: the
rule, how big each position is, and what the trade costs. The costs turn out to
matter more than the rule.

## Decision

### 1. The rule: trade a bucket only if it is both statistically and economically real

For each out-of-sample contract, find its price bucket and consult the rule
learned in-sample. A bucket is traded when **both** hold:

- **statistically real** -- significant under the same clustered, BH-corrected
  machinery Phase 3 reports;
- **economically real** -- its gross edge exceeds the Kalshi fee at that price
  by `min_net_edge`.

The side is the sign of the in-sample bias: negative means the market priced it
too high, so the profitable side is NO.

Both filters run on training data, so the thresholds are *derived* rather than
chosen (design decision doc 006, decision 4).

### 2. Fees: Kalshi's taker schedule, verified, and paid once

```
fee = ceil(0.07 * C * P * (1 - P))        no settlement or exercise fee
```

Verified against Kalshi's CFTC rule filing and two independent 2026 sources.
Positions are held to settlement, so the fee is paid **once on entry** -- there
is no exit trade and settlement is free.

**Taker, not maker.** The maker rate is a quarter of this, and using it would
be assuming the hard part away: a resting order's fill depends on the order
book, the historical data has no usable book (54.9% of settled snapshots quote
0/1, design decision doc 003), and an unfilled maker order earns nothing. Assuming taker is the
assumption we can defend.

**Rounding is per order.** The filed formula puts `C` inside the ceiling, so
the round-up is worth under a cent per contract at realistic size, and the
default charges the exact rate. An earlier draft defaulted to a *per-contract*
ceiling as "conservative"; that overcharges by 4.7x at P=0.03 (0.21c true rate,
1c charged) and wrongly rules the longshot bucket untradeable. Being
conservative in the wrong place is not caution, it is a different error. The
per-contract reading is retained behind a flag as a pessimistic bound.

**The shape is what matters.** The fee is a parabola peaking at P=0.50 --
exactly the worst place to have an edge -- and falling toward zero at both
extremes, where longshots and heavy favorites live. It removes the 0.50-0.70
buckets outright, which happen to be the two the study could not call
significant anyway.

### 3. Half-Kelly, capped per position, and capped per day

**Kelly**, for a binary contract, reduces to two clean forms:

```
buy YES at p believing q:   f* = (q - p) / (1 - p)
buy NO  at p believing q:   f* = (p - q) / p
```

Both are 0 at q = p and 1 at certainty.

**Half, not full.** Full Kelly maximises long-run growth only when the edge is
*known*. Ours is estimated from 60,114 contracts and carries real error, and
overbetting an overestimated edge loses superlinearly -- betting 2x Kelly has
zero expected growth, and beyond that it is ruinous. Half-Kelly keeps about
three quarters of the growth for roughly half the volatility.

**Per-position cap** (`max_position_fraction = 0.02`) guards the geometry: at
p = 0.98 the YES denominator is 0.02, so a small estimated edge produces an
enormous Kelly number and levers up the estimate's own error.

**Per-day budget** (`max_daily_deployment = 1.0`) is not a refinement -- without
it the backtest is incoherent. Kelly sizes every bet as though it were the only
one on the table, and this strategy holds roughly 570 positions on a typical
settlement day, so the per-position cap alone asks for about **11x the
bankroll** daily. Days over budget are scaled proportionally, preserving the
relative weights the edge chose.

The consequence, stated plainly: **the budget binds on 99.9% of trades**, so
sizing is effectively proportional-within-budget rather than Kelly. Kelly
determines the relative weights; the portfolio constraint determines the level.
Calling the result "Kelly-sized" without that sentence would be overselling it.

### 4. Returns are measured on capital deployed, not capital staked

A losing trade forfeits the stake *and* pays the fee, so `pnl / stake` reaches
-1.06. Compounding a series containing a value below -1 drives the equity curve
through zero into negative territory, after which the drawdown is not merely
wrong but unreadable -- this produced a NaN before it was caught. The
denominator is `stake + fee + slippage`: the cash that actually leaves the
account.

### 5. Slippage is not modelled, and that is the headline

The backtest fills at the last traded price. A real taker crosses the spread
and fills worse by roughly half the bid-ask. **This cannot be measured from
this data** -- settled snapshots carry no usable book -- so rather than invent
a number, the strategy reports the **breakeven slippage**: the adverse fill per
contract that would erase the entire edge.

It is **0.98c**. Under one cent.

That number, not the ROI, is the honest summary of tradeability, and it is why
this design decision doc does not claim the strategy works.

## Consequences

Out-of-sample, 27,404 trades over 48 settlement days:

| | strategy | anti-bias control |
|---|---|---|
| ROI on deployed capital | **+1.23%** | -14.59% |
| final equity (start 1.0) | 1.74 | 0.0003 |
| hit rate | 82.9% | 17.1% |
| max drawdown | -11.4% | -99.97% |
| fees paid | 0.62 | 2.32 |
| **breakeven slippage** | **0.98c** | -- |

**The falsification control works.** Inverting the belief loses 14.6% where the
strategy makes 1.2%, on the same contracts and the same fees. A finding that
cannot be made to fail is not evidence; this one can and doesn't.

Note the control had to invert the **belief**, not merely the side. Flipping
only the side leaves every position facing a negative Kelly fraction, so it
declines every trade and "does not lose money" -- passing vacuously, which is
the one outcome that would prove nothing. A test now asserts the control takes
positions.

**Fees consume a third of the gross edge** (1.20 gross to 0.58 net) and the
remaining margin is under a cent per contract. Anyone reading the +1.23% ROI
without the 0.98c beside it has been misled.

## Alternatives considered

- **Full Kelly.** Rejected: the edge is estimated, not known.
- **Flat sizing.** Rejected as the stated rule, though the daily budget means
  the realised sizing is close to it. Kelly still sets relative weights, and
  keeping it makes the sizing rule principled rather than arbitrary.
- **Maker fees.** Rejected: unfillable without an order book, see decision 2.
- **Trading every significant bucket regardless of fee.** Rejected: the
  0.50-0.70 buckets have a gross edge and a negative net one. Trading them
  would be paying Kalshi to express a view.
- **Assuming a fixed 1c or 2c spread.** Rejected as a fabricated number that
  would look rigorous. Reporting breakeven slippage puts the same information
  in the reader's hands without inventing a measurement.
- **Exiting before settlement.** Rejected: it doubles the fee and requires
  modelling an exit price we do not have.
