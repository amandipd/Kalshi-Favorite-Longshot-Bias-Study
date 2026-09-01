# Journal

A running, plain-language log of what got done each session. One entry per
date, added to as the project progresses.

## 2026-08-05

Today I worked on figuring out what date range to actually use for pulling data from Kalshi, instead of just guessing a round number like "last 12 months." I wrote a script (`test.py`) that talks to Kalshi's public API directly. First it checked where Kalshi's "live vs. historical" data cutoff sits, since older settled markets live in a different set of endpoints. Then, instead of trying to pull every single market in every category (Politics alone has over 2,000 separate market series, which would've taken hours), I only looked at the 20 highest-trading-volume series per category (Politics, Economics, Sports, Crypto) to get a fast, representative sample. I also had to filter out markets that settle every 15 minutes or every hour (like short-term Bitcoin price bets), since those pile up thousands of "settlements" a month and would make a category look way more active than it really is — I actually got this wrong on the first attempt (accidentally excluded a bunch of legitimate Sports markets because my filter confused "First Half" with "hourly"), then fixed it using Kalshi's real frequency label instead of guessing from the market name. After counting how many markets settled each month per category, I charted it to see the pattern visually. The chart shows Economics and Sports both had real, steady activity starting around December 2025 through June 2026, Crypto had some too, but Politics stayed thin no matter what window I picked — likely because political markets tend to be big one-off events rather than steady recurring ones. Based on this, my current leading pick for the project's date range is December 2025 through June 6, 2026, but I haven't locked that into `config.yaml` yet.

## 2026-08-28

Today I finished Phase 1: I ran the real data pull, built a health check for it, and wrote up the design decisions behind the ingestion layer.

The pull itself took about twenty minutes and brought back **145,047 settled markets across 772 pages** — the milestone only asked for 1,000, so there's no shortage of data. It walked the top 20 highest-volume series in each of Politics, Economics, Sports and Crypto, and nothing had to be re-fetched or restarted, which was the whole point of building it to be resumable.

Then I wrote `src/ingest/summary.py`, which reads back everything sitting in `data/raw/` and prints what's actually there — page counts, market counts, the date range covered, the split by category, and a short list of anomalies. The idea is to not trust the run's own log: the log tells you what one run *did*, and this tells you what's on disk after every run that ever touched it. It surfaced four things worth knowing before I start cleaning:

- **Sports is 91% of the sample** (132,456 of 145,047 markets). Politics is 433. That lopsidedness is a real problem for the headline result — if I just pool everything and compute one calibration curve, I'm publishing a chart about Sports and calling it "prediction markets." The Phase 3 segmentation was already planned, but now it's load-bearing rather than a nice-to-have, and I'll need to say plainly in the writeup that the pooled number is Sports-weighted.
- **1,904 markets came back with `result: "scalar"`** rather than yes/no. Those aren't binary contracts at all, so they have to be dropped in Phase 2. Good thing to have caught now — if I'd assumed every result was yes-or-no, they'd have quietly become zeros.
- **Settlements go back to 2021-07-30**, well before my December 2025 start date. That's on purpose: the pull stops walking a series once it's gone past the window, but the page that trips that check gets written whole rather than trimmed, so some older markets ride along. `clean.py` filters the window, not the ingestion layer, which means I can widen the study window later without re-downloading anything.
- **Zero duplicate tickers.** I'd expected some overlap between series and there was none.

One thing moved underneath me: Kalshi's live-vs-historical cutoff was 2026-06-06 when I explored this on Aug 5, and it's 2026-06-29 now. Kalshi keeps migrating recent data across that line, so the historical endpoints now reach about three weeks further forward than when I picked my date range. I left the config window alone — I'd rather have one fixed, defensible window than quietly extend it every time I re-run — but every run saves the cutoff it saw to `_cutoff.json`, so there's a record of what "everything available" meant on the day of each pull.

Finally, the documentation. `docs/000-overview.md` had been a TBD stub since day one and now states the actual research question, the hypothesis in a falsifiable form, and six concrete success criteria — including that a *negative* result (Kalshi turns out to be well calibrated) still counts as success, as long as it's measured honestly. I also wrote the two architecture decision records: `001-storage-layers.md` on why raw JSON is kept immutable and separate from the parsed and cleaned layers, and `002-ingestion-idempotency.md` on why resumption keys off file paths rather than a checkpoint file, why cached pages still get read (each page carries the next one's cursor), and why writes go through a temp file and a rename — a run killed mid-write would otherwise leave a truncated page that the next run happily treats as finished. And `docs/data-sources.md` documents every endpoint, the live-vs-historical trap, the rate limits, and the fact that every price Kalshi returns is a *string*.

