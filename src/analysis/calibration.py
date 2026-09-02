"""Calibration measurement: reliability diagram, Brier score, Murphy decomposition.

Pure functions over the processed DataFrame. Data in, data out -- no plotting
and no file writing, so every figure and every table in the report is derived
from the same numbers and a notebook cannot quietly compute its own variant.

The vocabulary, once, since the rest of the module leans on it:

    implied_price   what the market said P(event) was, an hour before close.
    outcome         what happened, 1 or 0.
    calibration     whether those agree *on average within a price level*. Of
                    all the contracts priced near 30c, did about 30% happen?
    bias            realized frequency minus mean price, per bucket. Negative
                    means the market priced the event higher than it happened
                    -- overpriced. The favorite-longshot hypothesis predicts
                    negative bias among longshots and positive among favorites.

Every interval and p-value here is clustered on `event_ticker`, because
contracts sharing an event are one outcome expressed many times (design decision doc 004,
decision 6). `statistics.py` holds the estimators; this module decides what
gets estimated.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats

from src.analysis.statistics import (
    benjamini_hochberg,
    cluster_bootstrap_ratio_ci,
    clustered_mean_test,
    wilson_interval,
)
from src.config import Config, get_config

__all__ = [
    "assign_buckets",
    "calibration_table",
    "brier_score",
    "brier_decomposition",
    "LogisticCalibration",
    "logistic_calibration",
]

PRICE_COL = "implied_price"
OUTCOME_COL = "outcome"


def _require_columns(df: pd.DataFrame, columns: tuple[str, ...]) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise KeyError(f"missing required column(s): {missing}")


def assign_buckets(prices: np.ndarray | pd.Series, n_buckets: int) -> np.ndarray:
    """Map prices in [0, 1] to equal-width bucket indices 0..n_buckets-1.

    Equal *width*, not equal count. Equal-count (quantile) buckets would put
    roughly 10,000 contracts in each bin, which is tempting for precision and
    wrong for this question: the hypothesis is about specific price *levels*
    ("longshots priced 0-10c are overpriced"), so the bin edges have to be
    price levels fixed in advance rather than edges chosen by the data. Data-
    chosen edges would also make the table incomparable to the published
    favorite-longshot literature, which uses fixed odds bands throughout.

    Both endpoints are included: a price of exactly 1.0 goes in the top bucket
    rather than a bucket of its own. Contracts do print at 1.0 (a settled-in-all
    -but-name favorite), so this edge is real, not hypothetical.
    """
    prices = np.asarray(prices, dtype=float)
    if prices.size and (prices.min() < 0.0 or prices.max() > 1.0):
        raise ValueError(
            f"prices must lie in [0, 1]; got [{prices.min()}, {prices.max()}]"
        )
    indices = np.floor(prices * n_buckets).astype(int)
    return np.clip(indices, 0, n_buckets - 1)


def calibration_table(
    df: pd.DataFrame,
    n_buckets: int | None = None,
    config: Config | None = None,
) -> pd.DataFrame:
    """The reliability diagram as a table: predicted vs. realized, per bucket.

    This is the study's headline output. Each row is a price level; the
    question is whether `realized_freq` matches `mean_price`, and `bias` is
    their difference.

    Three interval columns, deliberately:

        ci_low/ci_high      Clustered block bootstrap on the realized
                            frequency, resampling whole events. **The
                            reportable interval.**
        wilson_low/high     The same interval computed as if every contract
                            were an independent draw. Not reportable; present
                            only so `design_effect` can show what pretending
                            independence would have bought.
        bias_se             Cluster-robust standard error of the bias, which
                            is what the p-value is built from.

    Significance is two-sided against H0: this bucket's realized frequency
    equals its mean price, with Benjamini-Hochberg correction across the
    buckets (`q_value`). A bucket is called significant only on the corrected
    value.

    Args:
        df: Processed contracts. Needs implied_price, outcome, and the
            clustering column.
        n_buckets: Overrides config.analysis.n_buckets.
        config: Overrides the loaded config.

    Returns:
        One row per non-empty bucket, ordered by price level, with columns:
        bucket, bucket_low, bucket_high, n, n_events, mean_price,
        realized_freq, bias, ci_low, ci_high, wilson_low, wilson_high,
        bias_se, naive_se, design_effect, t_stat, p_value, q_value,
        significant.
    """
    config = config or get_config()
    settings = config.analysis
    n_buckets = n_buckets or settings.n_buckets
    cluster_col = settings.cluster_on
    _require_columns(df, (PRICE_COL, OUTCOME_COL, cluster_col))

    if df.empty:
        raise ValueError("cannot build a calibration table from zero contracts")

    prices = df[PRICE_COL].to_numpy(dtype=float)
    outcomes = df[OUTCOME_COL].to_numpy(dtype=float)
    clusters = df[cluster_col].to_numpy()
    buckets = assign_buckets(prices, n_buckets)

    present = np.unique(buckets)

    # One bootstrap for the whole table, not one per bucket. A resampled event
    # has contracts in several buckets and must be added to or dropped from
    # all of them together, or the intervals stop being coherent as a table.
    membership = np.zeros((len(df), present.size), dtype=float)
    successes = np.zeros_like(membership)
    for column, bucket in enumerate(present):
        in_bucket = buckets == bucket
        membership[in_bucket, column] = 1.0
        successes[in_bucket, column] = outcomes[in_bucket]

    ci_low, ci_high = cluster_bootstrap_ratio_ci(
        successes,
        membership,
        clusters,
        reps=settings.bootstrap_reps,
        seed=settings.bootstrap_seed,
        confidence=settings.confidence,
    )

    width = 1.0 / n_buckets
    rows = []
    for column, bucket in enumerate(present):
        in_bucket = buckets == bucket
        bucket_prices = prices[in_bucket]
        bucket_outcomes = outcomes[in_bucket]
        n = int(in_bucket.sum())
        n_yes = int(bucket_outcomes.sum())

        # The tested quantity is the per-contract calibration error, whose mean
        # is exactly realized_freq - mean_price. Testing it as a mean (rather
        # than as a proportion against a fixed target) is what lets the
        # cluster-robust estimator see the within-event correlation at all.
        errors = bucket_outcomes - bucket_prices
        test = clustered_mean_test(errors, clusters[in_bucket])
        naive_low, naive_high = wilson_interval(n_yes, n, settings.confidence)

        rows.append(
            {
                "bucket": int(bucket),
                "bucket_low": bucket * width,
                "bucket_high": (bucket + 1) * width,
                "n": n,
                "n_events": test.n_clusters,
                "mean_price": float(bucket_prices.mean()),
                "realized_freq": n_yes / n,
                "bias": test.mean,
                "ci_low": float(ci_low[column]),
                "ci_high": float(ci_high[column]),
                "wilson_low": naive_low,
                "wilson_high": naive_high,
                "bias_se": test.se,
                "naive_se": test.naive_se,
                "design_effect": test.design_effect,
                "t_stat": test.t_stat,
                "p_value": test.p_value,
            }
        )

    table = pd.DataFrame(rows)
    rejected, q_values = benjamini_hochberg(
        table["p_value"].to_numpy(), alpha=settings.fdr_alpha
    )
    table["q_value"] = q_values
    table["significant"] = rejected
    return table


def brier_score(df: pd.DataFrame) -> float:
    """Mean squared error of the implied price against the outcome.

    BS = (1/N) * sum_i (f_i - o_i)^2, with f the price and o in {0, 1}. Range
    is [0, 1] and lower is better; 0.25 is what a forecaster who always says
    50% scores on a balanced sample.

    It is a *proper* scoring rule: the expected score is minimised only by
    reporting your true belief, so it cannot be gamed by shading forecasts
    toward the extremes to look decisive. That is why it is the headline
    accuracy number rather than, say, hit rate.

    The raw score is not comparable across datasets with different base rates
    -- a corpus of near-certainties scores well for reasons that have nothing
    to do with skill -- which is exactly what `brier_decomposition` separates.
    """
    _require_columns(df, (PRICE_COL, OUTCOME_COL))
    if df.empty:
        raise ValueError("cannot score zero contracts")
    prices = df[PRICE_COL].to_numpy(dtype=float)
    outcomes = df[OUTCOME_COL].to_numpy(dtype=float)
    return float(np.mean((prices - outcomes) ** 2))


def brier_decomposition(
    df: pd.DataFrame,
    n_buckets: int | None = None,
    config: Config | None = None,
) -> dict[str, float]:
    """Murphy's three-way partition of the Brier score.

    Bucket the forecasts, then with n_k contracts in bucket k, mean price f_k,
    realized frequency o_k, and overall base rate o_bar:

        reliability = (1/N) * sum_k n_k * (f_k - o_k)^2
        resolution  = (1/N) * sum_k n_k * (o_k - o_bar)^2
        uncertainty = o_bar * (1 - o_bar)

        brier = reliability - resolution + uncertainty

    What each measures:

    *Reliability* is calibration error -- how far each bucket's realized
    frequency sits from the price it was sold at. Lower is better, and this is
    the term the favorite-longshot bias lives in.

    *Resolution* is how far the buckets spread away from the base rate: how
    much the forecasts actually discriminate between things that happened and
    things that didn't. Higher is better, and it is subtracted.

    *Uncertainty* is the base rate's own variance. It depends only on how often
    events happened, not on the forecasts, so nothing anyone forecasts can
    change it -- it is the floor the other two terms are measured against.

    This is what makes "well calibrated" and "useful" different properties. A
    forecaster who answers with the base rate on every question is perfectly
    calibrated (reliability = 0) and completely useless (resolution = 0), and
    scores exactly the uncertainty. Skill is calibration *plus* resolution.

    **On the exactness of the identity.** It holds exactly when every forecast
    within a bucket is the same number, which is true of a weather forecaster
    who only ever says 10%, 20%, ... and false here, where prices vary
    continuously inside each decile. Binning replaces each contract's price
    with its bucket mean, and that substitution changes the score slightly. So
    this function returns both: `binned_brier`, which the identity closes on
    exactly, and `brier`, the real score on unbinned prices. Their difference
    is `binning_residual` -- the within-bucket price variation that decile
    buckets cannot see. A large residual means the buckets are too coarse to
    describe the forecasts, which is a property worth knowing rather than
    hiding inside an "approximately equals".

    Returns:
        dict with reliability, resolution, uncertainty, binned_brier, brier,
        binning_residual, base_rate, n_buckets, n.
    """
    config = config or get_config()
    n_buckets = n_buckets or config.analysis.n_buckets
    _require_columns(df, (PRICE_COL, OUTCOME_COL))
    if df.empty:
        raise ValueError("cannot decompose the score of zero contracts")

    prices = df[PRICE_COL].to_numpy(dtype=float)
    outcomes = df[OUTCOME_COL].to_numpy(dtype=float)
    buckets = assign_buckets(prices, n_buckets)

    n = prices.size
    base_rate = float(outcomes.mean())

    reliability = 0.0
    resolution = 0.0
    binned_brier = 0.0
    for bucket in np.unique(buckets):
        in_bucket = buckets == bucket
        n_k = int(in_bucket.sum())
        f_k = float(prices[in_bucket].mean())
        o_k = float(outcomes[in_bucket].mean())
        reliability += n_k * (f_k - o_k) ** 2
        resolution += n_k * (o_k - base_rate) ** 2
        binned_brier += float(np.sum((f_k - outcomes[in_bucket]) ** 2))

    reliability /= n
    resolution /= n
    binned_brier /= n
    uncertainty = base_rate * (1.0 - base_rate)
    raw_brier = float(np.mean((prices - outcomes) ** 2))

    return {
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "binned_brier": binned_brier,
        "brier": raw_brier,
        "binning_residual": raw_brier - binned_brier,
        "base_rate": base_rate,
        "n_buckets": int(n_buckets),
        "n": int(n),
    }


@dataclass(frozen=True)
class LogisticCalibration:
    """Result of regressing the outcome on the market's own logit.

    Attributes:
        slope, intercept: Fitted coefficients. Perfect calibration is
            slope 1, intercept 0.
        slope_se, intercept_se: Cluster-robust standard errors.
        slope_ci, intercept_ci: Confidence intervals at config.confidence.
        slope_z, slope_p: Test of H0 slope = 1 -- NOT the regression's default
            test against 0, which asks whether price predicts outcome at all
            and is answered trivially yes.
        intercept_z, intercept_p: Test of H0 intercept = 0.
        joint_chi2, joint_df, joint_p: Wald test of both at once, H0
            (intercept, slope) = (0, 1). A market can miss the ideal on the
            pair while neither coefficient misses it alone.
        n, n_clusters: Contracts and events behind the fit.
        pseudo_r2: McFadden's, for reference only.
    """

    slope: float
    intercept: float
    slope_se: float
    intercept_se: float
    slope_ci: tuple[float, float]
    intercept_ci: tuple[float, float]
    slope_z: float
    slope_p: float
    intercept_z: float
    intercept_p: float
    joint_chi2: float
    joint_df: int
    joint_p: float
    n: int
    n_clusters: int
    pseudo_r2: float


def logistic_calibration(
    df: pd.DataFrame, config: Config | None = None
) -> LogisticCalibration:
    """Fit `outcome ~ logit(implied_price)` and compare it to the ideal.

    The second, bucket-free reading of the same question. `calibration_table`
    has to choose bin edges, and a reader is entitled to wonder how much of the
    shape those edges created. This uses every price at its own value.

    The model is

        logit(P(outcome)) = intercept + slope * logit(price)

    and a perfectly calibrated market gives intercept 0, slope 1: the market's
    stated log-odds *are* the true log-odds.

    **Reading the slope.** This is the part that is easy to get backwards, so
    it is worked through rather than asserted.

        slope > 1   True probabilities are MORE extreme than prices. A market
                    saying 5% is really 2%; one saying 95% is really 98%.
                    Longshots overpriced, favorites underpriced -- the
                    favorite-longshot bias.

        slope < 1   True probabilities are LESS extreme than prices. A market
                    saying 5% is really 15%. The market exaggerates, and
                    longshots are cheap -- the reverse of the hypothesis.

    Note this is the opposite of the familiar "slope < 1 means overconfident"
    from the forecast-evaluation literature, which regresses the other way
    round. Deriving it from the fitted equation each time is safer than
    remembering which convention a source used.

    **The test that matters is against 1, not 0.** Statsmodels reports
    `P>|z|` for H0 slope = 0, which asks whether price predicts outcome at all.
    It does, overwhelmingly, and that number is a distraction. The calibration
    question is whether the slope differs from 1, so that test is computed
    here.

    Standard errors cluster on `event_ticker` (design decision doc 004 decision 6): the fit is
    over 100k contracts but only ~30k independent events, and the uncorrected
    errors would be far too small.

    Every price in the processed layer lies strictly inside (0, 1) -- Kalshi's
    tick floor is 0.001 -- so the logit is defined on every row and no
    clipping, winsorising, or dropping is needed. This is measured on the
    corpus rather than assumed; a price of exactly 0 or 1 would raise.
    """
    config = config or get_config()
    settings = config.analysis
    cluster_col = settings.cluster_on
    _require_columns(df, (PRICE_COL, OUTCOME_COL, cluster_col))
    if df.empty:
        raise ValueError("cannot fit a calibration regression on zero contracts")

    prices = df[PRICE_COL].to_numpy(dtype=float)
    if np.any((prices <= 0.0) | (prices >= 1.0)):
        n_bad = int(np.sum((prices <= 0.0) | (prices >= 1.0)))
        raise ValueError(
            f"{n_bad} price(s) at exactly 0 or 1, where the logit is undefined. "
            "The processed layer should contain none; handling them is a "
            "research decision, not something to silently clip."
        )

    outcomes = df[OUTCOME_COL].to_numpy(dtype=float)
    groups = pd.factorize(df[cluster_col])[0]
    n_clusters = int(groups.max()) + 1

    design = sm.add_constant(np.log(prices / (1.0 - prices)))
    fit = sm.Logit(outcomes, design).fit(
        cov_type="cluster", cov_kwds={"groups": groups}, disp=0
    )

    intercept, slope = float(fit.params[0]), float(fit.params[1])
    intercept_se, slope_se = float(fit.bse[0]), float(fit.bse[1])
    conf = fit.conf_int(alpha=1.0 - settings.confidence)

    slope_z = (slope - 1.0) / slope_se
    intercept_z = intercept / intercept_se
    # Both coefficients at once. R selects them, q is the ideal (0, 1).
    joint = fit.wald_test(
        (np.eye(2), np.array([0.0, 1.0])), scalar=False, use_f=False
    )

    return LogisticCalibration(
        slope=slope,
        intercept=intercept,
        slope_se=slope_se,
        intercept_se=intercept_se,
        slope_ci=(float(conf[1][0]), float(conf[1][1])),
        intercept_ci=(float(conf[0][0]), float(conf[0][1])),
        slope_z=float(slope_z),
        slope_p=float(2.0 * stats.norm.sf(abs(slope_z))),
        intercept_z=float(intercept_z),
        intercept_p=float(2.0 * stats.norm.sf(abs(intercept_z))),
        joint_chi2=float(np.squeeze(joint.statistic)),
        joint_df=int(joint.df_denom if joint.df_denom else 2),
        joint_p=float(np.squeeze(joint.pvalue)),
        n=len(df),
        n_clusters=n_clusters,
        pseudo_r2=float(fit.prsquared),
    )
