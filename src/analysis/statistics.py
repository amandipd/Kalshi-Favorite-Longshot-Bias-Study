"""Interval estimation and hypothesis testing for the calibration study.

Pure functions over arrays. Nothing here reads config, touches disk, or knows
what a contract is -- `calibration.py` supplies the numbers and this module
says how uncertain they are.

The whole module exists to answer one question honestly: when a price bucket
resolves yes 12.5% of the time against a mean price of 15.2%, is that 2.7-point
gap real or is it noise? Two distinct threats stand between the point estimate
and that answer, and they need two different fixes:

    correlated observations   Contracts sharing an `event_ticker` are not
    (within one test)         separate draws. A 250-golfer field is *one*
                              outcome written 250 ways, and a threshold ladder
                              is monotonically bound by construction. Treating
                              them as 250 independent contracts inflates the
                              apparent sample and shrinks every interval.
                              Fixed by clustering -- `cluster_robust_se` and
                              `cluster_bootstrap_ci` below.

    many hypotheses           Ten buckets tested at p<0.05 each will produce
    (across tests)            roughly one false positive per run even if the
                              market is perfectly calibrated. Fixed by
                              controlling the false discovery rate --
                              `benjamini_hochberg`.

They are independent problems and neither correction substitutes for the
other; see docs/adr/005-bucketing-and-tests.md. `wilson_interval` is kept for
the naive-vs-clustered comparison, and is never a reportable interval on its
own for this dataset.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

__all__ = [
    "wilson_interval",
    "cluster_robust_se",
    "cluster_bootstrap_ci",
    "cluster_bootstrap_ratio_ci",
    "benjamini_hochberg",
    "MeanTest",
    "clustered_mean_test",
]


def wilson_interval(
    successes: int, n: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Why Wilson and not the textbook normal approximation p +/- z*sqrt(p(1-p)/n):
    the normal ("Wald") interval is built by pretending the *estimate* is the
    true proportion and treating its sampling distribution as symmetric. Both
    pretences fail exactly where this study lives. At p_hat = 0.03 the Wald
    interval reaches below zero, which is not a probability; at p_hat = 0
    (a bucket where nothing resolved yes) its width collapses to zero, claiming
    perfect certainty from the least informative data possible. Longshot
    buckets are the whole hypothesis, so an interval that misbehaves at the
    extremes is not a detail.

    Wilson instead inverts the score test: it asks which true proportions p
    would fail to reject the observed count, solving

        |p_hat - p| / sqrt(p(1-p)/n) <= z

    for p rather than for p_hat. The variance is evaluated at the hypothesised
    p, not the estimate, so the interval stays inside [0, 1], stays sensibly
    wide at 0 and 1, and holds close to nominal coverage at n as small as ~40.

    **This interval assumes independent Bernoulli draws, which this dataset
    violates.** It is computed only to quantify how badly -- the ratio of a
    clustered width to this one is the design effect reported in the
    calibration table. ADR 004 decision 6 forbids reporting it as the interval.

    Args:
        successes: Count of outcomes equal to 1.
        n: Total observations. Must be positive.
        confidence: Two-sided coverage, e.g. 0.95.

    Returns:
        (low, high), both within [0, 1].
    """
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    if not 0 <= successes <= n:
        raise ValueError(f"successes ({successes}) must lie in [0, {n}]")

    z = stats.norm.ppf(1.0 - (1.0 - confidence) / 2.0)
    p_hat = successes / n
    denominator = 1.0 + z**2 / n
    # The centre is the observed proportion pulled toward 1/2 -- the score
    # test's own shrinkage, not an ad-hoc prior.
    centre = (p_hat + z**2 / (2 * n)) / denominator
    half_width = (
        z * np.sqrt(p_hat * (1.0 - p_hat) / n + z**2 / (4 * n**2)) / denominator
    )
    return float(max(0.0, centre - half_width)), float(min(1.0, centre + half_width))