Writing the data-sources doc turned up something that decides the next big call for me. I'd assumed the Phase 2 price choice was a free judgment between last trade, bid-ask mid, and closing price. It isn't: on a settled market the order book is gone, and Kalshi reports `yes_bid: 0.0000` / `yes_ask: 1.0000` — a maximally wide quote that isn't a price at all. A mid computed from that is 0.50 for every single contract. The historical endpoint gives one post-settlement snapshot rather than a time series, so last traded price is the only usable implied probability available. That's a constraint to document honestly in ADR 003, not a preference to defend — and it's a real limitation of the study, since a last trade in a thin market can be hours stale.

Next up is the Phase 1 gate, then Phase 2: the price definition ADR, then parsing raw JSON into the interim dataset.

*(later the same day — Phase 2 begins)*

Phase 2 started with what was supposed to be a quick decision — pick which price counts as "the market's implied probability" — and turned into the most important finding of the project so far.

I'd assumed this was a free choice between three options the proposal listed: last traded price, the midpoint of the bid and ask, or a volume-weighted average. Before writing the ADR I decided to actually measure all three against the 145,047 markets I'd pulled, rather than reasoning about them from the docs. Two of the three died immediately.

**The bid-ask midpoint doesn't exist.** On a settled market the order book is gone — 54.9% of markets report a bid of $0.00 and an ask of $1.00, which isn't a wide spread so much as the absence of any quote at all. The median spread across the whole corpus is a full dollar, and open interest is exactly zero on all 145,047 records. A midpoint computed from that returns 50¢ for most of the dataset regardless of what anyone ever believed.

**The last traded price is the answer in disguise.** This is the one that would have quietly ruined the project. I cross-tabulated the settled snapshot's last price against how each market actually resolved, and **92.5% of binary markets have a last price already pinned to the correct settlement value** — under 1¢ when the answer was no, over 99¢ when it was yes. Only 6.8% sit anywhere in between. Had I built a calibration curve on that number, I'd have gotten a beautiful near-perfect result and it would have meant nothing, because the "forecast" and the outcome would have been the same variable. Nothing about the field looks wrong: it's well named, present on every record, correctly typed. You only catch it by checking.

My first instinct was that this was a contamination bug — that the snapshot was picking up trades from after settlement. It isn't. I checked the last trade before the market's official *close* time and it's 90% pinned too. The price is real; the market has simply stopped being uncertain. Kalshi's universe is mostly short-lived recurring contracts — median lifetime from open to close is about 24 hours — and whatever uncertainty they have burns off in the final minutes. So the problem was never *which* price but *when*: a price sampled at the moment of resolution isn't a forecast no matter which field it lives in.

The fix came from a different endpoint. `/historical/trades` accepts a ticker and a `max_ts`, so I can ask for the last trade at or before any moment I choose — a price from while the market was still genuinely guessing. I swept the horizon over 250 sampled markets and it's a real trade-off in both directions:

| horizon | retention | still pinned | usable |
|---|---|---|---|
| at close | 99.6% | 90.0% | 10.0% |
| **1h before** | **89.2%** | **17.5%** | **73.6%** |
| 6h before | 74.8% | 4.8% | 71.2% |
| 24h before | 30.8% | 7.8% | 28.4% |
| 72h before | 6.8% | 29.4% | 4.8% |

