# Favorite-Longshot Bias in Kalshi Prediction Markets

**Finding: the favorite-longshot bias is present in Kalshi contract prices, and it
replicates across two independent datasets on very different timescales.**

A prediction-market contract that trades at 30¢ is claiming a 30% chance. If prices
were perfectly calibrated, contracts priced near 30¢ would resolve YES about 30% of
the time. They don't. Cheap contracts resolve YES *less* often than their price
implies, and expensive contracts resolve YES *more* often — the pattern first
documented at racetracks, where bettors underpay for favorites and overpay for
longshots.

---

## Study 1 — settled contracts, priced one hour before close

82,595 settled Kalshi contracts, pooled into ten price bands and priced at a fixed
moment one hour before each contract closed.

Every band below 50¢ was overpriced. Every band from 50¢ to 90¢ was underpriced.
The sign flips cleanly at the 50¢ coin-flip point.

A horizon check at five hours earlier was also run, to test whether the bias is a
property of the prices or an artifact of the measurement moment.

## Study 2 — 15-minute markets, priced 8 minutes in

An independent replication on a completely different instrument: Kalshi's
15-minute up/down markets, which ask whether an asset will be higher 15 minutes
from now and settle continuously around the clock.

26,883 settled markets across 16 series (BTC, ETH, SOL, BNB, XRP, HYPE, NEAR,
DOGE, ZEC, gold, silver, platinum, copper, palladium, natural gas, WTI oil),
each priced at the 8-minute mark of its 15-minute window.

| Price band | n | Implied | Actually resolved YES | Gap (pts) |
|---|---|---|---|---|
| 0–10¢ | 3,176 | 5.71% | 4.63% | −1.08 |
| 10–20¢ | 2,857 | 14.48% | 12.46% | −2.02 |
| 20–30¢ | 2,568 | 24.38% | 22.04% | −2.34 |
| 30–40¢ | 2,301 | 34.35% | 33.12% | −1.23 |
| 40–50¢ | 2,116 | 44.49% | 44.09% | −0.40 |
| 50–60¢ | 2,102 | 54.50% | 54.00% | −0.50 |
| 60–70¢ | 2,373 | 64.61% | 67.26% | **+2.64** |
| 70–80¢ | 2,644 | 74.70% | 76.17% | **+1.47** |
| 80–90¢ | 2,943 | 84.58% | 86.03% | **+1.45** |
| 90–100¢ | 3,803 | 94.38% | 96.34% | **+1.96** |

Negative gap = contract resolved YES less often than its price claimed (overpriced).
Positive gap = resolved YES more often than its price claimed (underpriced).

Same shape as Study 1: sub-50¢ bands overpriced, upper bands underpriced, sign
flipping around the coin-flip point — on a different instrument, a different asset
mix, and a timescale roughly 250× shorter.

---

## Why replication across timescales matters

Study 1 measures contracts an hour before close. Study 2 measures them seven
minutes before close, on assets that didn't overlap with the first dataset. Two
datasets that share no contracts, no asset classes, and no time horizon,
producing the same directional bias, is stronger evidence than either result
alone — a single calibration curve can be an artifact of how one dataset was
assembled, but the same curve appearing twice under different conditions is
harder to explain that way.

---

## Scope and limitations

- Both studies measure **calibration**, not profitability. Whether a mispricing
  can be captured after transaction costs is a separate question and is not
  addressed here.
- Study 2 covers roughly three weeks of market history — a single market regime.
- Per-series results within Study 2 are individually noisy; the finding holds in
  aggregate, not reliably series by series.
- Kalshi market data was read from the public API. No account or position data is
  involved in either study.

---

## Code and data

**The implementation is private.** Source code, ingestion pipelines, cleaned
datasets, and the strategy work built on top of these findings are kept in a
private repository, as they underpin a personal trading strategy.

This repository exists to record the empirical finding only.
