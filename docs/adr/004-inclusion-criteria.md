# ADR 004 - What is in the study

**Status:** Proposed (Phase 2, 2026-08-29) -- awaiting ratification

## Context

ADR 003 settled *which number* is the forecast. This one settles *which rows*
carry it. Four places already forward-reference this document
(`000-overview.md`, `models.py`, `ingest/trades.py`, ADR 003), so the criteria
have been assumed by the rest of the project without ever being written down.

The split matters for the drop accounting. `parse_raw_to_interim` drops only
what *cannot become a row* -- a non-binary settlement, an unparsable price, a
failed model invariant. This ADR governs `interim_to_processed`, where every
exclusion is a **research** choice rather than a data defect, and where an
exclusion that falls unevenly across categories is a bias rather than a
smaller sample.

All counts below are measured over the ingested corpus (145,047 unique
tickers), not estimated.

### The retention chain

| step | n | kept |
|---|---|---|
| ingested (unique tickers) | 145,047 | -- |
| binary result (drop `scalar`) | 143,143 | 98.7% |
| `close_time` in study window | 116,015 | 81.0% |
| `volume_fp > 0` | 109,087 | 94.0% |
| *projected after ~18% T-1h loss* | *~89,000* | *~82%* |

Built on 2026-09-01, against the completed T-1h pass, the chain came out at
**100,210 contracts in 29,895 events** -- ahead of the projection, which had
double-counted the volume filter:

| step | n | kept |
|---|---|---|
| ingested (unique tickers) | 145,047 | -- |
| binary result (drop `scalar`) | 143,143 | 98.7% |
| has a T-1h trade | 122,767 | 85.8% |
| `close_time` in study window | 100,210 | 81.6% |
| `volume_fp > 0` | 100,210 | 100.0% |

Decision 3 turns out to do no work of its own: **every** zero-volume market was
already gone, because a market that never traded has no trade at any horizon,
exactly as argued below. It is kept as an explicit criterion because it *is*
one, and because it would bite immediately if the price method were ever
switched to a snapshot field, which reports a number without needing a trade.

Comfortably above criterion 1's floor of 1,000 contracts. Sample size is not
the binding constraint anywhere in this ADR; **independence is.**

### Contracts are not independent observations

The finding that shapes this document. Grouping the 143,143 binary markets by
`event_ticker`:

- **43,382 distinct events** for 143,143 markets -- a mean of **3.30 markets
  per event**.
- **99.9% of markets sit in an event with at least one sibling.** Only 168
  markets are alone.
- The largest event, `KXPGATOUR-PGC26`, holds **250 markets** -- one per
  golfer, "Will <player> win the PGA Championship?" -- of which **exactly one
  resolved yes**.
- Of the 37,877 two-market events, **37,866 resolve exactly one yes**.

Siblings are logically bound in two distinct ways:

1. **Mutually exclusive alternatives.** A 250-golfer field is one underlying
   random outcome -- who won -- expressed as 250 contracts. It is not 250
   independent draws. It is *one*.
2. **Nested thresholds.** `CPICORE-23DEC-T0.4` ("above 0.4%") and
   `-T0.5` ("above 0.5%") are monotonically related by construction: if core
   inflation exceeded 0.5% it necessarily exceeded 0.4%. The outcomes cannot
   disagree in one direction.

The consequence is specific and it is not fixed by anything already planned.
Criterion 4 calls for multiple-comparison correction across buckets and
segments, which addresses testing *many hypotheses*. It does nothing about
correlated observations *within* a bucket. A Wilson interval on a bucket of
n=8,000 contracts drawn from 1,200 events is computed on an effective sample
far smaller than 8,000, and will be too narrow -- overstating significance in
exactly the tail buckets the favorite-longshot hypothesis lives in, because
large mutually-exclusive fields are overwhelmingly longshots.

### Framing is uniform, so no complement is needed

ADR 003 deferred "NO-framing normalisation" here. Measured, it is a non-issue:
across all 143,143 binary markets, titles containing `not`, `no`, `never`,
`fail`, `under`, `less`, or `without` number **zero**. Every market is stated
as an affirmative proposition -- "Will X happen?", "Above $3.098",
"Below $70,000.00", "Over 7 runs scored" -- and `yes_sub_title` names that
proposition. "Below $70,000" is itself an affirmative claim, not the negation
of a sibling. The YES leg is therefore always P(stated proposition), and no
row needs flipping.

## Decision

**The study population is every ingested market that is binary, finalised,
closed inside the study window, traded at least once, and has a price at the
primary horizon. Event structure is carried, not filtered.**

1. **`result` in {`yes`, `no`}.** Drops 1,904 `scalar` markets, which settle
   at fractional values and are a different object. Applied at parse time.
