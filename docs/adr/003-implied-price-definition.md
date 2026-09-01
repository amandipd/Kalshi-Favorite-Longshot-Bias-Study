# ADR 003 - What counts as "the market's implied probability"

**Status:** Accepted (Phase 2, 2026-08-28)

## Context

The whole study rests on one number per contract: the price we read as the
market's forecast of P(event). Everything downstream -- the calibration curve,
the Brier decomposition, the strategy backtest -- is a statement about
whatever that number turns out to be. Choosing it wrongly does not produce a
noisy result; it produces a confident answer to a different question.

The proposal framed this as a free choice between three candidates: last
traded price, bid-ask mid at settlement, and a VWAP over some final window. It
is not free. Two measurements against the ingested corpus (145,047 markets,
143,143 of them binary) eliminated most of the option space before any
judgment was applied.

### The settled snapshot carries no usable book

`/historical/markets` returns one post-settlement snapshot per market, not a
time series. In that snapshot the order book is gone:

- **54.9%** of markets report `yes_bid = 0.0000` and `yes_ask = 1.0000` --
  a maximally wide quote, which is the absence of a book rather than a price.
- Median bid-ask spread across the corpus is **1.0000**. Mean is 0.5755.
- `open_interest_fp` is **0.0000 for every one of the 145,047 markets** --
  positions are cleared at settlement.

A bid-ask mid computed from this returns 0.50 for the majority of contracts
regardless of what the market ever believed. **Bid-ask mid is not available**,
and no amount of preference makes it so.

### The last traded price is the outcome restated

The more dangerous finding. Cross-tabulating snapshot `last_price_dollars`
against `result` over all 143,143 binary markets:

|  | result=no | result=yes |
|---|---|---|
| `last_price <= 0.01` | 73,508 | 949 |
| `last_price >= 0.99` | 36 | 58,942 |
| strictly between | 6,893 | 2,815 |

**92.5% of binary markets have a last price pinned to the correct settlement
value.** Only 9,708 markets (6.8%) carry a price strictly between 1c and 99c.

A calibration study built on this number would report near-perfect calibration
at the extremes and would be measuring nothing: the "forecast" and the outcome
are the same variable. This is the single most important thing to get right in
the project, and it is invisible unless you look -- the field is well named,
well typed, present on every record, and wrong for our purpose.

**It is not a contamination bug.** The obvious hypothesis is that the snapshot
leaks post-settlement trading. It does not. Sampling the last trade at or
before `close_time` itself (T-0h) over 250 markets shows **90.0% pinned** --
essentially the same. The price is a genuine market price; the market has
simply stopped being uncertain. Kalshi's universe is dominated by short-lived
recurring contracts (median lifetime open->close is **24.4 hours**), and their
uncertainty resolves in the final minutes.

So the real problem is not *which* price but *when*. A price sampled at the
moment of resolution is not a forecast, whatever field it comes from.

### There is a time series, on a different endpoint

`/historical/trades?ticker=<t>&max_ts=<unix>&limit=1` returns the last trade at
or before any timestamp, filtered to that market. That makes a price at a
chosen horizon before close available at one request per market per horizon.

Sweeping the horizon over 250 sampled binary markets with volume > 0:

| horizon | retention | pinned | usable (retention x unpinned) |
|---|---|---|---|
| T-0h | 99.6% | 90.0% | 10.0% |
| **T-1h** | **89.2%** | **17.5%** | **73.6%** |
| T-6h | 74.8% | 4.8% | 71.2% |
| T-24h | 30.8% | 7.8% | 28.4% |
| T-72h | 6.8% | 29.4% | 4.8% |

- *retention*: share of markets that have any trade that early. It collapses
  past 6h because most markets did not exist yet -- a market with a 24-hour
  life has no T-24h price by construction.
- *pinned*: share of retained prices sitting at <=0.01 / >=0.99 on the side
  that won.

The two failure modes run in opposite directions. Sample too late and the
price is the answer; sample too early and the market has no price at all, and
the survivors are exactly the long-lived contracts -- a sample selected on a
property correlated with category, liquidity and event type.

## Decision

**`implied_price` is the price of the last trade at or before
`close_time - horizon`, on the YES leg, with the horizon a config parameter.
The primary horizon is T-1h.**

T-1h maximises usable yield (73.6%): it keeps 89.2% of markets while leaving
only 17.5% of prices pinned. Its decile table is a well-formed calibration
curve rather than a two-spike distribution:

