# Methodology

How the calibration claim in `docs/findings.md` is constructed, and what each
step is defending against. This is the methods section of the final report.

The decisions themselves live in the design decision docs — this document says what the method
*is*; the design decision docs say why it is that and not something else.

| | |
|---|---|
| `adr/001` | storage layers |
| `adr/002` | ingestion idempotency |
| `adr/003` | which number is the forecast |
| `adr/004` | which rows are in the study |
| `adr/005` | bucketing, intervals, and tests |

---

## 1. Data

**Population.** Every Kalshi market that is binary, finalised, closed between
2025-12-01 and 2026-06-06, traded at least once, and has a trade at the pricing
horizon. 100,210 contracts across 29,895 events, from 145,047 ingested tickers.

**Forecast.** `implied_price` is the YES price of the last trade at or before
`close_time − 1 hour`, read as P(event occurs).

The horizon is the single most consequential choice in the study. The settled
snapshot's `last_price` — the obvious field — is **92.5% pinned to the
settlement value**: under 1c when the answer was no, over 99c when it was yes.
It is not contamination; Kalshi's markets are mostly short-lived (median
lifetime 25 hours) and their uncertainty burns off in the final minutes, so the
last trade before close is 90% pinned too. A calibration curve built on it
would be near-perfect and would mean nothing, because forecast and outcome
would be the same variable.

Sampling at T-1h drops pinning to **1.1%**. One hour maximises usable yield —
retention × (1 − pinned) — against the opposing failure at long horizons, where
the market did not yet exist and the survivors are a sample selected on
longevity. Full sweep in design decision doc 003.

**Outcome.** 1 if the event occurred, 0 otherwise, always defined relative to
the same proposition the price refers to. No contract needs a NO-framing flip:
measured across all 143,143 binary markets, titles containing negations number
zero (design decision doc 004).

**Exclusions** are counted against a named reason and broken down by category,
because an exclusion falling unevenly across categories is a bias rather than a
smaller sample. Parsing drops only rows that cannot become a typed contract;
every research exclusion happens in one later step and is config-driven.

## 2. Bucketing

Ten equal-width price buckets, `[0.0, 0.1)` … `[0.9, 1.0]`, left-closed with
the top edge closing.

Equal **width**, not equal count. Quantile buckets would give tighter tail
intervals, which is exactly the trade being refused: the hypothesis is a claim
about price *levels*, so the levels must be named in advance rather than
discovered in the data, and fixed bands are what the favorite-longshot
literature uses. Bucket width is not the binding constraint here anyway — the
thinnest bucket holds 6,394 contracts.

Segment tables use five buckets rather than ten, chosen from the segment
**sizes** (Sports 29,471 events, Politics 16) before any segment result was
computed.

## 3. Scoring

**Brier score**, the mean squared error of the price against the outcome. It is
a proper scoring rule: expected score is minimised only by reporting a true
belief, so it cannot be gamed by shading forecasts toward the extremes.

**Murphy decomposition** splits it into

```
brier = reliability − resolution + uncertainty
```

- *reliability* — calibration error, where the favorite-longshot bias lives
- *resolution* — how far buckets spread from the base rate, i.e. discrimination
- *uncertainty* — the base rate's own variance, which no forecast can change

The distinction matters because a forecaster who answers with the base rate
every time is perfectly calibrated and completely useless. Skill is calibration
*plus* resolution.

**On the identity's exactness.** It closes exactly only when every forecast
inside a bucket is identical, which is false for continuously varying prices.
Rather than assert it approximately, both scores are reported: `binned_brier`,
computed with each price replaced by its bucket mean, which the identity closes
on to machine precision, and `brier`, the real score. The difference is
`binning_residual` — a diagnostic of whether the buckets are fine enough, not
an error term. Measured: −0.00075 against 0.1213, so 0.6%.

## 4. Uncertainty

This is where most of the method's work goes, and there are two independent
threats.

### 4.1 Correlated observations — clustering

Contracts sharing an `event_ticker` are not independent draws. A 250-golfer
field is one underlying outcome expressed as 250 contracts, of which exactly
one resolves yes; a threshold ladder is monotonically bound by construction.
99.9% of markets sit in an event with at least one sibling, at a mean of 3.35
per event.

Treating them as independent inflates the apparent sample and narrows every
interval — most severely in the tail buckets, since large mutually-exclusive
fields are overwhelmingly longshots. Design decision doc 004 decision 6 therefore forbids
reporting any unclustered interval.

Two estimators, both clustering on the event:

**Cluster-robust standard error** (Liang–Zeger, specialised to a mean), for the
per-bucket bias and its p-value:

```
SE² = (G / (G − 1)) · Σ_g ( Σ_{i∈g} (x_i − x̄) )² / n²
```

