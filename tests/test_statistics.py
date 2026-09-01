"""Tests for src/analysis/statistics.py.

If the statistics are wrong, every number downstream is worthless and looks
fine, so these are checked against values computed by hand or published in the
source papers rather than against whatever the code happened to return.

Two properties get the most attention because they are the ones that would
fail silently:

  * a cluster-robust SE must *see* within-cluster correlation, in both
    directions -- wider when siblings agree, narrower when they are mutually
    exclusive, which is what a golfer field actually is;
  * BH must reject strictly less often than the uncorrected sweep it replaces.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.analysis.statistics import (
    benjamini_hochberg,
    cluster_bootstrap_ratio_ci,
    cluster_robust_se,
    clustered_mean_test,
    wilson_interval,
)


# --------------------------------------------------------------------------
# Wilson interval
# --------------------------------------------------------------------------


def test_wilson_matches_the_published_worked_example():
    """2 successes in 10 at 95% is (0.0567, 0.5098) in every textbook."""
    low, high = wilson_interval(2, 10, confidence=0.95)
    assert low == pytest.approx(0.0567, abs=1e-4)
    assert high == pytest.approx(0.5098, abs=1e-4)


def test_wilson_is_not_degenerate_at_zero_successes():
    """The failure that motivates Wilson over Wald.

    Wald gives p_hat +/- z*sqrt(p_hat(1-p_hat)/n), which at p_hat = 0 is the
    single point 0 -- perfect certainty from a bucket where nothing happened.
    Longshot buckets sit near zero, so this is the case that matters.
    """
    low, high = wilson_interval(0, 50, confidence=0.95)
    assert low == pytest.approx(0.0, abs=1e-12)
    assert high > 0.0
    # Still informative: 50 observations should rule out anything large.
    assert high < 0.10


def test_wilson_never_leaves_the_unit_interval():
    for successes, n in [(0, 5), (5, 5), (1, 3), (99, 100)]:
        low, high = wilson_interval(successes, n)
        assert 0.0 <= low <= high <= 1.0


def test_wilson_narrows_as_the_sample_grows():
    widths = [
        wilson_interval(n // 10, n)[1] - wilson_interval(n // 10, n)[0]
        for n in (100, 1_000, 10_000)
    ]
    assert widths[0] > widths[1] > widths[2]


@pytest.mark.parametrize("successes,n", [(-1, 10), (11, 10), (0, 0), (1, -5)])
def test_wilson_rejects_impossible_counts(successes, n):
    with pytest.raises(ValueError):
        wilson_interval(successes, n)


# --------------------------------------------------------------------------
# Cluster-robust standard error
# --------------------------------------------------------------------------


def test_cluster_se_matches_a_hand_computation():
    """values [1,1,0,0] in clusters [A,A,B,B], worked through by hand.

        mean      = 0.5
        residuals = [+.5, +.5, -.5, -.5]
        cluster sums: A = +1.0, B = -1.0
        SE^2 = (G/(G-1)) * (1^2 + (-1)^2) / n^2 = 2 * 2 / 16 = 0.25
        SE   = 0.5
    """
    values = np.array([1.0, 1.0, 0.0, 0.0])
    clusters = np.array(["A", "A", "B", "B"])
    se, n_clusters = cluster_robust_se(values, clusters)
    assert n_clusters == 2
    assert se == pytest.approx(0.5)


def test_positively_correlated_clusters_widen_the_interval():
    """Siblings that agree carry less information than their count suggests.

    Same data as the hand computation: the naive SE is sqrt(1/3)/2 = 0.2887,
    treating four agreeing observations as four draws. Clustering knows there
    were really two.
    """
    values = np.array([1.0, 1.0, 0.0, 0.0])
    clusters = np.array(["A", "A", "B", "B"])
    result = clustered_mean_test(values, clusters)
    assert result.naive_se == pytest.approx(np.sqrt(1 / 3) / 2)
    assert result.se > result.naive_se
    assert result.design_effect == pytest.approx(0.5 / (np.sqrt(1 / 3) / 2))


def test_mutually_exclusive_clusters_can_narrow_the_interval():
    """A golfer field in miniature: exactly one yes per event, always.

    Every cluster total is identical, so there is no between-cluster variance
    and the clustered SE is zero. Clustering is not a synonym for "wider" --
    it is a synonym for "correct", and negatively correlated siblings genuinely
    pin the mean down harder than independent draws would.
    """
    values = np.array([1.0, 0.0, 1.0, 0.0])
    clusters = np.array(["A", "A", "B", "B"])
    se, n_clusters = cluster_robust_se(values, clusters)
    assert n_clusters == 2
    assert se == pytest.approx(0.0)


def test_one_cluster_reports_no_information_rather_than_certainty():
    values = np.array([1.0, 0.0, 1.0])
    clusters = np.array(["A", "A", "A"])
    se, n_clusters = cluster_robust_se(values, clusters)
    assert n_clusters == 1
    assert se == 0.0

    # And the test built on it must not call that significant.
    result = clustered_mean_test(values, clusters)
    assert result.p_value == 1.0


def test_singleton_clusters_reproduce_the_independent_standard_error():
    """With one observation per cluster there is no correlation to find, so the
    cluster-robust SE should collapse back to the ordinary one (up to the
    finite-sample corrections, which differ by G/(G-1) vs n/(n-1) -- identical
    here since G = n)."""
    rng = np.random.default_rng(0)
    values = rng.normal(size=200)
    clusters = np.arange(200)
    se, n_clusters = cluster_robust_se(values, clusters)
    naive = np.std(values, ddof=1) / np.sqrt(200)
    assert n_clusters == 200
    assert se == pytest.approx(naive, rel=1e-12)


def test_cluster_se_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        cluster_robust_se(np.array([1.0, 2.0]), np.array(["A"]))


def test_clustered_mean_reports_the_point_estimate_unchanged():
    """Clustering changes the uncertainty, never the estimate."""
    values = np.array([1.0, 0.0, 1.0, 1.0, 0.0])
    clusters = np.array(["A", "A", "B", "B", "C"])
    result = clustered_mean_test(values, clusters)
    assert result.mean == pytest.approx(0.6)
    assert result.n == 5
    assert result.n_clusters == 3


# --------------------------------------------------------------------------
# Benjamini-Hochberg
# --------------------------------------------------------------------------

# The worked example from Benjamini & Hochberg (1995), which rejects exactly
# the first four hypotheses at alpha = 0.05.
BH_1995 = np.array(
    [
        0.0001, 0.0004, 0.0019, 0.0095, 0.0201,
        0.0278, 0.0298, 0.0344, 0.0459, 0.3240,
        0.4262, 0.5719, 0.6528, 0.7590, 1.0000,
    ]
)


def test_bh_matches_the_1995_paper():
    rejected, _ = benjamini_hochberg(BH_1995, alpha=0.05)
    assert rejected.sum() == 4
    assert rejected[:4].all()
    assert not rejected[4:].any()


def test_bh_rejects_less_than_the_uncorrected_sweep():
    """The entire point. Nine of these p-values are below 0.05 on their own;
    only four survive the correction."""
    rejected, _ = benjamini_hochberg(BH_1995, alpha=0.05)
    uncorrected = BH_1995 < 0.05
    assert uncorrected.sum() == 9
    assert rejected.sum() < uncorrected.sum()
    # Anything BH rejects, the uncorrected sweep would have too.
    assert (uncorrected | ~rejected).all()


def test_bh_q_values_are_monotone_in_p():
    _, q = benjamini_hochberg(BH_1995, alpha=0.05)
    assert np.all(np.diff(q) >= -1e-12)
    assert np.all(q >= BH_1995 - 1e-12)  # correction can only inflate
    assert np.all(q <= 1.0)


def test_bh_is_order_invariant():
    """Buckets arrive in price order, not p-value order; shuffling the input
    must permute the output identically and nothing else."""
    rng = np.random.default_rng(7)
    permutation = rng.permutation(BH_1995.size)
    rejected, q = benjamini_hochberg(BH_1995, alpha=0.05)
    shuffled_rejected, shuffled_q = benjamini_hochberg(BH_1995[permutation], alpha=0.05)
    assert np.array_equal(shuffled_rejected, rejected[permutation])
    assert shuffled_q == pytest.approx(q[permutation])


def test_bh_ignores_degenerate_buckets():
    p_values = np.array([0.001, np.nan, 0.9])
    rejected, q = benjamini_hochberg(p_values, alpha=0.05)
    assert rejected[0]
    assert not rejected[1]
    assert np.isnan(q[1])


def test_bh_on_pure_noise_rejects_almost_nothing():
    """Under a true null, uncorrected testing finds ~5% of buckets
    'significant'. That false-positive rate is what the correction exists to
    remove."""
    rng = np.random.default_rng(11)
    p_values = rng.uniform(size=200)  # H0 true everywhere: p ~ Uniform(0,1)
    rejected, _ = benjamini_hochberg(p_values, alpha=0.05)
    assert (p_values < 0.05).sum() >= 5
    assert rejected.sum() == 0


# --------------------------------------------------------------------------
# Cluster bootstrap
# --------------------------------------------------------------------------


def _one_column(outcomes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Shape a single group of outcomes as the (numerator, denominator) pair
    the ratio bootstrap expects."""
    return outcomes.reshape(-1, 1), np.ones((outcomes.size, 1))