```
T-1h    [0.0-0.1) n=49 realised=0.00      [0.5-0.6) n=19 realised=0.58
        [0.1-0.2) n=19 realised=0.11      [0.6-0.7) n=13 realised=0.77
        [0.2-0.3) n=19 realised=0.21      [0.7-0.8) n=16 realised=0.69
        [0.3-0.4) n=15 realised=0.33      [0.8-0.9) n=17 realised=0.88
        [0.4-0.5) n=19 realised=0.63      [0.9-1.0) n=37 realised=0.95
```

Supporting decisions:

1. **Horizon is config-driven, not hardcoded.** `ingest.trades.horizons` and
   `clean.price_horizon` carry it. The choice of 1h is defensible but not
   uniquely correct, and a result that only holds at one horizon is a result
   about that horizon.
2. **T-6h and T-24h are planned sensitivity runs.** The ingester is keyed on
   (market, horizon), so adding a horizon later re-fetches nothing already on
   disk. If the sign of the bias flips between horizons, that is a finding
   about time-to-resolution -- Phase 3's second segmentation axis -- not an
   inconsistency to hide.
3. **Price is always the YES leg.** Kalshi reports `yes_price_dollars` on
   every trade regardless of which side the taker was on, so no flip is needed
   at this layer. NO-framing normalisation happens in `clean.py`, per ADR 004.
4. **"No trade before the horizon" is recorded, not silently skipped.** The
   ingester writes an explicit null for those markets. They are a documented
   exclusion with a count, not an absence -- and their share by category is
   itself reportable, since it measures how much of each category is too
   short-lived to have a forecast at all.
5. **Pinned prices are kept, not filtered.** A market genuinely trading at 1c
   an hour before close is a real longshot and belongs in the longshot bucket.
   Dropping prices for being extreme would delete precisely the observations
   the favorite-longshot hypothesis is about.
6. **VWAP is not used.** It needs full trade history: ~333k requests and
   roughly **55 GB** for this corpus, against ~60 MB for a horizon snapshot.
   The cost is not justified by the marginal robustness, and a single last
   trade is the simpler thing to defend.

## Consequences

**Gained**

- A price measured while the market was actually forecasting, which is the
  minimum requirement for the question to mean anything.
- Time-to-resolution segmentation comes almost free -- it is the same
  ingester with a different horizon.
- The failure mode that would have quietly destroyed the study is documented
  with numbers rather than avoided by luck.

**Given up / limitations to state in the writeup**

- **A second ingestion pass**, ~2.7 hours per horizon at 15 req/s. Resumable,
  so it survives interruption, but it is real time.
- **~11% of markets have no T-1h price** and are excluded. That exclusion is
  not random: it removes the shortest-lived contracts, which skew toward
  high-frequency Sports and Crypto series. The cleaning log reports the drop
  by category so the reader can judge it.
- **A last trade can be stale.** In a thin market the last print an hour
  before close may be hours old and may not reflect the book at the horizon.
  With no book in the historical data there is no way to detect this per
  market; `volume` is carried on every row so staleness can be probed as a
  liquidity interaction in Phase 3.
- **17.5% of T-1h prices are still pinned.** For genuinely lopsided contracts
  this is correct behaviour, not an artifact -- but it does mean the extreme
  buckets mix "confidently priced" with "already effectively decided," and the
  T-6h run is the check on how much that matters.
- **One horizon per market, not a path.** Questions about price dynamics --
  drift, momentum into resolution -- are out of scope under this decision.

## Alternatives considered

- **Snapshot `last_price_dollars`.** Rejected: 92.5% pinned. It answers
  "does the settlement price predict settlement."
- **Bid-ask mid at settlement.** Rejected: not available. 54.9% of books are
  0/1 wide and open interest is zero everywhere.
- **`previous_price_dollars`.** Rejected: identical to `last_price` on 96.5%
  of records, so it inherits the same defect.
- **Restrict to the 6.8% unpinned snapshot markets.** Rejected, and worth
  naming because it is the tempting no-work option: that subsample is selected
  on *being unresolved at close*, which is conditioning on a variable
  downstream of the outcome. It would bias the result in a direction that is
  hard to sign and impossible to defend.
- **Full trade history + VWAP over a final window.** Rejected on cost: ~55 GB
  and ~6.2 hours, for a second-order improvement over a single last trade.
- **A longer horizon (T-24h) as primary.** Rejected: 30.8% retention, and the
  survivors are structurally different markets. Retained as a sensitivity run
  where its sample is treated as its own population, not as a subset.
