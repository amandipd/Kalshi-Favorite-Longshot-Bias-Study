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
