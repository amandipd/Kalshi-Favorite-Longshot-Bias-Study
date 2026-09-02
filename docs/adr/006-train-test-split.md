# Design decision doc 006 - Estimating on one period and trading on a later one

**Status:** Proposed (Phase 4, 2026-09-01) -- awaiting ratification

## Context

Phase 3 measured a favorite-longshot bias on all 100,210 contracts. Phase 4
asks whether it can be traded. Those are different questions, and the second
one is destroyed by using the answer to the first.

If the bias is estimated and traded on the same data, the backtest reports the
in-sample fit rather than a forecast. It would look excellent and mean nothing:
the strategy would "know" that the 30-40c bucket underperformed by 2.98c
because it had already seen those very contracts settle. Every parameter --
which buckets to trade, which side, how big -- would be fitted to outcomes it
then claims to predict.

## Decision

### 1. Split on time, never at random

Two independent reasons, either sufficient.

**Siblings.** A random split scatters an event's contracts across both sides.
The same PGA field, the same CPI ladder -- and because exactly one contract in
a mutually exclusive field resolves yes, seeing 249 of them in training tells
you the 250th. The "out-of-sample" set would contain outcomes logically
determined by the training set. This is the same non-independence design decision doc 004
built the clustering for, reappearing as leakage.

**Direction of time.** A random split lets the strategy learn from June to
trade January. Nobody can do that. A time split is the only one that
corresponds to a decision a trader could actually have made.

A random split here would produce a better-looking and meaningless number,
which is the most dangerous kind of result.

### 2. The boundary is a contract-count quantile, not a calendar midpoint

`train_fraction = 0.6` of contracts by close time, giving a split at
**2026-04-20**: 60,114 training contracts and 40,071 tradeable ones.

Volume ramps 3.5x across the window -- 7,561 contracts closed in December
against 26,850 in May -- so splitting the calendar in half would leave the
estimation period with roughly a quarter of the data and the least reliable
bias estimates precisely where they matter most.

Choosing the boundary from close dates and counts uses **no price and no
outcome**, so it cannot leak the label. It is a decision about sample size, not
about results.

### 3. The gap between "settled" and "priced" is dropped, not straddled

Training needs an outcome, known at `settle_ts`. Trading happens at
`close_ts - 1h`. Two conditions, and they do not partition the corpus:

- **train**: `settle_ts < split`
- **test**: `close_ts - 1h >= split`

A market priced before the split but settling after it satisfies neither.
Trading it would mean holding a position through the moment the training data
was assembled. **25 contracts** fall in this gap and are excluded from both
sides; `Split.excluded` reports the count so a reader can check the split was
honest rather than take it on faith.

### 4. The trading rule is derived from the training set, never chosen

This is the decision most easily got wrong while looking careful. A split is
worthless if the *rule* it evaluates was picked after seeing the full-sample
answer. Choosing "trade below 20c" by eye would leak the finding through a
parameter, leaving the split technically honest and substantively decorative.

So `bucket_rules` derives everything from the training frame: which buckets are
statistically real (significant under the same clustered, corrected machinery
Phase 3 reports), which side to take (the sign of the training bias), and
whether the edge survives the fee at that price. Nothing about the trading
period enters.

## Consequences

- Split at 2026-04-20. Train 60,114, test 40,071, 25 excluded in the gap.
- The out-of-sample period is **48 settlement days**, which is short. Any
  Sharpe or drawdown from it describes seven weeks, and `docs/limitations.md`
  says so rather than annualising it into a claim.
- The training set is drawn from a lower-volume period than the trading set,
  so the estimated edges come from a slightly different market regime. This is
  unavoidable in a time split and is the honest cost of not using a random one.
- Out-of-sample is still 96% Sports, exactly like the pooled study. The
  backtest tests tradeability, not generality.

## Alternatives considered

- **Random split.** Rejected: leaks through siblings and reverses time. See
  decision 1.
- **Split by event rather than time** (all of an event's contracts on one
  side). Rejected as insufficient: it fixes the sibling leak but still lets the
  strategy learn from the future.
- **k-fold cross-validation.** Rejected for the same reason, and it would also
  make "out-of-sample" a average over folds that each individually violate
  causality.
- **Walk-forward re-estimation** (refit the bias monthly and trade the next
  month). Genuinely better -- it uses the data more efficiently and adapts to
  regime -- and rejected only for scope. It is the first thing to build if this
  is extended, and it would let the estimate track the volume ramp instead of
  being fixed at a single early snapshot.
