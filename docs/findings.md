# Findings

**Phase 3, 2026-09-01.** Every number here comes from `make analyze` over
`data/processed/contracts.parquet` (100,210 settled Kalshi contracts across
29,895 events, closing 2025-12-01 to 2026-06-06, each priced at the last trade
one hour before its close). Method in `docs/methodology.md`; the decisions
behind it in `docs/adr/`.

---

## In one paragraph

Kalshi's prices are good probabilities. Almost none of their forecast error is
miscalibration — the market's stated percentages come true at close to the rate
they claim. On top of that there is a real, statistically solid
favorite-longshot bias in the direction the literature predicts: contracts
priced below 50c happen less often than their price implies, contracts above
50c happen more often. It is **small**, two to three cents, and it is
concentrated in short-lived markets. Two results complicate the story: heavy
favorites run the *wrong* way, and the bias outside Sports looks four to five
times larger — on a sample small enough that this is a lead rather than a
result.

---

## 1. The market is well calibrated

```
Brier score  0.1213
  reliability   0.00029   calibration error          (lower is better)
- resolution    0.1258    discrimination             (higher is better)
+ uncertainty   0.2476    base rate 0.4512           (irreducible)
```

Reliability is three ten-thousandths. Essentially the entire Brier score is the
base rate's own variance offset by genuine discrimination; the miscalibration
term is a rounding error next to both.

This is worth stating first because the rest of this document is about a bias,
and a reader could otherwise come away thinking Kalshi prices are bad. They are
not. The bias is a small distortion on top of a well-calibrated market.

The binning residual is -0.00075, 0.6% of the score, so decile buckets describe
these prices well and none of the decomposition is an artefact of the bins.

## 2. The favorite-longshot bias is present, in the predicted shape

| price band | n | events | priced | happened | bias | 95% CI (clustered) | q |
|---|---|---|---|---|---|---|---|
| 0.0–0.1 | 22,993 | 10,280 | 0.0335 | 0.0296 | −0.0040 | [0.0263, 0.0331] | 0.016 \* |
| 0.1–0.2 | 10,198 | 7,582 | 0.1421 | 0.1175 | −0.0246 | [0.1105, 0.1253] | 1.5e−10 \*\*\* |
| 0.2–0.3 | 8,119 | 7,013 | 0.2430 | 0.2163 | −0.0268 | [0.2069, 0.2257] | 2.5e−07 \*\*\* |
| 0.3–0.4 | 7,096 | 6,521 | 0.3444 | 0.3145 | **−0.0298** | [0.3037, 0.3258] | 5.3e−07 \*\*\* |
| 0.4–0.5 | 6,682 | 6,244 | 0.4449 | 0.4202 | −0.0247 | [0.4082, 0.4326] | 1.4e−04 \*\*\* |
| 0.5–0.6 | 6,394 | 5,613 | 0.5443 | 0.5554 | +0.0111 | [0.5438, 0.5670] | 0.069 |
| 0.6–0.7 | 6,766 | 6,389 | 0.6457 | 0.6543 | +0.0086 | [0.6426, 0.6659] | 0.147 |
| 0.7–0.8 | 6,660 | 6,211 | 0.7461 | 0.7622 | +0.0161 | [0.7512, 0.7721] | 0.004 \*\* |
| 0.8–0.9 | 7,568 | 6,745 | 0.8462 | 0.8622 | +0.0160 | [0.8537, 0.8706] | 2.6e−04 \*\*\* |
| 0.9–1.0 | 17,734 | 10,635 | 0.9651 | 0.9566 | −0.0086 | [0.9529, 0.9602] | 5.7e−06 \*\*\* |

*Bias is realized frequency minus mean price. Negative = the market charged
more than the event turned out to be worth. Intervals and tests cluster on
`event_ticker`; q is Benjamini-Hochberg-corrected across the ten buckets.*

**Every bucket below 50c is overpriced; four of five above 50c are
underpriced.** The sign flips cleanly at 0.50. Eight of ten buckets survive
correction; the two that do not are 0.50–0.70, the middle of the range, which
is exactly where the effect should be hardest to distinguish from zero.

Peak bias is 2.98 cents, at 30–40c. As a fraction of the contract's own price
that is 8.7%, so it is not negligible in relative terms even though it is small
in absolute ones.

### The bucket-free version agrees

Regressing the outcome on the market's own log-odds, so no bin edges are
involved at all:

```
slope      1.0442   95% CI [1.0229, 1.0656]   vs 1: z=+4.05  p=5.0e-05
intercept -0.0542   95% CI [-0.0777, -0.0308] vs 0: z=-4.53  p=5.8e-06
joint test of (0, 1): chi2 = 36.7 on 2 df, p = 1.1e-08
```

Perfect calibration is slope 1, intercept 0. A slope **above** 1 means the true
probabilities are more extreme than the prices — a market saying 5% is really
nearer 2%, one saying 95% is really nearer 98%. That is the favorite-longshot
direction, and the shape in the table is not an artefact of where the deciles
fell.

