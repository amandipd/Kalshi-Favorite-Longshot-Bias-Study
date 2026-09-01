# The Favorite-Longshot Bias in Kalshi Prediction Markets

*A calibration study and out-of-sample backtest*

Amandip Dutta · September 2026

---

## Abstract

We test whether Kalshi's prediction-market prices are calibrated
probabilities, using 100,210 settled binary contracts across 29,895 events
(December 2025 – June 2026). Contracts are priced at the last trade one hour
before close rather than at settlement, because the settled-snapshot price is
92.5% identical to the outcome and therefore uninformative as a forecast.
Standard errors and confidence intervals cluster on the underlying event,
since a mean 3.35 contracts share each event and are not independent draws.
We find Kalshi's prices are well-calibrated overall (Brier reliability
0.00029 of a total Brier score of 0.1213) but exhibit a small, statistically
robust favorite-longshot bias: contracts priced below 50c resolve yes less
often than their price implies, and contracts above 50c resolve more often,
with peak bias of 2.98 cents at the 30-40c band. A bucket-free logistic
calibration confirms the direction (slope 1.044, 95% CI [1.023, 1.066],
p = 5.0e-05 against the calibrated ideal of 1.0). The bias concentrates in
short-lived markets and appears substantially larger — though on a thin,
underpowered sample — outside the Sports category that dominates the pooled
data. An out-of-sample, fee-aware, Kelly-sized backtest with a falsification
control shows the effect survives Kalshi's transaction fees (+1.23% ROI vs.
-14.6% for the anti-bias control), but the margin that remains — under one
cent per contract — is smaller than a plausible bid-ask spread, which we
cannot measure directly from historical data. We conclude the bias is real
and statistically well-established, but its tradeable value is marginal at
best and likely absorbed by real-world execution costs.

---

## 1. Motivation

A binary prediction-market contract settles at $1 if an event occurs and $0
otherwise. A contract trading at 30c is, definitionally, the market's
collective statement that the event has a 30% chance of happening. This is a
falsifiable claim: gather every contract that traded near 30c, and check what
fraction of them actually resolved yes.

The **favorite-longshot bias** is the empirical regularity, first documented
in horse-race betting and since replicated across sports betting and other
prediction markets, that low-probability outcomes are systematically
overpriced (bettors pay more than fair value for a longshot) and
high-probability outcomes are systematically underpriced. If it holds on
Kalshi, it implies retail prediction-market prices are not fully efficient
probability estimates, and it suggests a mechanical trading edge: fade
longshots, buy favorites.

This study measures the bias directly from Kalshi's historical data,
establishes its statistical significance under the correlation structure the
data actually has, and tests whether it survives contact with real trading
costs.

## 2. Data and the central methodological problem

We ingested every finalized, binary Kalshi contract from the twenty
highest-volume series in four categories (Politics, Economics, Sports,
Crypto) closing between 2025-12-01 and 2026-06-06: 145,047 tickers, of which
143,143 are binary (vs. scalar) settlements.

**The obvious price field is uninformative.** Every settled market reports a
`last_price`. We measured that 92.5% of binary markets have this price pinned
to within one cent of the settlement value (under 1c if the answer was no,
over 99c if yes). Kalshi markets are short-lived — median lifetime from open
to close is roughly 24 hours — and by the time a market closes, its price has
already converged to the answer. A calibration curve built on this field
would compare the outcome against itself and produce a spuriously perfect
result.

We instead price every contract at the last trade **one hour before its
close**, pulled via a second ingestion pass over Kalshi's historical trades
endpoint. This reduces the pinned-price rate to 1.1%. The one-hour horizon
was chosen by sweeping candidate horizons (0h, 1h, 6h, 24h, 72h) and
maximizing `retention x (1 - pinned rate)`: too close to close and the price
is the answer; too far before close and the surviving sample selects on
market longevity, which correlates with category and event type.

After applying the study's inclusion criteria (binary, finalized, closed
within the study window, non-zero volume, priced at the T-1h horizon), the
processed dataset contains **100,210 contracts across 29,895 events**.

## 3. Statistical method

**Non-independence.** Contracts sharing an `event_ticker` are not independent
observations. A 250-contestant field ("who wins the PGA Championship") is one
underlying outcome expressed as 250 contracts, of which exactly one resolves
yes; threshold ladders on a single economic release are monotonically related
by construction. Mean contracts per event is 3.35, and 99.9% of markets have
at least one sibling. Every standard error, confidence interval, and p-value
in this study is computed clustering on `event_ticker`: a cluster-robust
variance estimator (Liang-Zeger) for point-estimate standard errors, and a
block bootstrap (resampling whole events with replacement, 2,000
replications) for confidence intervals on realized frequencies.