Sample too late and the price is just the answer. Sample too early and the market didn't exist yet — a contract with a 24-hour life has no 24-hour-out price by construction, so the survivors at that horizon are a sample selected on longevity, which correlates with category and event type. One hour before close is the sweet spot, and its price distribution finally looks like a calibration curve instead of two spikes: the 0–10¢ bucket resolves yes 0% of the time, the 40–50¢ bucket 63%, the 90–100¢ bucket 95%.

Interestingly the 6-hour horizon looks *flatter* than the 1-hour one — longshots resolving above their price, favorites below, which is the favorite-longshot signature — but there are only 11 to 36 markets per bucket there, so that's a hint to check later, not a finding.

All of this is written up in `docs/adr/003-implied-price-definition.md`, including the options I rejected and why. The one I want to remember rejecting is the lazy escape hatch: just study the 6.8% of markets whose snapshot price *isn't* pinned, and skip the extra data pull. That's tempting and it's badly wrong — that subsample is selected precisely on the market still being uncertain at close, which means conditioning on something downstream of the outcome I'm trying to predict.

So Phase 2 grew a second ingestion pass that the proposal never anticipated. `src/ingest/trades.py` walks every settled binary market and fetches its price at each configured horizon, writing one JSONL file per series per horizon (about 80 files, rather than 143,000 tiny ones that would make OneDrive miserable). It resumes the same way the first pass does — it reads back which tickers are already in the file — and it records an explicit null when a market had no trade that early, so a re-run doesn't ask again and the cleaning log can *count* those exclusions rather than infer them from silence. The T-1h run is going now at about 11 markets/second, roughly 3.5 hours, and should land around 50 MB.

I also wrote the first half of `src/clean.py`: `compute_implied_price` supporting all four methods (including the two I proved unusable — kept deliberately, so the writeup can *show* the degenerate curve rather than just assert it), and `parse_raw_to_interim`, which turns raw JSON into typed `Contract` rows. Everything it drops is counted against a named reason and broken down by category, because an exclusion that falls unevenly across categories is a bias rather than just a smaller sample. 82 tests pass.

Still to do in this milestone: build the interim dataset once the pull finishes, then the exploration notebook.

## 2026-09-01

The T-1h pull finished — all 143,143 binary markets have a price from an hour
before close — so today was about turning that into the two dataset layers the
analysis reads, and the first look at what's in them.

Before parsing anything I closed a gap between ADR 004 and the code. I wrote
that ADR after `clean.py`, and its decision 6 says `event_ticker` has to be
carried on every row so Phase 3 can cluster standard errors on it — "any
interval computed without this clustering is wrong." The `Contract` model
didn't have the field. Parsing 145k rows and then discovering they can't be
clustered would have meant doing it twice, so the field went in first, along
with two assertions the ADR calls for: a market whose `status` isn't
`finalized` now raises rather than being dropped (an unsettled market's
`result` isn't an outcome, so that's a bug in ingestion, not a row to skip),
and a market with no `event_ticker` is dropped against a named reason rather
than falling back to an empty string — a row that can't be clustered is worse
than a missing one, because a shared empty key pools unrelated markets into a
single fake event. I checked both against the corpus before writing the code:
zero markets are missing an event ticker and all 145,047 are `finalized`, which
is what ADR 004 claimed and is now enforced rather than assumed.

Then I built the interim layer, and the horizon fix from Phase 2 held up:
**pinned prices fell from 92.5% to 1.1%**. That single number is the whole
justification for the second ingestion pass. The settled snapshot's last price
was the answer wearing a forecast's clothes; the T-1h price is an actual
forecast. 122,767 of 145,047 markets survive parsing — 20,376 had no trade an
hour before close (5,860 of them Crypto strike ladders nobody ever took) and
1,904 were scalar settlements.

