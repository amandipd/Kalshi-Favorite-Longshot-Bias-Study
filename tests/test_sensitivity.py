"""Tests for src/strategy/sensitivity.py.

The sensitivity sweeps exist to answer one question: does the headline result
depend on the exact threshold chosen, or does it hold across a range? These
tests check the *mechanics* of the sweep (it reuses the real backtest
functions unmodified, it varies exactly the parameter it claims to, results
are reproducible) on small synthetic data, rather than re-asserting the real
dataset's numbers -- those are recorded once, by hand, in
reports/calibration-study.md and docs/limitations.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.strategy.sensitivity import (
    sweep_kelly_fraction,
    sweep_min_net_edge,
    sweep_train_fraction,
)
from test_backtest import biased_frame, strategy_config

pytest.importorskip("pyarrow")


def test_min_net_edge_sweep_drops_buckets_monotonically():
    """Raising the bar for net edge can only remove buckets, never add them."""
    config = strategy_config()
    df = biased_frame()
    sweep = sweep_min_net_edge(df, thresholds=(0.0, 0.02, 0.05, 0.1), config=config)

    assert list(sweep["threshold"]) == [0.0, 0.02, 0.05, 0.1]
    assert (sweep["buckets_traded"].diff().dropna() <= 0).all()


def test_min_net_edge_sweep_eventually_trades_nothing():
    """A threshold above every bucket's net edge must halt trading entirely,
    not merely shrink it -- the sweep has to actually reach the boundary."""
    config = strategy_config()
    sweep = sweep_min_net_edge(
        biased_frame(), thresholds=(0.0, 1.0), config=config
    )
    last = sweep.iloc[-1]
    assert last["buckets_traded"] == 0
    assert last["trades"] == 0
    assert last["roi"] == 0.0
    assert not last["falsifies"]


def test_train_fraction_sweep_varies_the_split_and_nothing_else():
    """Different train fractions must produce different splits, and the
    reported metrics must come from `backtest_bias_strategy` itself -- checked
    by cross-referencing one sweep row against a direct call."""
    from src.strategy.backtest import backtest_anti_bias_control, backtest_bias_strategy

    config = strategy_config()
    df = biased_frame()
    sweep = sweep_train_fraction(df, thresholds := (0.5, 0.7), config=config)

    for fraction in thresholds:
        varied = config.model_copy(
            update={"strategy": config.strategy.model_copy(update={"train_fraction": fraction})}
        )
        _, main, _, _ = backtest_bias_strategy(df, config=varied)
        _, control, _, _ = backtest_anti_bias_control(df, config=varied)
        row = sweep[sweep["threshold"] == fraction].iloc[0]
        assert row["roi"] == pytest.approx(main["roi"])
        assert row["control_roi"] == pytest.approx(control["roi"])


def test_kelly_fraction_sweep_roi_is_nearly_scale_invariant():
    """ROI is profit per dollar deployed. Kelly sizing rescales every stake by
    the same multiplier before the daily budget clips it, so ROI should be far
    less sensitive to this sweep than to the other two -- a check on the
    sizing rule's role, not a claim that it is exactly flat (the budget can
    bind differently at different scales)."""
    config = strategy_config()
    df = biased_frame(n=20_000)  # enough volume that the budget binds similarly
    sweep = sweep_kelly_fraction(df, thresholds := (0.25, 0.5, 0.75, 1.0), config=config)

    roi = sweep["roi"].to_numpy()
    assert roi.max() - roi.min() < 0.5 * abs(roi.mean())


def test_kelly_fraction_sweep_does_not_change_which_buckets_trade():
    """The Kelly multiplier scales stakes; it cannot change which buckets are
    statistically or economically significant, since that decision is made
    entirely from `min_net_edge` and the training data."""
    config = strategy_config()
    sweep = sweep_kelly_fraction(
        biased_frame(), fractions=(0.1, 0.5, 1.0), config=config
    )
    assert sweep["buckets_traded"].nunique() == 1


def test_sweeps_are_reproducible():
    config = strategy_config()
    df = biased_frame()
    a = sweep_min_net_edge(df, config=config)
    b = sweep_min_net_edge(df, config=config)
    pd.testing.assert_frame_equal(a, b)


def test_sweep_columns_match_a_direct_backtest_call():
    """The sweep must not compute its own version of ROI or breakeven
    slippage -- it has to be reading them from `summarise`, so a sweep row can
    never silently disagree with `make analyze`'s printed report."""
    from src.strategy.backtest import backtest_bias_strategy

    config = strategy_config()
    df = biased_frame()
    _, main, _, rules = backtest_bias_strategy(df, config=config)
    sweep = sweep_min_net_edge(df, thresholds=(0.0,), config=config)
    row = sweep.iloc[0]

    assert row["roi"] == pytest.approx(main["roi"])
    assert row["trades"] == main["trades"]
    assert row["breakeven_slippage"] == pytest.approx(main["breakeven_slippage"])
    assert row["buckets_traded"] == int(rules["trade"].sum())
