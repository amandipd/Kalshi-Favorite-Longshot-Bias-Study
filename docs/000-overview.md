# Overview

## Research question

A binary prediction-market contract settles at $1.00 if its event happens and
$0.00 if it does not. A contract trading at $0.30 is therefore the market
saying "this has a 30% chance." **Are those implied probabilities accurate?**
Pool every contract that traded near 30c, check how often those events
actually occurred, and compare the realised frequency to 0.30.

## Hypothesis

The **favorite-longshot bias**: implied probabilities are not merely noisy but
*systematically* wrong at the tails.

- **Longshots** (roughly 5-20c) win **less** often than their price implies --
  they are overpriced.
- **Favorites** (roughly 80-95c) win **more** often than their price implies --
  they are underpriced.

Stated as a null to be rejected: for every price bucket `b`, the realised
outcome rate equals the mean implied price, `E[outcome | price in b] = E[price | b]`.
The alternative is a monotone-signed deviation -- negative at the low end,
positive at the high end.

Two secondary hypotheses, tested by segmentation:

1. The bias is stronger in categories with more retail/entertainment flow
   (Sports, Crypto) than in categories with informed flow (Economics).
2. The bias widens with time-to-resolution, where uncertainty is highest.

## Success criteria

The project succeeds if it produces claims that survive cross-examination, not
if it finds a large effect. Specifically:

1. **A dataset that can be defended.** >= 1,000 resolved binary contracts,
   built by deterministic transforms from immutable raw API responses, with
   every dropped row logged and a stated reason.
2. **A calibration curve with uncertainty.** Realised frequency vs. implied
   price by bucket, each point carrying a Wilson interval, so "off by 3 points"
   is separable from "off by 3 points, and here is whether that is noise."
3. **A signed, quantified bias estimate.** A Brier score with its
   reliability / resolution / uncertainty decomposition, plus a logistic
   calibration fit whose slope and intercept are reported with standard errors.
   A slope < 1 is the compact statement of favorite-longshot bias.
4. **An honest significance claim.** Multiple-comparison correction applied
   across buckets and segments; a stated result even if the answer is "the
   deviation is not distinguishable from noise at this sample size."
5. **A falsifiable strategy.** A Kelly-sized backtest that only uses
   information available before resolution, charges realistic fees and
   spreads, and is run against a shuffled-outcome control -- the control must
   show no edge, or the pipeline itself is leaking.
6. **Reproducibility.** `make all` reconstructs every number and figure in the
   report from raw data on a clean checkout.

A negative result -- Kalshi is well calibrated over this window -- is a
success under these criteria. What is not acceptable is an unfalsifiable one.

## Scope

- **Venue:** Kalshi (primary). Polymarket is a stretch second venue; the
  ingestion layer is venue-generic so adding it does not reshape the pipeline.
- **Window:** 2025-12-01 to 2026-06-06. Chosen empirically -- see
  `docs/journal.md` (2026-08-05) and the `date_range` comment in `config.yaml`.
- **Universe:** the top 20 series by traded volume in each of Politics,
  Economics, Sports and Crypto, excluding sub-daily recurring series.
  See `docs/adr/004-inclusion-criteria.md` (Phase 2) for the full criteria.