def cluster_robust_se(values: np.ndarray, clusters: np.ndarray) -> tuple[float, int]:
    """Cluster-robust standard error of the mean of `values`.

    The estimator (Liang-Zeger, specialised to a mean) sums residuals *within*
    each cluster before squaring:

        SE^2 = (G / (G - 1)) * sum_g ( sum_{i in g} (x_i - x_bar) )^2 / n^2

    The nesting is the whole point. The independent-sample formula squares each
    residual on its own, which implicitly assumes cross-terms average to zero.
    Inside an event they do not: a golfer field's residuals are strongly
    negatively bound (one winner, 249 losers), a threshold ladder's positively.
    Squaring the cluster's *total* keeps those cross-terms, so correlation
    shows up as a wider interval instead of vanishing.

    Two consequences worth expecting. Clustering usually widens the interval
    but is not required to -- with negatively correlated siblings, which is
    what a mutually-exclusive field produces, cluster totals vary *less* than
    independent draws would and the SE can legitimately shrink. And precision
    is now governed by G, the number of clusters, not by n: 8,000 contracts
    from 1,200 events carry roughly 1,200 events' worth of information.

    Args:
        values: Per-observation quantity being averaged, shape (n,).
        clusters: Group label per observation, shape (n,). Any hashable dtype;
            only equality is used.

    Returns:
        (standard_error, n_clusters). The SE is 0.0 when G == 1, where the
        estimator is undefined -- callers must treat that as "no inference
        possible", not as certainty.
    """
    values = np.asarray(values, dtype=float)
    clusters = np.asarray(clusters)
    if values.shape[0] != clusters.shape[0]:
        raise ValueError(
            f"values ({values.shape[0]}) and clusters ({clusters.shape[0]}) "
            "must have the same length"
        )
    n = values.shape[0]
    if n == 0:
        raise ValueError("cannot compute a standard error of zero observations")

    codes, _ = _factorize(clusters)
    n_clusters = int(codes.max()) + 1
    if n_clusters < 2:
        return 0.0, n_clusters

    residuals = values - values.mean()
    cluster_sums = np.bincount(codes, weights=residuals, minlength=n_clusters)
    correction = n_clusters / (n_clusters - 1.0)
    variance = correction * float(np.sum(cluster_sums**2)) / n**2
    return float(np.sqrt(variance)), n_clusters


@dataclass(frozen=True)
class MeanTest:
    """Result of a clustered two-sided test that a mean is zero.

    Attributes:
        mean: The point estimate.
        se: Cluster-robust standard error.
        naive_se: Standard error assuming independent observations, kept for
            the design-effect ratio.
        t_stat: mean / se.
        p_value: Two-sided p-value from a t distribution with G - 1 df.
        n: Observations.
        n_clusters: Distinct clusters, which is the sample size that matters.
        design_effect: se / naive_se. Above 1 means the naive interval was too
            narrow by that factor; below 1 means the within-event correlation
            is negative, which mutually-exclusive fields genuinely produce.
    """

    mean: float
    se: float
    naive_se: float
    t_stat: float
    p_value: float
    n: int
    n_clusters: int
    design_effect: float


def clustered_mean_test(values: np.ndarray, clusters: np.ndarray) -> MeanTest:
    """Two-sided test of H0: E[values] = 0, clustering on `clusters`.

    Reference distribution is Student's t with G - 1 degrees of freedom rather
    than the normal. With ~30,000 events the two are indistinguishable, but the
    per-category and per-horizon segments in `segmentation.py` run on far fewer
    clusters, where the normal would overstate significance. Using t everywhere
    means the headline table and the thin segments are computed the same way.

    Returns a MeanTest; see its docstring for the fields.
    """
    values = np.asarray(values, dtype=float)
    n = values.shape[0]
    mean = float(values.mean())

    # ddof=1: the sample variance, since the mean is estimated from the data.
    naive_se = float(np.std(values, ddof=1) / np.sqrt(n)) if n > 1 else 0.0
    se, n_clusters = cluster_robust_se(values, clusters)

    if se > 0 and n_clusters > 1:
        t_stat = mean / se
        p_value = float(2.0 * stats.t.sf(abs(t_stat), df=n_clusters - 1))
    else:
        # Degenerate: one cluster, or zero variation between clusters. No
        # evidence either way, so report the least significant thing possible
        # rather than dividing by zero and calling it certainty.
        t_stat = 0.0 if se == 0 else float("nan")
        p_value = 1.0

    return MeanTest(
        mean=mean,
        se=se,
        naive_se=naive_se,
        t_stat=float(t_stat),
        p_value=p_value,
        n=n,
        n_clusters=n_clusters,
        design_effect=float(se / naive_se) if naive_se > 0 else float("nan"),
    )