Next I wrote `interim_to_processed`, the step ADR 004 specifies. What's
interesting about it is how little it does: two filters, a close-time window
and a non-zero volume, plus two assertions. Everything else in that ADR is a
filter I decided *not* to write — no volume floor, no de-duplication by event,
no NO-framing flip — and each of those absences is as much a decision as the
filters that run, so the function says so in its docstring. Someone reading it
later shouldn't have to wonder whether the volume floor was omitted on purpose.

The result: **100,210 contracts across 29,895 events**, closing between
2025-12-01 and 2026-06-06. That's ahead of ADR 004's ~89,000 projection,
because the projection had double-counted the volume filter. In fact the volume
filter now removes **nothing at all** — every zero-volume market was already
gone at the parse step, since a market that never traded has no trade at any
horizon either. That's the ADR's own argument for the criterion turning out to
be so completely true that the criterion is redundant. I kept it anyway and
recorded why: it costs nothing, and it would bite immediately if the price
method were ever switched back to a snapshot field, which happily reports a
number for a market that never traded.

Then the first honest look at a calibration curve:

| price bucket | n | mean price | resolved yes | gap |
|---|---|---|---|---|
| 0.0–0.1 | 24,117 | 0.037 | 0.032 | −0.004 |
| 0.1–0.2 | 10,037 | 0.152 | 0.125 | −0.027 |
| 0.2–0.3 | 7,897 | 0.254 | 0.226 | −0.027 |
| 0.3–0.4 | 7,070 | 0.355 | 0.325 | −0.029 |
| 0.4–0.5 | 6,634 | 0.455 | 0.431 | −0.024 |
| 0.5–0.6 | 6,384 | 0.555 | 0.573 | +0.018 |
| 0.6–0.7 | 6,747 | 0.655 | 0.665 | +0.010 |
| 0.7–0.8 | 6,813 | 0.757 | 0.769 | +0.013 |
| 0.8–0.9 | 7,675 | 0.857 | 0.869 | +0.012 |
| 0.9–1.0 | 16,836 | 0.969 | 0.961 | −0.008 |

That is the favorite–longshot signature, and it flips sign cleanly at 0.50:
everything priced below it resolves yes *less* often than its price says,
everything above resolves yes *more* often. It is the shape the hypothesis
predicted.

I want to be careful about how much that's worth right now, because four things
stand between this table and a finding. It's **96% Sports** — this is a chart
about sports betting that happens to include some other markets. The intervals
aren't computed yet, and when they are they have to be **clustered on
`event_ticker`**, which will widen them; the encouraging part is that the
buckets are less concentrated than I feared (the 0.0–0.1 bucket holds 24,117
contracts but 10,839 distinct events, so about 2.2 correlated rows per event
rather than the 250-golfer worst case). There's **no multiple-comparison
correction** yet. And the top bucket runs the *other* way, −0.008 where the
pattern predicts positive, which may just be the ceiling at 1.0 or may be
something real about heavy favorites.

The one that genuinely surprised me is Economics, the category with real
independence from the Sports bulk: its 0.25–0.50 bucket is priced at 0.370 and
resolves yes 21.1% of the time — a 16-point gap, far larger than anything in
the pooled table. n is only 123 contracts across a handful of events, so it is
a lead and not a result. But if the effect is genuinely bigger away from
sports, that inverts the assumption I'd been carrying, which was that Sports
would show the strongest retail bias.

114 tests pass. Next: the exploration notebook, and then Phase 3's calibration
machinery — where the clustered intervals get built, and where that Economics
gap either survives contact with an honest confidence interval or doesn't.

---

Later the same day: Phase 3's first milestone, which is where the table above
either becomes a finding or stops being one.

The whole milestone is about the *interval*, not the estimate. The point
estimates were never in doubt — bucket the prices, average the outcomes,
subtract — and I already had them this morning. What I did not have was any
right to call the gaps real, because ADR 004 says plainly that an interval
computed without clustering on `event_ticker` "is wrong and must not be
reported," and until today nothing computed one.