Residuals are summed *within* a cluster before squaring, so the cross-terms
that the independent-sample formula assumes away are retained. Note this does
not always widen the interval: negatively correlated siblings — which is what a
mutually-exclusive field produces — make cluster totals vary *less* than
independent draws, and the SE can legitimately shrink. Clustering is a synonym
for correct, not for wider.

**Block bootstrap**, resampling whole events with replacement, for the interval
on realized frequency. Resampling individual contracts would rebuild the
independence the clustering exists to deny — it would draw thirty copies of one
golfer and none of his field, breaking the "exactly one winner" constraint. The
bootstrap runs once for the whole table rather than per bucket, because one
event's contracts land in several buckets and a replicate that drops that event
must drop it everywhere at once; otherwise the intervals are individually
defensible and jointly incoherent. 2,000 replications, fixed seed.

The reference distribution is Student's t on G−1 degrees of freedom, not the
normal. At 29,895 events the two coincide; the segment tables run on far fewer,
and using t everywhere means the headline and the thin slices are computed
identically rather than differently at the point where it matters.

**Wilson score intervals** are computed but never reported. They assume
independent Bernoulli draws, which this dataset violates; they exist solely as
the denominator of the design effect (clustered SE ÷ naive SE), which quantifies
what pretending independence would have bought. Wilson rather than the normal
approximation because the Wald interval extends below zero at 3% and collapses
to zero width at 0% — degenerate exactly where the hypothesis lives.

### 4.2 Many hypotheses — false discovery rate

Ten buckets tested at p < 0.05 each give roughly a 40% chance of at least one
false positive under a true null, and the segmentation multiplies the count.

Significance is read off Benjamini–Hochberg-corrected **q-values** at α = 0.05.
The raw p-value is retained in the output but never carries a star.

BH rather than Bonferroni because the claim is a shape across buckets, so the
useful guarantee is "of the buckets flagged, at most 5% are expected to be
spurious" rather than "no false positive anywhere." Bonferroni at m = 10 would
demand p < 0.005 per bucket, which for a 2–3 cent effect would measure its own
conservatism.

**Correction and clustering are not substitutes.** Correction handles many
tests; clustering handles correlated observations inside one test. Doing only
the first produces carefully adjusted intervals that were too narrow to begin
with.

### 4.3 Segmentation

Every segment × bucket test enters **one** BH family. Correcting inside each
segment separately would let the number of segments grow for free.

One subtlety worth stating because it is easy to get backwards: pooling is not
uniformly stricter. BH is a step-up procedure, so a segment carrying strong
signal raises the others' ranks faster than it raises m, and a marginal test can
emerge with a *smaller* q pooled than alone. The guarantee is about the share of
false discoveries across the family, not about any individual q moving one way.

A bucket with fewer than 30 events keeps its point estimate and interval but is
**never tested** and never enters the family. Testing a cell with four events
spends α on a question the data cannot answer and makes every other test pay
for it. Such cells are marked `underpowered`, not deleted — deleting them would
hide that the category exists at all.

## 5. Logistic calibration

A second, bin-free reading, so the shape cannot be an artefact of bin edges:

```
logit P(outcome) = intercept + slope · logit(price)
```

Perfect calibration is intercept 0, slope 1. Standard errors cluster on the
event, as everywhere else.

**Reading the slope**, derived rather than remembered, because the convention
is easy to reverse:

- **slope > 1** — true probabilities *more* extreme than prices. A market
  saying 5% is really 2%; one saying 95% is really 98%. Longshots overpriced,
  favorites underpriced: the favorite-longshot bias.
- **slope < 1** — true probabilities *less* extreme. The market exaggerates and
  longshots are cheap: the reverse.

The test that matters is against **1**, not 0. Statsmodels reports p for H0
slope = 0 — "does price predict outcome at all" — which is trivially
significant and is not a calibration result. A joint Wald test of
(intercept, slope) = (0, 1) is reported alongside, since a market can miss the
ideal pair while neither coefficient misses alone.

Every price in the processed layer lies strictly inside (0, 1) — Kalshi's tick
floor is 0.001 — so the logit is defined on every row and no clipping or
winsorising is applied. This is measured, not assumed; a boundary price raises
rather than being silently clipped, because handling it would be a research
decision.

## 6. Reproducibility

`make analyze` and `make figures` regenerate every number and every figure from
`data/processed/contracts.parquet` plus `config.yaml`. The bootstrap seed is
fixed in config, so identical inputs yield identical intervals indefinitely.

No threshold is hardcoded: bucket counts, confidence level, replication count,
seed, FDR level, the clustering column, and the power floor are all config
keys. The notebook displays results and computes none of its own, so a chart
cannot disagree with the table beneath it.

Statistical primitives are unit-tested against hand computations and published
worked examples — the Wilson interval from the textbook, Benjamini–Hochberg
against its own 1995 paper, a hand-derived Brier decomposition, the Murphy
identity asserted as an exact equality at 1e-12, and synthetic datasets with
planted biases of known size and direction that the pipeline must recover.