def cluster_bootstrap_ci(
    statistic_matrix: np.ndarray,
    clusters: np.ndarray,
    reps: int,
    seed: int,
    confidence: float = 0.95,
) -> tuple[np.ndarray, np.ndarray]:
    """Percentile confidence intervals from resampling whole clusters.

    The block bootstrap resamples *events* with replacement, never individual
    contracts. Drawing contracts independently would rebuild the very
    independence the clustering exists to deny -- it would happily draw 30
    copies of one golfer and none of his field, breaking the "exactly one
    winner" constraint that makes the siblings correlated in the first place.
    Taking an event means taking all of its contracts, so every replicate
    respects the constraints the real data has.

    Resampling is done jointly across all buckets rather than per bucket. A
    single event's contracts land in several price buckets, so a replicate that
    drops that event must drop it from all of them at once; that is what makes
    the resulting intervals coherent as a *table* rather than ten unrelated
    intervals.

    Implementation note: resampling G clusters with replacement is equivalent
    to drawing multinomial weights over the G clusters, which turns each
    replicate into one small matrix product instead of a gather over every row.
    That is what makes 2,000 reps over 100k contracts a second rather than a
    coffee break.

    Args:
        statistic_matrix: Shape (n_observations, k). Each column is a quantity
            to be *averaged* over the resampled observations. Bootstrapping a
            ratio (a bucket's yes-rate, say) is done by passing its numerator
            and denominator as separate columns and dividing afterwards, since
            the mean of the ratio is not the ratio of the means.
        clusters: Cluster label per observation, shape (n_observations,).
        reps: Number of replications.
        seed: Fixed for reproducibility.
        confidence: Two-sided coverage.

    Returns:
        (low, high), each shape (k,), the percentile bounds of the resampled
        column sums. Callers combine columns (e.g. successes / count) *per
        replicate*, so this returns sums rather than means -- see
        `cluster_bootstrap_ratio_ci` for the common case.
    """
    sums = _bootstrap_cluster_sums(statistic_matrix, clusters, reps, seed)
    alpha = 1.0 - confidence
    low = np.percentile(sums, 100 * alpha / 2.0, axis=0)
    high = np.percentile(sums, 100 * (1.0 - alpha / 2.0), axis=0)
    return low, high


def cluster_bootstrap_ratio_ci(
    numerator: np.ndarray,
    denominator: np.ndarray,
    clusters: np.ndarray,
    reps: int,
    seed: int,
    confidence: float = 0.95,
) -> tuple[np.ndarray, np.ndarray]:
    """Percentile intervals for per-column ratios under a cluster bootstrap.

    The intended use is a whole calibration table at once: `numerator` holds
    each contract's outcome indicator per bucket and `denominator` its
    membership indicator, so column b of the ratio is bucket b's realized
    frequency. Forming the ratio inside each replicate -- rather than
    bootstrapping numerator and denominator separately -- keeps the two moving
    together, which is what a resampled event actually does to them.

    A replicate that draws no observations for some bucket yields 0/0 there;
    those replicates are dropped from that column's percentiles (and only that
    column's), since they carry no information about its frequency.

    Args:
        numerator: Shape (n, k), per-observation numerator contributions.
        denominator: Shape (n, k), per-observation denominator contributions.
        clusters: Cluster label per observation.
        reps, seed, confidence: As `cluster_bootstrap_ci`.

    Returns:
        (low, high), each shape (k,). NaN in a column that never had a
        non-empty replicate.
    """
    numerator = np.asarray(numerator, dtype=float)
    denominator = np.asarray(denominator, dtype=float)
    if numerator.shape != denominator.shape:
        raise ValueError(
            f"numerator {numerator.shape} and denominator {denominator.shape} "
            "must have the same shape"
        )

    k = numerator.shape[1]
    stacked = np.hstack([numerator, denominator])
    sums = _bootstrap_cluster_sums(stacked, clusters, reps, seed)
    num_sums, den_sums = sums[:, :k], sums[:, k:]

    alpha = 1.0 - confidence
    low = np.full(k, np.nan)
    high = np.full(k, np.nan)
    for column in range(k):
        usable = den_sums[:, column] > 0
        if not usable.any():
            continue
        ratios = num_sums[usable, column] / den_sums[usable, column]
        low[column] = np.percentile(ratios, 100 * alpha / 2.0)
        high[column] = np.percentile(ratios, 100 * (1.0 - alpha / 2.0))
    return low, high


