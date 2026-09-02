# Design decision doc 005 - How calibration is bucketed, bounded and tested

**Status:** Proposed (Phase 3, 2026-09-01) -- awaiting ratification

## Context

Design decision doc 003 settled which number is the forecast; design decision doc 004 settled which rows carry
it. This one settles how the 100,210 surviving contracts become a *claim*: how
prices are grouped, what the interval around each group means, and what has to
be true before a gap between price and outcome is called real.

The measurement is arithmetically trivial -- bucket the prices, average the
outcomes, subtract. Every decision below is about the uncertainty around that
subtraction, because the point estimates were never in doubt and the intervals
are the entire difference between a chart and a finding.

Three threats stand between the table and the claim, and they are independent
of each other:

1. **The buckets themselves.** Where the edges go decides what "a longshot"
   means, and edges chosen after seeing the data decide it in the data's
   favour.
2. **Correlated observations inside a bucket.** design decision doc 004 measured this: 100,210
   contracts come from 29,895 events, and siblings are logically bound rather
   than merely similar. A bucket of 22,993 contracts drawn from 10,280 events
   does not carry 22,993 observations' worth of information.
3. **Many tests.** Ten buckets tested at p < 0.05 apiece produce roughly a 40%
   chance of at least one false positive under a true null, before the study
   segments by category or horizon and multiplies the count.

## Decision

### 1. Ten equal-width buckets, edges fixed before looking

Deciles of price, `[0.0, 0.1)` through `[0.9, 1.0]`, left-closed with the top
edge closing so a contract printing at exactly 1.00 has somewhere to go.

