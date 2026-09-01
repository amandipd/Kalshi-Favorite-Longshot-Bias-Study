"""Tests for src/analysis/segmentation.py.

Segmentation is where a calibration study most easily invents a finding:
slice ten ways, report the best slice. So the assertions here are mostly about
the guards rather than the arithmetic --

  * the correction family spans every segment, so adding segments costs
    something instead of being free;
  * a bucket with too few events is reported but never tested, and never
    dilutes the family;
  * lifetime bins are quantiles while price bins stay fixed-width, because one
    is a nuisance dimension and the other is the hypothesis.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.analysis.segmentation import (
    bias_by_category,
    bias_by_lifetime,
    lifetime_hours,
    segment_calibration,
)
# pytest puts tests/ on sys.path (no __init__.py), so the sibling module's
# Config builder is importable rather than duplicated here.
from test_calibration import make_config

pytest.importorskip("pyarrow")

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def segmented_frame(
    n_per_segment: int = 4_000,
    segments: tuple[str, ...] = ("alpha", "beta"),
    biases: tuple[float, ...] = (0.0, 0.0),
    seed: int = 100,
) -> pd.DataFrame:
    """Contracts in several segments, each with its own planted bias."""
    rng = np.random.default_rng(seed)
    frames = []
    for index, (segment, bias) in enumerate(zip(segments, biases)):
        prices = rng.uniform(0.05, 0.95, size=n_per_segment)
        outcomes = rng.binomial(1, np.clip(prices + bias, 0.001, 0.999))
        frames.append(
            pd.DataFrame(
                {
                    "implied_price": prices,
                    "outcome": outcomes,
                    "event_ticker": [
                        f"{segment}-E{i}" for i in range(n_per_segment)
                    ],
                    "category": segment,
                    "open_ts": EPOCH,
                    # Lifetimes must vary or there is nothing to bucket.
                    "close_ts": [
                        EPOCH + timedelta(hours=float(h))
                        for h in rng.uniform(2, 200, size=n_per_segment)
                    ],
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------
# The correction family
# --------------------------------------------------------------------------


def test_every_segment_is_present_and_every_contract_accounted_for():
    df = segmented_frame()
    table = bias_by_category(df, config=make_config())
    assert set(table["segment"]) == {"alpha", "beta"}
    assert table["n"].sum() == len(df)


def test_the_correction_family_spans_segments():
    """Every segment's tests go into ONE family: q must equal BH over the
    pooled p-values, not BH run inside each segment.

    Note what is deliberately NOT asserted -- that pooling always raises the
    bar. Benjamini-Hochberg is a step-up procedure, so a segment carrying
    strong signal raises the others' ranks faster than it raises m, and a
    marginal test can come out with a SMALLER q pooled than alone. That is
    correct FDR control, not leniency: the guarantee is about the proportion
    of false discoveries in the family, not about each q individually. So the
    test pins the family membership, which is the actual decision.
    """
    from src.analysis.calibration import calibration_table
    from src.analysis.statistics import benjamini_hochberg

    df = segmented_frame(biases=(-0.04, 0.0))
    config = make_config()
    family = bias_by_category(df, config=config)

    pooled = []
    for segment in sorted(df["category"].unique()):
        subset = df[df["category"] == segment]
        pooled.append(calibration_table(subset, n_buckets=5, config=config)["p_value"])
    _, expected_q = benjamini_hochberg(pd.concat(pooled).to_numpy(), 0.05)

    assert family["q_value"].to_numpy() == pytest.approx(expected_q, nan_ok=True)


def test_per_segment_correction_would_give_a_different_answer():
    """Guards against the family silently degenerating into per-segment
    correction, which would look identical on a single-segment dataset."""
    from src.analysis.calibration import calibration_table
    from src.analysis.statistics import benjamini_hochberg

    df = segmented_frame(biases=(-0.04, 0.0))
    config = make_config()
    family = bias_by_category(df, config=config)

    per_segment = []
    for segment in sorted(df["category"].unique()):
        subset = df[df["category"] == segment]
        table = calibration_table(subset, n_buckets=5, config=config)
        _, q = benjamini_hochberg(table["p_value"].to_numpy(), 0.05)
        per_segment.append(q)
    separate = np.concatenate(per_segment)

    assert not np.allclose(family["q_value"].to_numpy(), separate, equal_nan=True)


def test_a_real_effect_still_survives_the_wider_family():
    """The correction must cost something without costing everything: a
    planted 6-point bias in both segments stays significant."""
    df = segmented_frame(biases=(-0.06, -0.06))
    table = bias_by_category(df, config=make_config())
    assert table["significant"].sum() >= 6


def test_a_calibrated_segment_is_not_flagged():
    df = segmented_frame(biases=(0.0, 0.0), seed=101)
    table = bias_by_category(df, config=make_config())
    assert not table["significant"].any()


# --------------------------------------------------------------------------
# The power floor
# --------------------------------------------------------------------------


def test_a_thin_segment_is_reported_but_never_tested():
    """Politics in miniature: a segment with a handful of events keeps its
    point estimate -- deleting it would hide that the category exists -- but
    carries no q-value and cannot be called significant."""
    big = segmented_frame(n_per_segment=4_000, segments=("sports",), biases=(-0.05,))
    tiny = segmented_frame(
        n_per_segment=20, segments=("politics",), biases=(-0.30,), seed=7
    )
    table = bias_by_category(
        pd.concat([big, tiny], ignore_index=True), config=make_config()
    )

    thin = table[table["segment"] == "politics"]
    assert len(thin) > 0  # reported
    assert thin["underpowered"].all()
    assert thin["q_value"].isna().all()
    assert not thin["significant"].any()
    assert thin["bias"].notna().all()  # the estimate survives


def test_underpowered_rows_do_not_dilute_the_family():
    """A thin segment's untestable cells must not enlarge m and weaken every
    real test. The well-powered segment's q-values must be identical with and
    without the thin segment present."""
    big = segmented_frame(n_per_segment=4_000, segments=("sports",), biases=(-0.05,))
    tiny = segmented_frame(
        n_per_segment=20, segments=("politics",), biases=(-0.30,), seed=7
    )
    config = make_config()

    alone = bias_by_category(big, config=config)
    with_thin = bias_by_category(
        pd.concat([big, tiny], ignore_index=True), config=config
    )
    joined = with_thin[with_thin["segment"] == "sports"]["q_value"].to_numpy()
    assert joined == pytest.approx(alone["q_value"].to_numpy(), nan_ok=True)


def test_the_power_floor_is_configurable():
    df = segmented_frame(n_per_segment=200, segments=("alpha",), biases=(0.0,))
    permissive = bias_by_category(df, config=make_config(min_events_per_bucket=1))
    strict = bias_by_category(df, config=make_config(min_events_per_bucket=10_000))
    assert not permissive["underpowered"].any()
    assert strict["underpowered"].all()


# --------------------------------------------------------------------------
# Lifetime
# --------------------------------------------------------------------------


def test_lifetime_hours_is_close_minus_open():
    df = pd.DataFrame(
        {
            "open_ts": [EPOCH, EPOCH],
            "close_ts": [EPOCH + timedelta(hours=36), EPOCH + timedelta(minutes=90)],
        }
    )
    assert lifetime_hours(df).tolist() == pytest.approx([36.0, 1.5])


def test_lifetime_buckets_are_quantiles_not_equal_width():
    """A two-year tail would put almost everything in the first equal-width
    bin. Quantiles keep every lifetime bucket populated enough to carry a
    clustered interval."""
    rng = np.random.default_rng(30)
    n = 8_000
    # Heavily skewed: most markets live a day, a few live two years.
    hours = np.concatenate([rng.uniform(1, 30, size=n - 50), rng.uniform(5_000, 17_000, size=50)])
    prices = rng.uniform(0.05, 0.95, size=n)
    df = pd.DataFrame(
        {
            "implied_price": prices,
            "outcome": rng.binomial(1, prices),
            "event_ticker": [f"E{i}" for i in range(n)],
            "open_ts": EPOCH,
            "close_ts": [EPOCH + timedelta(hours=float(h)) for h in hours],
        }
    )

    table = bias_by_lifetime(df, n_time_buckets=4, config=make_config())
    counts = table.groupby("segment")["n"].sum()
    assert len(counts) == 4
    # Equal-width bins would leave three near-empty; quantiles must not.
    assert counts.min() > n * 0.10


def test_lifetime_labels_carry_the_hour_range():
    df = segmented_frame(n_per_segment=2_000, segments=("a", "b"), biases=(0.0, 0.0))
    table = bias_by_lifetime(df, n_time_buckets=2, config=make_config())
    assert all("h" in str(s) for s in table["segment"].unique())


def test_lifetime_needs_the_timestamp_columns():
    df = segmented_frame(n_per_segment=100).drop(columns=["open_ts"])
    with pytest.raises(KeyError, match="open_ts"):
        bias_by_lifetime(df, config=make_config())


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


def test_segmentation_is_reproducible():
    df = segmented_frame(biases=(-0.03, 0.0))
    config = make_config()
    pd.testing.assert_frame_equal(
        bias_by_category(df, config=config), bias_by_category(df, config=config)
    )


def test_a_missing_segment_column_raises():
    with pytest.raises(KeyError, match="venue"):
        segment_calibration(segmented_frame(), "venue", config=make_config())