**Multiple comparisons.** Testing ten price buckets independently at
p < 0.05 would produce roughly a 40% chance of at least one false positive
under a true null. We control the false discovery rate via
Benjamini-Hochberg correction across all buckets tested in a given table,
including across segments when segmenting by category or lifetime.

**Bucketing.** Ten fixed-width price buckets (0-10c, ..., 90-100c), with
edges chosen before observing the data, since the hypothesis concerns
specific price levels rather than data-chosen quantiles.

## 4. Calibration results

Overall Brier score: **0.1213**, decomposed via the Murphy decomposition into
reliability (calibration error) 0.00029, resolution (discrimination) 0.1258,
and uncertainty (base-rate variance) 0.2476. Reliability near zero indicates
the market is, in aggregate, extremely well calibrated — the forecast error
is almost entirely attributable to irreducible outcome variance and genuine
discriminative skill, not to systematic mispricing.

| Price band | n | Mean price | Realized freq. | Bias | 95% CI (clustered) | q-value |
|---|---|---|---|---|---|---|
| 0-10c | 22,993 | 0.034 | 0.030 | -0.004 | [0.026, 0.033] | 0.016 |
| 10-20c | 10,198 | 0.142 | 0.118 | -0.025 | [0.111, 0.125] | <0.001 |
| 20-30c | 8,119 | 0.243 | 0.216 | -0.027 | [0.207, 0.226] | <0.001 |
| 30-40c | 7,096 | 0.344 | 0.315 | **-0.030** | [0.304, 0.326] | <0.001 |
| 40-50c | 6,682 | 0.445 | 0.420 | -0.025 | [0.408, 0.433] | <0.001 |
| 50-60c | 6,394 | 0.544 | 0.555 | +0.011 | [0.544, 0.567] | 0.069 |
| 60-70c | 6,766 | 0.646 | 0.654 | +0.009 | [0.643, 0.666] | 0.147 |
| 70-80c | 6,660 | 0.746 | 0.762 | +0.016 | [0.751, 0.772] | 0.004 |
| 80-90c | 7,568 | 0.846 | 0.862 | +0.016 | [0.854, 0.871] | <0.001 |
| 90-100c | 17,734 | 0.965 | 0.957 | -0.009 | [0.953, 0.960] | <0.001 |

Every bucket below 50c is overpriced (negative bias, market charges more than
the event is worth); four of five buckets above 50c are underpriced. The
pattern is the favorite-longshot shape. Peak bias is at the 30-40c band, and
eight of ten buckets remain significant after correction. Notably, the two
buckets straddling the 50c midpoint are the only ones not significant — the
market's hardest region to distinguish from perfectly calibrated is exactly
where the theory predicts the effect should vanish.

**Logistic calibration.** As a bucket-free confirmation, we fit
`logit(P(outcome)) = a + b * logit(price)` with cluster-robust standard
errors. Perfect calibration implies (a, b) = (0, 1). We estimate
**b = 1.044** (95% CI [1.023, 1.066]), significantly above 1 (p = 5.0e-05),
which is the favorite-longshot direction: true probabilities are more
extreme than the market's prices. A joint Wald test rejects (0, 1) at
p = 1.1e-08.

**Segmentation.** The bias concentrates in markets with short trading
lifetimes — the shortest quartile (under 15.6 hours from open to close) is
significantly miscalibrated in four of five price bands, up to -5.5c, while
the two middle lifetime quartiles are statistically indistinguishable from
calibrated. By category, the pooled result is 95.9% Sports by contract count;
Economics (186 events) shows a substantially larger bias (-13.7c in its
20-40c band) and Politics (16 events) returns no testable cell under the
power floor. We treat the non-Sports result as a lead requiring more data,
not a confirmed finding — see `docs/findings.md` §4 and `docs/limitations.md`.

## 5. Out-of-sample backtest

**Design.** We split chronologically at the 60th percentile of contract close
dates (2026-04-20), estimating the bias exclusively on the 60,114 contracts
that had settled before that date and trading exclusively the 40,071
contracts priced at or after it. Markets in the settle/price gap (priced
before, settling after) are excluded from both sides. The trading rule — which
buckets to trade, on which side, at what estimated edge — is derived entirely
from the training partition; the trading loop never receives an out-of-sample
outcome. A shuffle test confirms this structurally: replacing every
out-of-sample outcome with random noise leaves every position size
byte-identical.

