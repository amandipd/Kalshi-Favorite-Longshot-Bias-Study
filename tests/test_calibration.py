"""Tests for src/analysis/calibration.py.

The Murphy identity is the load-bearing assertion here. It is checked as an
*exact* equality against `binned_brier` rather than an approximate one against
the raw score, because the approximate version passes even when a term is
subtly wrong -- and the size of the gap between the two is itself a reported
quantity (`binning_residual`), not a tolerance to hide inside.

Alongside it, three synthetic datasets with a known answer:

    perfectly calibrated    reliability ~ 0, and no bucket significant.
    base-rate forecaster    resolution ~ 0, brier == uncertainty. Perfectly
                            calibrated and perfectly useless -- the case that
                            shows why calibration alone is not skill.
    planted bias            a known 15-point gap that the table must find.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.calibration import (
    assign_buckets,
    brier_decomposition,
    brier_score,
    calibration_table,
    logistic_calibration,
)
from src.config import AnalysisConfig, Config

pytest.importorskip("pyarrow")


def make_config(**overrides) -> Config:
    """A Config carrying only what the analysis layer reads.

    The ingest and clean sections are irrelevant here but Config validates as a
    whole, so they are filled with throwaway values. Reps are low because these
    tests assert an interval exists and behaves, not its third decimal.
    """
    from src.config import CleanConfig, DateRange, IngestConfig, RetryConfig, TradesConfig
    from datetime import date

    settings = {
        "n_buckets": 10,
        "confidence": 0.95,
        "cluster_on": "event_ticker",
        "bootstrap_reps": 300,
        "bootstrap_seed": 20260901,
        "fdr_alpha": 0.05,
        "segment_n_buckets": 5,
        "min_events_per_bucket": 30,
    }
    settings.update(overrides)

    return Config(
        date_range=DateRange(start=date(2026, 1, 1), end=date(2026, 6, 6)),
        categories=["Economics"],
        kalshi_base_url="https://example.test",
        polymarket_base_url="https://poly.test",
        rate_limit_per_second=1000.0,
        retry=RetryConfig(
            max_attempts=3,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.02,
            jitter_seconds=0.0,
            timeout_seconds=5.0,
        ),
        ingest=IngestConfig(
            raw_dir="raw",
            top_n_series_per_category=2,
            page_limit=2,
            subdaily_frequencies=frozenset(),
            trades=TradesConfig(trades_dir="trades", horizons_hours=[1.0]),
        ),
        clean=CleanConfig(
            price_method="horizon_trade",
            price_horizon_hours=1.0,
            interim_path="interim.parquet",
            processed_path="processed.parquet",
        ),
        analysis=AnalysisConfig(**settings),
    )


def frame(prices, outcomes, events=None) -> pd.DataFrame:
    prices = np.asarray(prices, dtype=float)
    if events is None:
        # One event per contract: independent observations, so clustering is a
        # no-op and the test is about the estimator, not the correlation.
        events = [f"E{i}" for i in range(prices.size)]
    return pd.DataFrame(
        {
            "implied_price": prices,
            "outcome": np.asarray(outcomes, dtype=int),
            "event_ticker": list(events),
        }
    )


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price,expected",
    [
        (0.0, 0),
        (0.05, 0),
        (0.0999, 0),
        (0.1, 1),
        (0.5, 5),
        (0.99, 9),
        (1.0, 9),  # the top edge closes rather than opening an 11th bucket
    ],
)
def test_bucket_edges(price, expected):
    assert assign_buckets(np.array([price]), 10)[0] == expected


def test_bucket_count_is_honoured():
    prices = np.linspace(0.0, 1.0, 101)
    assert set(assign_buckets(prices, 4)) == {0, 1, 2, 3}
    assert set(assign_buckets(prices, 20)) == set(range(20))


def test_prices_outside_the_unit_interval_are_rejected():
    with pytest.raises(ValueError):
        assign_buckets(np.array([0.5, 1.4]), 10)


# --------------------------------------------------------------------------
# Brier score and its decomposition
# --------------------------------------------------------------------------


def test_brier_score_by_hand():
    """prices [0.1, 0.3, 0.7, 0.9] against outcomes [0, 1, 0, 1]:
    (0.01 + 0.49 + 0.49 + 0.01) / 4 = 0.25."""
    df = frame([0.1, 0.3, 0.7, 0.9], [0, 1, 0, 1])
    assert brier_score(df) == pytest.approx(0.25)


def test_brier_of_a_perfect_forecaster_is_zero():
    df = frame([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0])
    assert brier_score(df) == pytest.approx(0.0)


def test_brier_of_the_worst_possible_forecaster_is_one():
    df = frame([1.0, 0.0], [0, 1])
    assert brier_score(df) == pytest.approx(1.0)


def test_decomposition_by_hand():
    """Same four contracts, two buckets, every term worked out longhand.

        buckets: [0.1, 0.3] -> 0 ; [0.7, 0.9] -> 1
        base rate o_bar = 0.5             uncertainty = 0.25
        bucket 0: n=2, f=0.2, o=0.5       bucket 1: n=2, f=0.8, o=0.5
        reliability = (2*0.09 + 2*0.09)/4 = 0.09
        resolution  = (2*0    + 2*0   )/4 = 0
        binned      = 0.09 - 0 + 0.25     = 0.34
        raw brier   = 0.25, so the binning residual is -0.09
    """
    df = frame([0.1, 0.3, 0.7, 0.9], [0, 1, 0, 1])
    result = brier_decomposition(df, n_buckets=2, config=make_config())

    assert result["base_rate"] == pytest.approx(0.5)
    assert result["uncertainty"] == pytest.approx(0.25)
    assert result["reliability"] == pytest.approx(0.09)
    assert result["resolution"] == pytest.approx(0.0)
    assert result["binned_brier"] == pytest.approx(0.34)
    assert result["brier"] == pytest.approx(0.25)
    assert result["binning_residual"] == pytest.approx(-0.09)


def test_murphy_identity_holds_exactly_on_the_binned_score():
    """reliability - resolution + uncertainty == binned_brier, to machine
    precision, on data with no structure to help it along."""
    rng = np.random.default_rng(42)
    prices = rng.uniform(size=5_000)
    outcomes = rng.binomial(1, prices)
    df = frame(prices, outcomes)

    result = brier_decomposition(df, config=make_config())
    identity = result["reliability"] - result["resolution"] + result["uncertainty"]
    assert identity == pytest.approx(result["binned_brier"], abs=1e-12)


@pytest.mark.parametrize("n_buckets", [2, 5, 10, 20, 50])
def test_murphy_identity_holds_at_every_bucket_count(n_buckets):
    rng = np.random.default_rng(n_buckets)
    prices = rng.uniform(size=2_000)
    outcomes = rng.binomial(1, prices)
    result = brier_decomposition(frame(prices, outcomes), n_buckets=n_buckets, config=make_config())
    identity = result["reliability"] - result["resolution"] + result["uncertainty"]
    assert identity == pytest.approx(result["binned_brier"], abs=1e-12)


def test_finer_buckets_shrink_the_binning_residual():
    """The residual is within-bucket price variation the decomposition cannot
    see. Narrower buckets leave less of it, which is what makes the residual a
    diagnostic of whether the bucketing is fine enough rather than a nuisance.
    """
    rng = np.random.default_rng(3)
    prices = rng.uniform(size=20_000)
    outcomes = rng.binomial(1, prices)
    df = frame(prices, outcomes)
    config = make_config()

    coarse = abs(brier_decomposition(df, n_buckets=2, config=config)["binning_residual"])
    fine = abs(brier_decomposition(df, n_buckets=50, config=config)["binning_residual"])
    assert fine < coarse


def test_a_perfectly_calibrated_forecaster_has_near_zero_reliability():
    rng = np.random.default_rng(1)
    prices = rng.uniform(size=50_000)
    outcomes = rng.binomial(1, prices)  # outcomes generated AT the price
    result = brier_decomposition(frame(prices, outcomes), config=make_config())
    assert result["reliability"] < 1e-3


def test_the_base_rate_forecaster_is_calibrated_and_useless():
    """Answering with the base rate on every question scores exactly the
    uncertainty: reliability 0 (it is right on average) and resolution 0 (it
    distinguishes nothing). This is why 'well calibrated' is not 'useful'."""
    rng = np.random.default_rng(2)
    outcomes = rng.binomial(1, 0.4, size=20_000)
    base_rate = outcomes.mean()
    df = frame(np.full(outcomes.size, base_rate), outcomes)

    result = brier_decomposition(df, config=make_config())
    assert result["reliability"] == pytest.approx(0.0, abs=1e-12)
    assert result["resolution"] == pytest.approx(0.0, abs=1e-12)
    assert result["brier"] == pytest.approx(result["uncertainty"], abs=1e-12)


def test_decomposition_rejects_an_empty_frame():
    with pytest.raises(ValueError):
        brier_decomposition(frame([], []), config=make_config())


# --------------------------------------------------------------------------
# Calibration table
# --------------------------------------------------------------------------


def test_table_accounts_for_every_contract():
    rng = np.random.default_rng(4)
    prices = rng.uniform(size=3_000)
    outcomes = rng.binomial(1, prices)
    table = calibration_table(frame(prices, outcomes), config=make_config())

    assert table["n"].sum() == 3_000
    assert list(table["bucket"]) == sorted(table["bucket"])
    assert (table["bucket_low"] < table["bucket_high"]).all()


def test_bias_is_realized_minus_predicted():
    table = calibration_table(
        frame([0.05, 0.05, 0.05, 0.05], [1, 0, 0, 0]), config=make_config()
    )
    row = table.iloc[0]
    assert row["mean_price"] == pytest.approx(0.05)
    assert row["realized_freq"] == pytest.approx(0.25)
    assert row["bias"] == pytest.approx(0.20)


def test_intervals_bracket_the_realized_frequency():
    rng = np.random.default_rng(6)
    prices = rng.uniform(size=4_000)
    outcomes = rng.binomial(1, prices)
    table = calibration_table(frame(prices, outcomes), config=make_config())

    assert (table["ci_low"] <= table["realized_freq"] + 1e-9).all()
    assert (table["ci_high"] >= table["realized_freq"] - 1e-9).all()
    assert (table["wilson_low"] <= table["realized_freq"]).all()
    assert (table["wilson_high"] >= table["realized_freq"]).all()


def test_a_calibrated_market_produces_no_significant_bucket():
    """The false-positive check. Outcomes drawn at the price mean every bucket
    is unbiased by construction, so anything flagged here is the machinery
    inventing a finding."""
    rng = np.random.default_rng(8)
    prices = rng.uniform(size=20_000)
    outcomes = rng.binomial(1, prices)
    table = calibration_table(frame(prices, outcomes), config=make_config())

    assert not table["significant"].any()
    assert (table["bias"].abs() < 0.05).all()


def test_a_planted_bias_is_detected():
    """Longshots that resolve yes 15 points less often than they are priced.
    A real effect this size in 8,000 independent contracts must survive both
    the clustering and the correction."""
    rng = np.random.default_rng(10)
    prices = rng.uniform(0.30, 0.40, size=8_000)
    outcomes = rng.binomial(1, prices - 0.15)
    table = calibration_table(frame(prices, outcomes), config=make_config())

    row = table[table["bucket"] == 3].iloc[0]
    assert row["bias"] == pytest.approx(-0.15, abs=0.02)
    assert row["significant"]
    assert row["q_value"] < 0.05


def test_correlated_siblings_widen_the_interval_they_would_have_had():
    """The same contracts, clustered honestly and clustered not at all.

    Each event here is a four-contract field whose members share one outcome,
    so the naive interval is counting 4,000 draws where there are 1,000. The
    clustered interval must be materially wider, and the design effect must
    say so.
    """
    rng = np.random.default_rng(12)
    event_prices = rng.uniform(0.4, 0.5, size=1_000)
    event_outcomes = rng.binomial(1, event_prices)

    prices = np.repeat(event_prices, 4)
    outcomes = np.repeat(event_outcomes, 4)
    events = np.repeat([f"E{i}" for i in range(1_000)], 4)

    clustered = calibration_table(frame(prices, outcomes, events), config=make_config())
    row = clustered.iloc[0]

    assert row["design_effect"] > 1.5
    clustered_width = row["ci_high"] - row["ci_low"]
    wilson_width = row["wilson_high"] - row["wilson_low"]
    assert clustered_width > wilson_width * 1.5


def test_table_is_reproducible():
    """A fixed seed means a rerun on identical data reproduces every interval,
    so a number in the report can always be traced back."""
    rng = np.random.default_rng(13)
    prices = rng.uniform(size=2_000)
    outcomes = rng.binomial(1, prices)
    df = frame(prices, outcomes)

    first = calibration_table(df, config=make_config())
    second = calibration_table(df, config=make_config())
    pd.testing.assert_frame_equal(first, second)


def test_table_requires_the_clustering_column():
    df = frame([0.2, 0.8], [0, 1]).drop(columns=["event_ticker"])
    with pytest.raises(KeyError, match="event_ticker"):
        calibration_table(df, config=make_config())


def test_table_rejects_an_empty_frame():
    with pytest.raises(ValueError):
        calibration_table(frame([], []), config=make_config())


# --------------------------------------------------------------------------
# Logistic calibration
# --------------------------------------------------------------------------


def logit(p):
    return np.log(p / (1 - p))


def inv_logit(x):
    return 1.0 / (1.0 + np.exp(-x))


def test_a_calibrated_market_fits_the_ideal_slope_and_intercept():
    """Outcomes drawn at the price: slope 1, intercept 0, and neither
    coefficient distinguishable from the ideal."""
    rng = np.random.default_rng(20)
    prices = rng.uniform(0.02, 0.98, size=30_000)
    outcomes = rng.binomial(1, prices)

    fit = logistic_calibration(frame(prices, outcomes), config=make_config())
    assert fit.slope == pytest.approx(1.0, abs=0.05)
    assert fit.intercept == pytest.approx(0.0, abs=0.05)
    assert fit.slope_p > 0.05
    assert fit.joint_p > 0.05
    assert fit.slope_ci[0] < 1.0 < fit.slope_ci[1]


def test_favorite_longshot_bias_produces_a_slope_above_one():
    """The direction that is easy to get backwards, pinned by construction.

    True log-odds are 1.3x the market's, so the truth is MORE extreme than the
    price: a market saying 5% is really ~2%. Longshots overpriced, favorites
    underpriced -- the hypothesis. The fitted slope must exceed 1.
    """
    rng = np.random.default_rng(21)
    prices = rng.uniform(0.02, 0.98, size=30_000)
    outcomes = rng.binomial(1, inv_logit(1.3 * logit(prices)))

    fit = logistic_calibration(frame(prices, outcomes), config=make_config())
    assert fit.slope == pytest.approx(1.3, abs=0.06)
    assert fit.slope > 1.0
    assert fit.slope_p < 1e-6


def test_the_reverse_bias_produces_a_slope_below_one():
    """The mirror image: truth LESS extreme than the price, so the market
    exaggerates and longshots are cheap. Slope below 1."""
    rng = np.random.default_rng(22)
    prices = rng.uniform(0.02, 0.98, size=30_000)
    outcomes = rng.binomial(1, inv_logit(0.7 * logit(prices)))

    fit = logistic_calibration(frame(prices, outcomes), config=make_config())
    assert fit.slope == pytest.approx(0.7, abs=0.06)
    assert fit.slope < 1.0
    assert fit.slope_p < 1e-6


def test_the_slope_is_tested_against_one_not_zero():
    """A slope of 1 is perfect calibration and must return a large p-value.
    Statsmodels' default test is against 0 -- 'does price predict outcome at
    all' -- which is overwhelmingly significant here and would be reported as
    a finding by anyone reading the wrong column."""
    rng = np.random.default_rng(23)
    prices = rng.uniform(0.02, 0.98, size=20_000)
    outcomes = rng.binomial(1, prices)

    fit = logistic_calibration(frame(prices, outcomes), config=make_config())
    assert fit.slope_p > 0.05  # against 1: calibrated
    naive_z = fit.slope / fit.slope_se  # against 0: trivially significant
    assert abs(naive_z) > 50


def test_logistic_clusters_its_standard_errors():
    """Four-contract events sharing one outcome: the clustered SE must exceed
    what an independence assumption would report."""
    import statsmodels.api as sm

    rng = np.random.default_rng(24)
    event_prices = rng.uniform(0.05, 0.95, size=2_000)
    event_outcomes = rng.binomial(1, event_prices)
    prices = np.repeat(event_prices, 4)
    outcomes = np.repeat(event_outcomes, 4)
    events = np.repeat([f"E{i}" for i in range(2_000)], 4)

    fit = logistic_calibration(frame(prices, outcomes, events), config=make_config())
    design = sm.add_constant(logit(prices))
    unclustered = sm.Logit(outcomes, design).fit(disp=0)

    assert fit.n_clusters == 2_000
    assert fit.slope_se > unclustered.bse[1] * 1.5


def test_logistic_refuses_a_price_at_the_boundary():
    """logit(0) and logit(1) are infinite. Clipping is a research decision, so
    the function raises rather than making it silently."""
    with pytest.raises(ValueError, match="exactly 0 or 1"):
        logistic_calibration(frame([0.0, 0.5, 0.9], [0, 1, 1]), config=make_config())
    with pytest.raises(ValueError, match="exactly 0 or 1"):
        logistic_calibration(frame([0.1, 0.5, 1.0], [0, 1, 1]), config=make_config())