So `src/analysis/statistics.py` is a small estimator library and the two things
in it that matter are both about correlated siblings. The cluster-robust
standard error sums residuals *within* an event before squaring them, which
is the entire difference from the textbook formula: squaring each residual
alone is what silently assumes the cross-terms vanish, and inside a golfer
field they emphatically do not. The block bootstrap resamples whole events
rather than contracts, for the same reason from the other direction — drawing
contracts independently would happily produce a replicate with thirty copies
of one golfer and none of his field, quietly rebuilding the independence the
clustering exists to deny. And it runs **once for the whole table**, because a
single event's contracts land in several price buckets and a replicate that
drops that event has to drop it from all of them at once. Ten separately
bootstrapped buckets would each be defensible and the table would be
incoherent.

The thing I got wrong on the first pass, and want to remember: I had assumed
clustering means *wider*. It doesn't — it means *correct*. A mutually
exclusive field is negatively correlated inside the cluster (exactly one
winner, 249 losers), and the cluster totals then vary **less** than
independent draws would, so the clustered SE can legitimately shrink. There's
now a test asserting exactly that on a miniature golfer field, sitting next to
its opposite. If I'd written the tests expecting one direction I'd have
"fixed" the estimator until it was wrong.

The Murphy decomposition needed similar honesty. `reliability − resolution +
uncertainty = brier` holds *exactly* only when every forecast inside a bucket
is the same number — true for a forecaster who only ever says 10%, 20%, false
here where prices vary continuously inside each decile. The proposal asked for
a test that the identity holds "≈", and an approximate assertion is precisely
the kind that passes with a term subtly wrong. So `brier_decomposition` now
returns both scores: `binned_brier`, which the identity closes on to machine
precision and which the test asserts at 1e-12, and the real `brier` on unbinned
prices, with their difference reported as `binning_residual`. That residual is
a diagnostic rather than an error — it measures within-bucket price variation
the buckets can't see. Here it's −0.00075 against a Brier of 0.1213, so 0.6%:
deciles describe these prices well.

Then the results, from `make analyze`:

| bucket | n | events | price | actual | bias | 95% CI (clustered) | deff | q |
|---|---|---|---|---|---|---|---|---|
| 0.00–0.10 | 22,993 | 10,280 | 0.0335 | 0.0296 | −0.0040 | [0.0263, 0.0331] | 1.45 | 0.016 * |
| 0.10–0.20 | 10,198 | 7,582 | 0.1421 | 0.1175 | −0.0246 | [0.1105, 0.1253] | 1.14 | 1.5e−10 *** |
| 0.20–0.30 | 8,119 | 7,013 | 0.2430 | 0.2163 | −0.0268 | [0.2069, 0.2257] | 1.08 | 2.5e−07 *** |
| 0.30–0.40 | 7,096 | 6,521 | 0.3444 | 0.3145 | −0.0298 | [0.3037, 0.3258] | 1.03 | 5.3e−07 *** |
| 0.40–0.50 | 6,682 | 6,244 | 0.4449 | 0.4202 | −0.0247 | [0.4082, 0.4326] | 1.03 | 1.4e−04 *** |
| 0.50–0.60 | 6,394 | 5,613 | 0.5443 | 0.5554 | +0.0111 | [0.5438, 0.5670] | 0.96 | 0.069 |
| 0.60–0.70 | 6,766 | 6,389 | 0.6457 | 0.6543 | +0.0086 | [0.6426, 0.6659] | 1.03 | 0.147 |
| 0.70–0.80 | 6,660 | 6,211 | 0.7461 | 0.7622 | +0.0161 | [0.7512, 0.7721] | 1.04 | 0.004 ** |
| 0.80–0.90 | 7,568 | 6,745 | 0.8462 | 0.8622 | +0.0160 | [0.8537, 0.8706] | 1.07 | 2.6e−04 *** |
| 0.90–1.00 | 17,734 | 10,635 | 0.9651 | 0.9566 | −0.0086 | [0.9529, 0.9602] | 1.20 | 5.7e−06 *** |

Brier 0.1213 = reliability 0.00029 − resolution 0.1258 + uncertainty 0.2476.