2. **The window filter is on `close_time`**, not `settlement_ts` or
   `open_time`. `close_time` is the anchor the horizon price is defined
   against (ADR 003), so it is the timestamp that decides when the forecast
   was made. The three barely differ in practice -- 81.0% / 81.1% / 81.7% --
   so this is chosen for coherence, not yield.
3. **`volume_fp > 0`.** Excludes 6,928 in-window markets that never traded.
   This is close to definitional rather than discretionary: a market with no
   trades has no trade at any horizon, and all 7,165 zero-volume markets
   measured in the T-1h pass returned null. Kalshi lists auto-generated strike
   ladders that nobody takes -- 80.5% of ingested Crypto markets are
   zero-volume -- and a contract with no trade has no price to calibrate.
4. **No volume floor above zero.** A floor of 100 or 1,000 contracts is
   tempting for "quality" and is rejected: liquidity correlates with the
   retail flow that secondary hypothesis 1 is about, so filtering on it
   would remove the observations most likely to carry the bias, and would do
   so unevenly by category. **`volume` is carried on every row and used as a
   Phase 3 segmentation axis instead**, where its effect is measured rather
   than assumed. A floored sensitivity run is a planned robustness check, not
   the primary population.
5. **No de-duplication by event, and no sampling down of large fields.** All
   250 golfers stay. Dropping siblings would delete the longshot tail, which
   is precisely what the hypothesis is about, and choosing which sibling
   survives would itself be a selection rule requiring defence.
6. **`event_ticker` is carried onto every contract row.** This is the
   mechanism that makes decision 5 safe: Phase 3 clusters standard errors on
   `event_ticker` (or block-bootstraps by event) so that correlated siblings
   inflate the interval instead of shrinking it. **Any interval computed
   without this clustering is wrong and must not be reported.**
7. **No NO-framing normalisation.** Measured above. The YES leg is the stated
   proposition on every row.
8. **De-duplication is by `ticker`, at ingest.** A market appearing under two
   series is priced once. Zero duplicates observed, so this is an assertion
   to keep true rather than a filter doing work.
9. **`status` is asserted, not filtered.** All 143,143 binary markets are
   `finalized`. A future non-finalised row is a bug and should raise.

Every exclusion above is counted by reason **and broken down by category** in
the drop log, per the existing `DropLog` contract.

## Consequences

**Gained**

- A population defined by measurement, with a stated reason and a count for
  every row not in it.
- The independence problem is caught before the analysis rather than after a
  reviewer asks why 8,000 golfer contracts counted as 8,000 observations.
- No filter in this ADR selects on liquidity, price, or outcome, so none of
  them can manufacture the bias being tested.

**Given up / limitations to state in the writeup**

- **The corpus is 96% Sports after filtering** (104,905 of 109,087; Economics
  2,768, Crypto 1,149, Politics 265). Filtering makes the skew *worse* than
  the 91% at ingest, because the window and volume filters bite hardest on
  Crypto's untraded strike ladders. The pooled calibration curve is a Sports
  curve and must be labelled as one; the per-category curves are the honest
  headline.
- **Politics is 265 contracts.** That is a reported segment, not a tested
  hypothesis. Secondary hypothesis 1 rests on the Economics/Sports contrast,
  where both sides have real n.
- **Effective sample size is much smaller than row count.** 109,087 markets in
  33,300 events. Clustered intervals will be materially wider than naive ones,
  and that is the correct outcome, not a loss of power to work around.
- **~19% of ingested markets fall outside the window** and are kept on disk
  but unused, per ADR 002's decision to filter in `clean.py` rather than at
  ingest. Widening the window later re-downloads nothing.
- **Nested threshold events are correlated but not exchangeable.** Clustering
  handles the variance; it does not make a monotone ladder into independent
  draws. If a bucket turns out to be dominated by one ladder family, that is
  worth reporting separately.

## Alternatives considered

- **Filter on `settlement_ts`.** Rejected on coherence: settlement can lag
  close by an arbitrary settlement timer, so it does not describe when the
  forecast was made. Yield is indistinguishable (81.1% vs 81.0%).
- **A volume floor (>=100 or >=1,000).** Rejected: selects on liquidity, which
  is a hypothesised moderator of the effect. Retained as a sensitivity run.
- **Keep one market per event.** Rejected: deletes the longshot tail the study
  is about, and needs its own defensible selection rule.
- **Cap large events (e.g. keep the 20 most liquid golfers).** Rejected for
  the same reason, and it selects on liquidity twice over.
- **Weight each contract by 1/event size.** Rejected as the primary treatment:
  it changes the estimand from "per contract" to "per event" without saying
  so. Clustered standard errors keep the estimand and fix the variance, which
  is the narrower and more defensible correction. Worth reporting as a
  robustness check.
- **Ignore the clustering and rely on multiple-comparison correction.**
  Rejected: they address different problems. Correction handles many tests;
  clustering handles correlated observations inside one test. Doing only the
  first would produce confident intervals that are simply too narrow.