## 3. It is concentrated in short-lived markets

Splitting by how long each market traded before it was priced (quartiles at
15.6h / 25.0h / 45.1h):

| lifetime | 0.0–0.2 | 0.2–0.4 | 0.4–0.6 | 0.6–0.8 | 0.8–1.0 |
|---|---|---|---|---|---|
| **1–15.6h** | −0.0232 \*\*\* | **−0.0553** \*\*\* | −0.0241 \*\*\* | +0.0208 \*\* | +0.0000 |
| 15.6–25.0h | −0.0087 | −0.0184 | −0.0043 | +0.0033 | −0.0155 \*\* |
| 25.0–45.1h | −0.0017 | −0.0163 | +0.0037 | +0.0119 | +0.0005 |
| 45.1h+ | −0.0109 \*\*\* | −0.0156 | +0.0019 | +0.0116 | +0.0084 \* |

The shortest-lived quartile is miscalibrated in **four of five** price bands,
with the largest bias anywhere in the pooled data (−5.5 cents at 20–40c). The
two middle quartiles are almost entirely indistinguishable from calibrated.

That is consistent with an information-aggregation story — a market that has
only existed for a few hours has had less opportunity to incorporate what
people know — but it is not proof of one. Lifetime is confounded with what kind
of contract it is: a 15-hour market is overwhelmingly a same-day sporting
event.

## 4. The two results that complicate the story

### Heavy favorites run backwards

The 0.90–1.00 bucket is **negative** (−0.0086, q = 5.7e−06) where the
hypothesis predicts positive: contracts priced at 96.5c resolve yes 95.7% of
the time. This is not the ceiling effect I first assumed. It is the bucket
where clustering had its second-largest effect, and it survived comfortably. On
17,734 contracts from 10,635 events, it is a specific claim with no explanation
attached to it yet.

### The bias may be much larger outside Sports

| category | events | 0.0–0.2 | 0.2–0.4 | 0.4–0.6 | 0.6–0.8 | 0.8–1.0 |
|---|---|---|---|---|---|---|
| Sports | 29,471 | −0.009 \*\*\* | −0.027 \*\*\* | −0.007 | +0.013 \*\* | −0.001 |
| Economics | 186 | −0.030 \*\*\* | **−0.137** \*\* | −0.104 | −0.089 | +0.011 |
| Crypto | 222 | −0.026 \*\*\* | −0.152 \*\* | *thin* | **+0.168** \*\* | −0.085 |
| Politics | 16 | *thin* | *thin* | *thin* | *thin* | *thin* |

Economics' 20–40c band is priced at 28.8c and resolves yes 15.1% of the time —
a **13.7-point** gap, five times anything in the pooled table, and it survives
clustering and a family-wide correction on 85 events. Crypto shows the same
pattern in the longshot bands plus a large positive bias at 60–80c.

Two cautions, and they are load-bearing:

1. **The pooled table is 95.9% Sports.** Read every headline number above as a
   statement about Kalshi sports contracts that happens to include other
   markets.
2. **Non-Sports coverage is thin where it counts.** Economics has 2,743
   contracts but only **186 events**, and clustered inference is governed by
   events. Politics has 16 events in total and every cell is reported
   untestable — that is the honest answer, not a gap to paper over.

Also note Economics is negative in *four of five* bands, including above 50c.
That is not the favorite-longshot shape; it looks more like a general
overpricing of YES. If it holds up, it is a different effect wearing the same
clothes.

## 5. What clustering cost

Contracts sharing an event are not independent observations (3.35 per event;
one 250-golfer field is a single outcome written 250 ways). Treating them as
independent inflates every interval's precision.

The design effect — clustered SE divided by naive SE — is ~1.03 through the
middle buckets and rises in **both tails**: 1.45 at 0–10c and 1.20 at
90c–100c, the two buckets holding the large mutually-exclusive fields. In the
bottom bucket the corrected q moves from 5e−04 to 1.6e−02, a thirtyfold weaker
claim that remains significant.

No bucket's verdict flips in the pooled table. That is a fact about this
dataset's balance, not a licence to skip the correction: the shift is largest
precisely in the longshot bucket the hypothesis is about, and the segment
tables have one to two orders of magnitude fewer events to spend.

---

## What this does not yet establish

- **Whether it is tradeable.** A 2–3 cent edge has to clear fees and the
  bid-ask spread. No fee model has been built; that is Phase 4.
- **Out-of-sample.** Every number above is in-sample over one six-month window.
- **One venue.** Kalshi only; Polymarket remains the stretch second venue.
- **One horizon.** T-1h is the primary. T-6h and T-24h are ingested and not yet
  analysed, so the sensitivity of the whole result to that choice is untested.
- **Why favorites run backwards**, and **whether the non-Sports effect is
  real** — the two open questions above.
