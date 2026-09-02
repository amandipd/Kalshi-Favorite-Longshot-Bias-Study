# Limitations

Every honest study has a list like this. The point of writing it down is not
to pre-empt criticism with a disclaimer paragraph -- it's to say, precisely,
which of these findings would change under which future piece of evidence, so
a reader (including future me) knows exactly how much weight each claim can
bear.

## The single biggest one: no spread is modelled

The backtest fills every trade at the last traded price. A real order crosses
the bid-ask spread, and that cost is not in this model anywhere, because it
cannot be measured from this data: settled snapshots carry no usable order
book (54.9% quote bid $0.00 / ask $1.00, design decision doc 003). This is the same defect
that forced the two-pass ingestion in the first place, showing up again one
layer downstream.

The **breakeven slippage** -- 0.98c per contract -- is reported precisely
because of this gap. It says: whatever the true spread turns out to be, if it
costs more than a cent to cross on average, the strategy is unprofitable. A
1-2 cent half-spread is entirely plausible on markets this thin, which means
the honest reading of the backtest is "the edge, after fees, is inside the
noise floor of a cost I cannot measure" -- not "this strategy makes 1.23%."

This is a data limitation, not a modelling shortcut I chose not to take. Fixing
it needs live order-book data, which the historical endpoints do not provide.

## The out-of-sample period is short and undiversified

48 settlement days, 40,071 contracts, and -- inheriting the pooled corpus's own
composition -- effectively all Sports. Three consequences:

- The Sharpe-like ratio and max drawdown describe seven weeks of one sport-
  heavy regime, not a market cycle. Annualizing a 48-day statistic assumes the
  next twelve months look like this particular 48 days, which is an assumption,
  not a measurement.
- The out-of-sample window sits in the higher-volume second half of the
  ingestion period (design decision doc 006), so it is not a random sample of "future" Kalshi
  behaviour -- it is specifically the busier months.
- A single train/test split means the ROI is one draw, not a distribution.
  Walk-forward re-estimation (re-splitting monthly and re-running) would give
  a spread of out-of-sample ROIs instead of one number, and is the natural next
  step rather than something this milestone attempted.

## The bias itself is small, and small effects are the ones fragile to
## specification choices

Peak gross bias is 2.98 cents. Compare that to two boundary calls made
elsewhere in the pipeline that could plausibly have gone the other way and are
each worth roughly that much:

- **Bucket edges are left-closed.** design decision doc 005 documents that this alone moved
  1,124 contracts between buckets relative to a right-closed convention.
- **The pricing horizon is T-1h**, chosen because it maximizes a yield metric,
  not because it is the unique correct answer. T-6h retains more markets with
  even less pinning (design decision doc 003's own table) and has not yet been re-run through
  the full calibration and backtest pipeline. If the sign or magnitude of the
  bias moved substantially at T-6h, that would say the finding is sensitive to
  a choice inside the study's own design space, not just to real-world
  frictions like fees.

Neither of these invalidates the finding -- both were made for stated,
defensible reasons -- and the backtest's own thresholds were swept to check
whether the result depends on the exact value chosen (`src/strategy/sensitivity.py`,
`reports/figures/07_sensitivity.png`):

- **`min_net_edge`** (0.0 to 2.0c): out-of-sample ROI stays positive and the
  control keeps losing across 0.0-1.0c, and ROI actually *rises* as the bar
  is raised (1.23% -> 1.73% -> 2.40%) while the number of tradeable buckets
  falls (7 -> 6 -> 3) -- consistent with the edge being concentrated in a few
  buckets with real margin rather than manufactured by including every
  marginal one. Above 1.0c nothing clears the bar and the strategy trades
  nothing, which is the ceiling on how much margin exists, not a failure.
- **`train_fraction`** (0.4 to 0.8, moving where design decision doc 006's split falls): ROI
  ranges 1.05%-2.23% and the control falsifies at every value tried. The
  result is not balanced on the specific 0.6 chosen.
- **`kelly_fraction`** (0.1 to 1.0, an invariance check rather than a search):
  ROI is nearly flat (1.03%-1.23%), as it should be if Kelly sizing is only
  rescaling stakes rather than driving the result.

So the *qualitative* finding -- a small positive edge, a control that reliably
loses -- is not an artefact of the exact threshold values reported as
headline numbers. But "sensitive to specification" and "small" still compound
each other in a different sense: the bias itself (2.98c peak, pre-fee) is
small enough that the two boundary calls above, each worth a comparable
amount, are exactly the kind of choice that could move the *finding*, even
though they don't move the *backtest's* conclusion given the finding as
input. A 3-cent effect deserves more scepticism than a 30-cent one would.

## Non-Sports categories are underpowered, not disconfirmed

Economics shows a bias roughly five times the pooled table's peak (docs/findings.md
section 4), on 186 events. Politics returns no testable cell at all, on 16.
Both are reported and neither is claimed. The honest state is: the pooled
result is well-powered and small; the categories where the effect looks larger
do not currently have enough independent events to say whether "larger" is
real or noise dressed as a big number on a small sample. This can only be
fixed with more months of data, not with a different analysis of the same six
months.

## Selection effects in what got ingested at all

`top_n_series_per_category = 20` (config.yaml) means only the twenty
highest-volume series per category were ingested. This is deliberate --
Design decision doc 004's own language calls the long tail "near-zero-volume markets where
price reflects noise, not a market view" -- but it means every number in this
study describes Kalshi's most liquid markets, not Kalshi as a whole. A bias
found in the top 20 series per category need not hold in the long tail, and
this study makes no claim that it does.

## The strategy rule is one reasonable design, not the only one

Buckets are traded when significant AND fee-positive in-sample; sizing is
half-Kelly capped by a daily portfolio budget (design decision doc 007). Two things worth
naming as choices rather than facts:

- **The daily budget binds on 99.9% of trades.** Kelly sets the relative
  weights across positions; the portfolio constraint sets the overall level.
  A strategy free to lever beyond 1x bankroll daily (which no real account
  can do) would size differently. Reporting "Kelly-sized" without this caveat
  would overstate how much of the sizing decision Kelly is actually making.
- **Positions are held to settlement.** No early exit is modelled, which is
  conservative on fees (one trade instead of two) but means the strategy is
  fully exposed to each position's full variance until resolution, with no
  ability to cut a bad-looking position early.

## What would change these findings

Concretely, in order of how much it would move the headline claim:

1. **Live order-book data for even a sample of markets**, replacing the
   breakeven-slippage estimate with a measured cost. This is the single
   highest-value follow-up.
2. **A second venue (Polymarket)**, which was the proposal's stretch goal and
   was not attempted. Confirming the same sign and rough magnitude off-Kalshi
   would be the strongest evidence this is a market phenomenon rather than a
   Kalshi-specific one.
3. **More months of data**, primarily to power the Economics/Crypto segments
   past "thin" and to give the backtest more than one out-of-sample draw via
   walk-forward re-estimation.
4. **The T-6h and T-24h horizons run through calibration and the backtest**,
   not just the pinning-rate table in design decision doc 003, to show the result is not an
   artefact of the specific T-1h choice.