**The clustering cost what ADR 004 predicted, exactly where it predicted.**
The design effect is ~1.03 through the middle and rises in both tails — 1.45
in the 0–10¢ bucket, 1.20 in the 90¢–100¢ bucket — which are the two buckets
holding the big mutually-exclusive fields. In the bottom bucket the corrected
q moves from 5e−04 (naive) to 1.6e−02 (clustered): a thirtyfold weaker claim,
still significant. No bucket's verdict actually flips in the pooled table, and
I want to be careful not to read that as "the clustering didn't matter." It
matters most in the longshot bucket the hypothesis is *about*, and the
per-category segments have one to two orders of magnitude fewer events to
spend, so this is the pooled table getting away with it, not the correction
being unnecessary.

Two things I didn't expect. First, **reliability is 0.00029** — three
ten-thousandths. Decomposed, almost all of the Brier score is the base rate's
own uncertainty (0.2476) offset by genuine discrimination (0.1258), and
essentially none of it is miscalibration. The favorite-longshot bias here is
real, it is significant in eight of ten buckets, and it is *small*: two to
three points. That's a more honest headline than the shape of the curve
suggests on its own, and it moves the interesting question to Phase 4 — a
3-point edge is not obviously a tradeable one once fees and the bid-ask
spread are paid.

Second, the top bucket is still going the wrong way. −0.0086 where the
favorite-longshot story predicts positive, and now with a q of 5.7e−06 rather
than a shrug. It's no longer something I can wave off as the ceiling at 1.0,
because the clustering had its largest effect there and it survived. Heavy
favorites at 96.5¢ resolving yes 95.7% of the time is a *specific* claim and I
don't have an explanation for it yet.

One discrepancy worth flagging against this morning's table: bucket counts
moved (the bottom bucket from 24,117 to 22,993) because I fixed the bin edges
to be left-closed. Pandas' default is right-closed, which put the 1,124
contracts priced at exactly 0.10 in the *bottom* bucket. Left-closed matches
how the bands are spoken about — 10¢ starts the 10–20¢ band — and needs no
special case at zero. Recorded in ADR 005 so nobody later has to wonder why
two tables of the same data disagree.

All of it is in `docs/adr/005-bucketing-and-tests.md`, including why Wilson
intervals are computed but never reported (they're the denominator of the
design effect and nothing else), and why BH rather than Bonferroni. `make
analyze` regenerates the table and writes `reports/calibration_table.csv`, so
there is exactly one place the headline numbers come from. 172 tests pass, 58
of them new.

Still open for Milestone 2: the logistic calibration (slope vs. the ideal 1),
segmentation by category and time-to-resolution — which is where this morning's
Economics lead gets its honest interval and where the 96%-Sports caveat either
narrows or stays — and the figures.

---

Milestone 2, same day. The segmentation is where the 96%-Sports caveat stops
being a caveat and becomes an answer, and it delivered — including one result
that inverts what I'd been assuming for a month.

Three things needed deciding before any code, and all three came from looking
at the data rather than the proposal.

**The logit is defined everywhere, which I expected to have to argue about.**
Kalshi's tick floor is 0.001, so no price sits at exactly 0 or 1 and the
logistic regression needs no clipping, winsorising, or dropping. I'd budgeted
an ADR paragraph for a judgment call that turned out not to exist. It still
raises on a boundary price rather than clipping silently, because if one ever
appears, how to handle it is a research decision and not a numerical
convenience.

**Category sizes are brutal once you count events instead of contracts.**
Sports has 29,471 events. Economics has 2,743 contracts but **186 events**.
Politics has 265 contracts and **16 events**. Clustered inference is governed
by events, so Economics has roughly a fifteenth of the sample its contract
count advertises. That's what the power floor is for: a bucket under 30 events
keeps its estimate and interval but is never tested and never enters the
correction family. Politics comes back untestable in every single cell, which
is the honest answer rather than a gap to paper over.