A bucket is traded when its in-sample estimate is both statistically
significant (post-correction) and economically positive after Kalshi's taker
fee, `fee = ceil(0.07 * C * P * (1-P))` (verified against Kalshi's CFTC rule
filing). Positions are sized at half-Kelly using the in-sample edge, capped
at 2% of bankroll per position and constrained by a daily portfolio budget
(1.0x bankroll) — necessary because the strategy holds hundreds of
simultaneous positions on a typical settlement day, and per-position Kelly
sizing alone would ask for roughly 11x available capital.

**Falsification control.** We additionally backtest the exact mirror-image
strategy — inverting the estimated bias, not merely the trade direction — which
should lose money out-of-sample if the measured effect is real.

**Results**, 27,404 trades over 48 settlement days:

| Metric | Strategy | Anti-bias control |
|---|---|---|
| ROI (on capital deployed) | **+1.23%** | -14.59% |
| Final bankroll (start = 1.0) | 1.74x | 0.0003x |
| Hit rate | 82.9% | 17.1% |
| Max drawdown | -11.4% | -99.97% |

The control loses decisively on the identical contracts under identical
fees, which is the falsification the design was built to allow.

**The number that matters most, however, is breakeven slippage: 0.98 cents
per contract.** This is the adverse fill (relative to the last traded price)
that would exactly erase the strategy's entire net edge. The backtest fills
at the last traded price with no spread cost, because settled Kalshi
snapshots carry no usable order book (54.9% quote a $0.00/$1.00 spread) —
the same data limitation that motivated the T-1h pricing horizon in the
first place. A real taker order crosses the bid-ask spread; if that spread
costs more than roughly a cent to cross on these markets, which is a
plausible figure for thin retail prediction markets, the entire measured
edge disappears. Kalshi's own fees already consume roughly a third of the
gross bias before slippage is considered at all.

**Sensitivity.** Three thresholds in the backtest design could plausibly have
been set differently: how much fee-adjusted margin a bucket must clear before
it is traded (`min_net_edge`), where the train/test boundary falls
(`train_fraction`), and the Kelly multiplier. Re-running the full backtest
and its control across a range of each shows the qualitative result is not
balanced on the specific values used above. Raising `min_net_edge` from 0 to
1.0c *increases* ROI (1.23% -> 2.40%) while trading progressively fewer,
higher-margin buckets, consistent with the edge being concentrated rather
than manufactured by marginal inclusions; above 1.0c no bucket clears the
bar, which bounds how much margin exists rather than indicating failure.
Moving the split point across `train_fraction` in [0.4, 0.8] keeps ROI
positive (1.05%-2.23%) and the control losing throughout. The Kelly
multiplier, swept as an invariance check rather than a search, leaves ROI
nearly flat (1.03%-1.23%), confirming that sizing rescales stakes without
changing which side wins. Full sweep in `reports/figures/07_sensitivity.png`.

## 6. Discussion and conclusion

Kalshi's prices are, in aggregate, close to honest probabilities: reliability
error is three ten-thousandths of the total Brier score. On top of that
near-perfect calibration sits a small, statistically robust favorite-longshot
bias — a few cents at its peak, present across eight of ten price buckets
after correcting for both event clustering and multiple comparisons, and
confirmed independently by a bucket-free logistic fit.

Whether that bias is *exploitable* is a separate and harder question than
whether it is *real*. Our backtest shows the bias survives Kalshi's stated
transaction fee schedule out-of-sample, with a working falsification control
that behaves exactly as required. But the margin that survives fees — under a
cent per contract — sits below the threshold of a cost we cannot directly
measure from historical data (the bid-ask spread), which means the honest
summary is not "this strategy is profitable" but rather "the edge, after
known costs, is smaller than an unknown but plausibly comparable cost."

The clearest paths to a stronger conclusion, in order of expected value, are:
obtaining live order-book data to replace the breakeven-slippage estimate
with a measured one; replicating the finding on a second venue (Polymarket)
to test whether it is a property of prediction markets generally or of
Kalshi specifically; and extending the observation window to power the
non-Sports segments past the point where they are currently classified as
underpowered.

Full methodology: `docs/methodology.md`. Complete results, including
segmentation tables: `docs/findings.md`. Design decisions and their
alternatives: `docs/adr/`. Honest accounting of what this study does not
establish: `docs/limitations.md`.