def benjamini_hochberg(
    p_values: np.ndarray, alpha: float = 0.05
) -> tuple[np.ndarray, np.ndarray]:
    """Benjamini-Hochberg step-up procedure: control the false discovery rate.

    The problem it solves: ten bucket tests at alpha = 0.05 each give roughly a
    40% chance (1 - 0.95^10) of at least one false positive even if the market
    is perfectly calibrated, and this study runs far more than ten once
    categories and horizons are segmented. Reporting "bucket 7 is significant"
    from an uncorrected sweep is reporting the sweep's noise floor.

    Why BH rather than Bonferroni: Bonferroni controls the probability of *any*
    false positive, which is the right target when a single false claim is
    ruinous. Here the claim is a shape across buckets, so the useful guarantee
    is that of the buckets called significant, at most `alpha` of them are
    expected to be false -- the false discovery rate. BH is also far less
    conservative, which matters because the favorite-longshot effect is small
    (single-digit percentage points) and Bonferroni at n=10 would need
    p < 0.005 per bucket to notice it.

    The procedure: sort the m p-values ascending, find the largest rank i with
    p_(i) <= (i/m) * alpha, and reject everything up to it. The adjusted values
    returned are the standard monotone-enforced q-values, computed by taking a
    running minimum from the largest p downward so that a small p can never end
    up with a larger q than a bigger one.

    Args:
        p_values: Raw two-sided p-values, shape (m,).
        alpha: Target false discovery rate.

    Returns:
        (rejected, q_values), both shape (m,), in the input order. NaN p-values
        (a degenerate bucket) are never rejected and keep a NaN q-value.
    """
    p_values = np.asarray(p_values, dtype=float)
    m_total = p_values.shape[0]
    rejected = np.zeros(m_total, dtype=bool)
    q_values = np.full(m_total, np.nan)

    valid = np.flatnonzero(~np.isnan(p_values))
    m = valid.shape[0]
    if m == 0:
        return rejected, q_values

    order = valid[np.argsort(p_values[valid], kind="stable")]
    ranks = np.arange(1, m + 1)
    scaled = p_values[order] * m / ranks

    # Running minimum from the largest p downward enforces monotonicity: q is
    # the smallest FDR at which this hypothesis, and every more significant
    # one, would be rejected.
    q_sorted = np.minimum.accumulate(scaled[::-1])[::-1]
    q_values[order] = np.minimum(q_sorted, 1.0)

    below = np.flatnonzero(p_values[order] <= ranks * alpha / m)
    if below.size:
        rejected[order[: below[-1] + 1]] = True
    return rejected, q_values


def _factorize(labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Map arbitrary cluster labels to contiguous integer codes 0..G-1."""
    uniques, codes = np.unique(np.asarray(labels), return_inverse=True)
    return codes.astype(np.intp).ravel(), uniques


def _bootstrap_cluster_sums(
    matrix: np.ndarray, clusters: np.ndarray, reps: int, seed: int
) -> np.ndarray:
    """Column sums of `matrix` under `reps` cluster-resampled replicates.

    Returns shape (reps, k). Each replicate draws G clusters with replacement;
    a cluster drawn twice contributes its totals twice, exactly as a literal
    resample would.
    """
    matrix = np.asarray(matrix, dtype=float)
    if matrix.ndim != 2:
        raise ValueError(f"expected a 2-D matrix, got shape {matrix.shape}")
    if matrix.shape[0] != len(clusters):
        raise ValueError(
            f"matrix has {matrix.shape[0]} rows but there are {len(clusters)} "
            "cluster labels"
        )
    if reps < 1:
        raise ValueError(f"reps must be at least 1, got {reps}")

    codes, uniques = _factorize(clusters)
    n_clusters = uniques.shape[0]

    # Per-cluster totals, so a replicate is a weighted sum over G rows rather
    # than a gather over n.
    cluster_totals = np.zeros((n_clusters, matrix.shape[1]))
    np.add.at(cluster_totals, codes, matrix)

    rng = np.random.default_rng(seed)
    # Multinomial counts over clusters == sampling G clusters with replacement.
    weights = rng.multinomial(
        n_clusters, np.full(n_clusters, 1.0 / n_clusters), size=reps
    ).astype(float)
    return weights @ cluster_totals