**"Time to resolution" doesn't exist in this dataset.** The proposal asks for
`bias_by_time_to_resolution`. But ADR 003 prices every market at exactly one
hour before close, so time-from-forecast-to-outcome is ~1 hour for all 100,210
rows *by construction*. A function with that name would be measuring settlement
lag while claiming to measure forecast horizon. What actually varies is market
**lifetime** — how long it traded before I priced it — median 25h, quartiles
15.6 / 25.0 / 45.1, tail past two years. That supports the question the
proposal was reaching for, and it turned out to be the most interesting split
in the study.

The mistake I made and had to back out: I wrote a test asserting that pooling
segments into one Benjamini-Hochberg family always produces q-values at least
as large as correcting each segment alone — "more tests, higher bar." It
failed, and it was the test that was wrong, not the code. **BH is a step-up
procedure**, so a segment full of strong signal lifts the other tests' ranks
faster than it lifts m, and a marginal test can emerge with a *smaller* q
pooled than alone. I checked it directly: five p-values at 1e-8 pull a
neighbouring 0.02 from q=0.10 down to q=0.033. The FDR guarantee is about the
share of false discoveries across the family, not about any individual q moving
one way. I'd written the module docstring and a config comment both asserting
the wrong version, so those got corrected too. Worth remembering that I'd have
happily shipped that sentence in the writeup.

Results.

**The bucket-free check agrees with the bucketed one.** Slope 1.0442, 95% CI
[1.0229, 1.0656], p=5.0e-05 against the ideal of 1; intercept −0.0542. Slope
above 1 means true probabilities are more extreme than prices — longshots
overpriced, favorites underpriced. That's the favorite-longshot direction, and
it means the shape in the decile table isn't an artefact of where I put the bin
edges. I derived the direction from the fitted equation rather than trusting
memory, because it's the mirror image of the "slope < 1 means overconfident"
convention from the forecast-evaluation literature, which regresses the other
way round. Two tests now plant slope 1.3 and 0.7 so the convention can't
silently invert on me later.

**Miscalibration concentrates almost entirely in short-lived markets.** The
shortest quartile (under 15.6h) is significantly miscalibrated in four of five
price bands, including a −5.5 cent bias at 20–40¢, the largest anywhere in the
pooled data. The two middle quartiles are essentially indistinguishable from
calibrated. That's consistent with an information-aggregation story — less time
open, less opportunity to incorporate what people know — but lifetime is
confounded with contract type, since a 15-hour market is overwhelmingly a
same-day game.

**And the Economics lead survived.** This is the one that inverts my prior. Its
20–40¢ band is priced at 28.8¢ and resolves yes 15.1% of the time — a
**13.7-point** gap, five times anything in the pooled table, and it survives
clustering *and* a family-wide correction on 85 events. Crypto shows the same
thing (−15.2 at 20–40¢) plus a large positive bias at 60–80¢. Sports, the 96%
that drives every pooled number, has the *weakest* effect of any testable
category: −2.7 cents at its worst.

So the assumption I'd carried since the proposal — that Sports, being the
retail-money category, would show the strongest bias — is backwards. The pooled
table isn't a measurement of prediction markets that happens to be mostly
sports; it's a measurement of sports that *dilutes* a much larger effect
elsewhere.

One caution I want on the record, because it would be easy to get excited here:
Economics is negative in four of five bands, including above 50¢. That is not
the favorite-longshot shape, which requires a sign flip. It looks more like a
general overpricing of YES. If it holds up it's a different effect wearing the
same clothes, and I should stop calling it favorite-longshot bias until I know
which it is.

Also built: four figures (`make figures`), generated from the same functions
`make analyze` prints so a chart can't drift from its table, and a notebook that
displays results and computes none of its own. `docs/methodology.md` and
`docs/findings.md` are written. 192 tests pass, 20 new.

Next, Phase 4: the fee model, which is where a 2–3 cent pooled edge either
becomes a strategy or doesn't. But the more interesting thread is the one this
milestone opened — whether the non-Sports effect is real, which needs more
Economics events than a six-month window gives, and whether the top-bucket
anomaly and the Economics asymmetry are the same phenomenon.