def test_bootstrap_is_reproducible_under_a_fixed_seed():
    rng = np.random.default_rng(3)
    outcomes = rng.integers(0, 2, size=400).astype(float)
    clusters = np.repeat(np.arange(100), 4)
    numerator, denominator = _one_column(outcomes)

    first = cluster_bootstrap_ratio_ci(numerator, denominator, clusters, 200, seed=42)
    second = cluster_bootstrap_ratio_ci(numerator, denominator, clusters, 200, seed=42)
    assert first[0] == pytest.approx(second[0])
    assert first[1] == pytest.approx(second[1])


def test_bootstrap_interval_brackets_the_point_estimate():
    rng = np.random.default_rng(5)
    outcomes = rng.integers(0, 2, size=600).astype(float)
    clusters = np.repeat(np.arange(150), 4)
    numerator, denominator = _one_column(outcomes)

    low, high = cluster_bootstrap_ratio_ci(numerator, denominator, clusters, 500, seed=1)
    assert low[0] <= outcomes.mean() <= high[0]


def test_bootstrap_is_wider_when_siblings_are_correlated():
    """Same 600 observations, same yes-rate, different dependence structure.

    In the correlated version every event's four contracts share one outcome,
    so there are really 150 draws, not 600. The interval has to notice.
    """
    rng = np.random.default_rng(9)
    per_event = rng.integers(0, 2, size=150).astype(float)

    correlated = np.repeat(per_event, 4)
    clusters = np.repeat(np.arange(150), 4)
    independent = correlated.copy()
    rng.shuffle(independent)  # same values, but now spread across events

    def width(values, cluster_labels):
        numerator, denominator = _one_column(values)
        low, high = cluster_bootstrap_ratio_ci(
            numerator, denominator, cluster_labels, 1_000, seed=17
        )
        return high[0] - low[0]

    # Singleton clusters == the naive independent bootstrap.
    naive_width = width(correlated, np.arange(600))
    clustered_width = width(correlated, clusters)
    assert clustered_width > naive_width * 1.5


def test_bootstrap_handles_a_bucket_no_replicate_can_miss():
    """Two columns, the second holding a single event. Replicates that draw it
    zero times give 0/0 there and are dropped from that column alone; the first
    column's interval must be unaffected."""
    outcomes = np.array([1.0, 0.0, 1.0, 0.0, 1.0, 1.0])
    clusters = np.array(["A", "A", "B", "B", "C", "C"])
    membership = np.array(
        [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]
    )
    numerator = membership * outcomes[:, None]

    low, high = cluster_bootstrap_ratio_ci(numerator, membership, clusters, 300, seed=2)
    assert 0.0 <= low[0] <= high[0] <= 1.0
    # Column 1 only ever sees event C, whose outcomes are both 1.
    assert high[1] == pytest.approx(1.0)


def test_bootstrap_rejects_a_shape_mismatch():
    with pytest.raises(ValueError):
        cluster_bootstrap_ratio_ci(
            np.ones((4, 2)), np.ones((4, 3)), np.arange(4), 10, seed=0
        )