Equal *width*, not equal count. Quantile buckets would hold ~10,000 contracts
each and give tighter tail intervals, which is exactly the trade being
refused: the hypothesis is a claim about price *levels* ("contracts priced
0-10c are overpriced"), so the levels have to be named in advance rather than
discovered. Data-chosen edges would also break comparability with the
favorite-longshot literature, which uses fixed bands throughout.

Ten is the literature's convention and needs no defence from this data, but it
happens to cost nothing here: the thinnest bucket holds 6,394 contracts from
5,613 events, so bucket width is nowhere near the binding constraint on
precision. Clustering is.

**Note on the left-closed convention.** The exploratory table in the journal
entry of 2026-08-29 used pandas' default right-closed bins, which put the
1,124 contracts priced at exactly 0.10 in the *bottom* bucket. This design decision doc closes
the other way, so the bottom bucket falls from 24,117 to 22,993. Left-closed
is the convention that matches how the bands are spoken about -- 10c is the
start of the 10-20c band, not the end of the 0-10c one -- and it needs no
special case at zero. Any table computed before this date differs from the
published one for that reason and no other.

### 2. Wilson intervals over the normal approximation -- but never reported alone

Wilson score intervals are computed for every bucket. The normal (Wald)
interval `p +/- z*sqrt(p(1-p)/n)` fails precisely where this study lives: at
`p_hat = 0.03` it extends below zero, and at `p_hat = 0` it has zero width,
claiming certainty from the least informative data available. Wilson inverts
the score test instead, evaluating the variance at the hypothesised proportion
rather than the estimate, so it stays inside [0, 1] and stays sensibly wide at
the extremes.

**Wilson still assumes independent draws, which this dataset does not have.**
It is computed only as the denominator of the design effect -- the factor by
which pretending independence would have narrowed the interval. Design decision doc 004
decision 6 forbids reporting it as the interval, and `format_table` in
`src/analysis/report.py` does not print it.

### 3. Every interval and every p-value clusters on `event_ticker`

Two mechanisms, both clustering on the event, because they answer different
questions:

- **Cluster-robust standard error** (Liang-Zeger, specialised to a mean) for
  the per-bucket bias and its p-value. It sums residuals *within* an event
  before squaring, so the cross-terms that independence assumes away are kept.
- **Block bootstrap** resampling whole events, for the interval on realized
  frequency. Resampling contracts individually would rebuild the independence
  the clustering exists to deny -- it would happily draw thirty copies of one
  golfer and none of his field, violating the "exactly one winner" constraint
  that makes the siblings correlated in the first place.

The bootstrap runs **once for the whole table**, not once per bucket. A single
event's contracts land in several price buckets, so a replicate that drops
that event must drop it from all of them together; anything else produces ten
intervals that are individually defensible and jointly incoherent.

The reference distribution is Student's t with G-1 degrees of freedom rather
than the normal. At 29,895 events the difference is invisible, but the
per-category segments in Phase 3 Milestone 2 run on far fewer events, and
using t everywhere means the headline table and the thin segments are computed
the same way rather than differently at the point where it starts to matter.

Seed and replication count live in `config.yaml` (`bootstrap_seed`,
`bootstrap_reps`), so an identical processed table reproduces identical
intervals forever.

### 4. Benjamini-Hochberg across buckets, not Bonferroni

Significance is read off the BH-corrected q-value at `fdr_alpha = 0.05`; the
raw p-value is retained in the table but never carries a star.

Bonferroni controls the probability of *any* false positive, which is the
right target when one false claim is ruinous. Here the claim is a shape across
buckets, so the useful guarantee is the false discovery rate: of the buckets
called significant, at most 5% are expected to be spurious. BH is also far
less conservative, and that matters when the effect is 2-3 percentage points
-- Bonferroni at m=10 would demand p < 0.005 per bucket and would be
measuring its own conservatism as much as the market's.

Correction and clustering are **not substitutes**. Correction handles testing
many hypotheses; clustering handles correlated observations inside one. Doing
only the first gives confident intervals that are simply too narrow, and doing
only the second gives honest intervals swept for the best-looking bucket.

### 5. The Murphy decomposition is reported against a `binned_brier`, not an approximation

`reliability - resolution + uncertainty = brier` holds exactly only when every
forecast inside a bucket is the same number -- true of a forecaster who only
ever says 10%, 20%, ..., false here, where prices vary continuously inside each
decile.

Rather than assert the identity approximately, `brier_decomposition` returns
both scores: `binned_brier`, computed with each contract's price replaced by
its bucket mean, which the identity closes on to machine precision; and
`brier`, the real score on unbinned prices. Their difference is reported as
`binning_residual`. It is a diagnostic, not an error term -- a large residual
means the buckets are too coarse to describe the forecasts. Measured here it
is -0.00075 against a Brier of 0.1213, or 0.6%, so decile buckets describe
these prices well.

The unit test asserts the identity as an *exact* equality against
`binned_brier` at 1e-12. An approximate assertion against the raw score would
pass with a term subtly wrong.


### 6. Segments share one correction family, with a power floor

Added in Milestone 2, when segmentation multiplied the number of tests.

Every segment x bucket test enters a single Benjamini-Hochberg family.
Correcting inside each segment separately would let the segment count grow for
free, which is the failure the correction exists to prevent.

Stated carefully, because the intuitive version is wrong: pooling is **not
uniformly stricter**. BH is a step-up procedure, so a segment carrying strong
signal lifts the others' ranks faster than it lifts m, and a marginal test can
come out with a smaller q pooled than alone. Verified directly -- a segment of
five p-values at 1e-8 pulls a neighbouring 0.02 from q=0.10 to q=0.033. The
guarantee is about the share of false discoveries across the family, not about
any single q moving in one direction, and a test asserting the monotone version
was written, failed, and was replaced rather than the code being "fixed".

A segment bucket with fewer than **30 events** is reported with its estimate and
interval but never tested, never starred, and never enters the family
(`min_events_per_bucket`). Clustered inference is governed by the number of
events, and Politics has 16 in total; testing such a cell spends alpha on a
question the data cannot answer and makes every real test pay for it. Those
cells are flagged `underpowered` rather than dropped -- deleting them would hide
that the category is in the study at all.

Segment tables use **five** price buckets rather than ten. Coarser buckets for
thinner slices, and the number was fixed from the segment *sizes* (Sports
29,471 events, Economics 186, Crypto 222, Politics 16) before any segment
result was computed. Edges chosen after seeing which split looks significant
are not edges; they are a finding manufactured by hand.

### 7. Logistic calibration is tested against slope 1, not slope 0

`logistic_calibration` fits `logit P(outcome) = a + b * logit(price)` with
cluster-robust errors and tests H0: b = 1 and H0: a = 0, plus a joint Wald test
of the pair.

Statsmodels' default `P>|z|` tests b = 0, which asks whether price predicts
outcome at all. It does, at z > 90. Reporting that column would be reporting a
tautology as a finding, so the test against 1 is computed explicitly and a unit
test asserts a perfectly calibrated synthetic dataset returns a *large*
p-value.

The slope's direction is derived in the docstring rather than remembered:
**b > 1 is the favorite-longshot direction** (true probabilities more extreme
than prices, so longshots are overpriced). This is the opposite of the familiar
"slope < 1 means overconfident" from the forecast-evaluation literature, which
regresses the other way round. Two unit tests plant b = 1.3 and b = 0.7 and
assert the fitted sign, so the convention cannot silently invert.

No price in the processed layer sits at exactly 0 or 1 -- Kalshi's tick floor is
0.001, and the observed range is [0.001, 0.999] -- so the logit is defined
everywhere and no clipping is applied. A boundary price raises rather than being
winsorised, because choosing how to handle it would be a research decision and
not a numerical convenience.

### 8. "Time to resolution" is replaced by market lifetime, because it does not vary

The proposal calls for `bias_by_time_to_resolution`. That variable is constant
in this dataset: design decision doc 003 prices every market at exactly one hour before close,
so the gap between forecast and outcome is ~1 hour for all 100,210 rows by
construction. There is nothing to segment, and a function with that name would
be measuring settlement lag while claiming to measure forecast horizon.

`bias_by_lifetime` segments on `close_ts - open_ts` instead: how long the market
traded *before* it was priced. Median 25 hours, quartiles at 15.6 / 25.0 / 45.1,
tail past two years. It supports the question the proposal was reaching for --
does a market with more time to aggregate information price better? -- and the
answer is yes, sharply.

Lifetime buckets are **quantiles**, the opposite of the fixed-width choice made
for price, and for the opposite reason: price bands are the hypothesis and must
be fixed in advance, while lifetime is a nuisance dimension with a two-year tail
and no theory about where its edges belong. Equal-width bins would put 99% of
the corpus in the first one.

Note the confound in any reading of this result: lifetime correlates with
contract type, since a 15-hour market is overwhelmingly a same-day sporting
event.

## Consequences of the Milestone 2 additions

- The logistic fit agrees with the bucketed table: slope **1.0442**
  (95% CI [1.0229, 1.0656]), significantly above 1 at p = 5.0e-05, with
  intercept -0.0542. The shape in the decile table is not an artefact of the
  bin edges.
- **Politics is untestable in every cell** and is reported as such. This is the
  power floor doing its job on the first real dataset it met.
- The largest biases in the study are in Economics and Crypto, on 186 and 222
  events respectively -- powered enough to test, thin enough that they are
  leads rather than results.
- The shortest-lived quartile of markets is miscalibrated in four of five price
  bands; the two middle quartiles are almost entirely indistinguishable from
  calibrated.

## Consequences

Measured on the 100,210-contract processed table (`make analyze`,
`reports/calibration_table.csv`):

| | |
|---|---|
| Brier score | 0.1213 |
| reliability (calibration error) | 0.00029 |
| resolution (discrimination) | 0.1258 |
| uncertainty (base rate 0.4512) | 0.2476 |

**Clustering cost what design decision doc 004 predicted it would, where it predicted it.**
The design effect is ~1.03 through the middle buckets and rises in exactly the
two tails: 1.45 in the 0-10c bucket and 1.20 in the 90c-100c bucket, the two
buckets that hold the large mutually-exclusive fields. In the bottom bucket
the corrected q-value moves from 5e-04 to 1.6e-02 -- a thirtyfold weaker
claim, still significant.

No bucket's conclusion actually flips between the naive and clustered
treatments in the pooled table. That is a result about *this* dataset's
balance, not a licence to skip the clustering: the shift is largest precisely
in the longshot bucket the hypothesis is about, and the per-category segments
have one to two orders of magnitude fewer events to spend.

Two buckets (0.50-0.60 and 0.60-0.70) are not significant after correction.
The middle of the price range is where the market is hardest to distinguish
from calibrated, which is what the hypothesis predicts.

## Alternatives considered

- **Quantile (equal-count) buckets.** Rejected: edges chosen by the data, and
  incomparable to the literature. Worth running as a robustness check, where
  the question is whether the shape survives a different partition.
- **Bonferroni.** Rejected as too conservative for a 2-3 point effect; see
  decision 4. Cheap to report alongside if a reviewer prefers it.
- **Wald/normal intervals.** Rejected: degenerate at the extremes, which is
  where the hypothesis lives.
- **A random-effects (multilevel) model with an event-level intercept.**
  Rejected as the primary treatment: it buys efficiency by assuming a
  distribution for the event effects, and the events here are heterogeneous by
  construction (a 250-golfer field and a 2-contract CPI ladder are not draws
  from one population). Cluster-robust SEs assume nothing about that
  distribution, which is the right trade when the clustering structure is
  known but its shape is not.
- **Weighting each contract by 1/event size.** Rejected as primary in design decision doc 004
  because it silently changes the estimand from per-contract to per-event.
  Still worth reporting as a robustness check, and it is the natural
  companion to the clustered intervals rather than a replacement.
- **Bootstrapping each bucket independently.** Rejected: an event spans
  buckets, so per-bucket resampling produces intervals that cannot be read as
  a table.
- **`test_bucket_bias` in `statistics.py`, as the proposal sketched it.**
  Moved: the bucket structure is defined in `calibration.py`, and putting the
  test there keeps `statistics.py` a pure estimator library that knows nothing
  about contracts. The function is `calibration_table`, which returns the
  inference columns alongside the point estimates so a bucket's number and its
  q-value cannot be computed from different subsets.
