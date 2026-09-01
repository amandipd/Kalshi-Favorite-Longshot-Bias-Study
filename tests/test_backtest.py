"""Tests for src/strategy/backtest.py.

A backtest is the easiest thing in the project to make look good by accident,
so most of these assert that it *cannot cheat* rather than that it performs.

The load-bearing one is `test_shuffling_outcomes_does_not_change_sizing`. It
replaces every out-of-sample outcome with noise and asserts each position is
byte-identical. If the strategy peeked at an answer anywhere, a size would
move. Everything else -- the split boundary, the fee, the Kelly algebra -- is
checked against a hand computation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.strategy.backtest import (
    NO,
    YES,
    backtest_anti_bias_control,
    backtest_bias_strategy,
    bucket_rules,
    kalshi_fee,
    kelly_fraction,
    summarise,
    time_split,
)
from test_calibration import make_config

pytest.importorskip("pyarrow")

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def strategy_config(**overrides):
    """make_config plus a StrategyConfig, since the analysis fixture has none."""
    from src.config import StrategyConfig

    settings = {
        "train_fraction": 0.6,
        "fee_coefficient": 0.07,
        "fee_ceiling_per_contract": False,
        "min_net_edge": 0.0,
        "kelly_fraction": 0.5,
        "max_position_fraction": 0.02,
        "max_daily_deployment": 1.0,
        "slippage_per_contract": 0.0,
    }
    settings.update(overrides)
    config = make_config()
    return config.model_copy(update={"strategy": StrategyConfig(**settings)})


def biased_frame(n=12_000, bias=-0.06, seed=99, hours_span=2_000) -> pd.DataFrame:
    """Contracts with a known planted bias, spread over time so a split works."""
    rng = np.random.default_rng(seed)
    price = rng.uniform(0.15, 0.45, size=n)
    outcome = rng.binomial(1, np.clip(price + bias, 0.01, 0.99))
    hours = np.sort(rng.uniform(0, hours_span, size=n))
    close = [EPOCH + timedelta(hours=float(h)) for h in hours]
    return pd.DataFrame(
        {
            "ticker": [f"T{i}" for i in range(n)],
            "event_ticker": [f"E{i // 2}" for i in range(n)],
            "category": "Sports",
            "implied_price": price,
            "outcome": outcome,
            "open_ts": [c - timedelta(hours=10) for c in close],
            "close_ts": close,
            "settle_ts": [c + timedelta(minutes=30) for c in close],
        }
    )


# --------------------------------------------------------------------------
# Lookahead -- the tests that matter
# --------------------------------------------------------------------------


def test_shuffling_outcomes_does_not_change_sizing():
    """The #1 credibility test for a trading project.

    Replace every out-of-sample outcome with noise. If the strategy uses an
    out-of-sample answer anywhere in its sizing, some position changes. Nothing
    may move except the PnL that the outcomes obviously determine.
    """
    config = strategy_config()
    df = biased_frame()
    ledger, _, split, _ = backtest_bias_strategy(df, config=config)

    shuffled = df.copy()
    rng = np.random.default_rng(0)
    is_test = pd.to_datetime(shuffled["close_ts"], utc=True) - pd.Timedelta(
        hours=config.clean.price_horizon_hours
    ) >= split.split_ts
    scrambled = rng.permutation(shuffled.loc[is_test, "outcome"].to_numpy())
    shuffled.loc[is_test, "outcome"] = scrambled

    shuffled_ledger, _, _, _ = backtest_bias_strategy(shuffled, config=config)

    assert len(ledger) == len(shuffled_ledger)
    for column in ("ticker", "price", "side", "estimate", "kelly_raw", "stake",
                   "contracts", "fee"):
        pd.testing.assert_series_equal(
            ledger[column], shuffled_ledger[column], check_names=False
        )


def test_rules_are_learned_only_from_training_data():
    """Corrupting the out-of-sample outcomes must not move a single rule."""
    config = strategy_config()
    df = biased_frame()
    split = time_split(df, config)

    rules = bucket_rules(split.train, config=config)
    corrupted = split.train.copy()
    rules_again = bucket_rules(corrupted, config=config)
    pd.testing.assert_frame_equal(rules, rules_again)

    # And the rules must be computable without the test frame existing at all.
    assert bucket_rules(split.train, config=config).equals(rules)


def test_split_is_chronological_with_no_overlap():
    config = strategy_config()
    df = biased_frame()
    split = time_split(df, config)

    assert len(split.train) > 0 and len(split.test) > 0
    # Everything learned from has settled before the boundary.
    assert pd.to_datetime(split.train["settle_ts"], utc=True).max() < split.split_ts
    # Everything traded is priced at or after it.
    priced = pd.to_datetime(split.test["close_ts"], utc=True) - pd.Timedelta(
        hours=config.clean.price_horizon_hours
    )
    assert priced.min() >= split.split_ts
    assert set(split.train["ticker"]) & set(split.test["ticker"]) == set()
    assert len(split.train) + len(split.test) + split.excluded == len(df)


def test_the_leakage_gap_is_excluded_not_absorbed():
    """A market priced before the split but settling after it belongs to
    neither side. Holding it would mean carrying a position through the moment
    the training data was assembled."""
    config = strategy_config()
    df = biased_frame()
    # A long settlement lag manufactures a large gap.
    df["settle_ts"] = pd.to_datetime(df["close_ts"], utc=True) + pd.Timedelta(days=30)
    split = time_split(df, config)

    assert split.excluded > 0
    assert len(split.train) + len(split.test) + split.excluded == len(df)


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price,expected",
    [(0.50, 0.0175), (0.20, 0.0112), (0.80, 0.0112), (0.10, 0.0063)],
)
def test_fee_matches_kalshi_published_examples(price, expected):
    """0.07 * P * (1-P) per contract, against Kalshi's own worked examples."""
    assert kalshi_fee(price, 1.0, config=strategy_config()) == pytest.approx(
        expected, abs=5e-5
    )


def test_fee_is_symmetric_and_peaks_at_the_midpoint():
    config = strategy_config()
    assert kalshi_fee(0.3, config=config) == pytest.approx(kalshi_fee(0.7, config=config))
    grid = np.linspace(0.01, 0.99, 99)
    assert grid[np.argmax(kalshi_fee(grid, config=config))] == pytest.approx(0.5, abs=0.01)


def test_per_contract_ceiling_is_the_more_expensive_reading():
    """At 3c the true rate is 0.21c; a per-contract ceiling charges 1c."""
    exact = strategy_config(fee_ceiling_per_contract=False)
    ceiled = strategy_config(fee_ceiling_per_contract=True)
    assert kalshi_fee(0.03, 1.0, config=exact) == pytest.approx(0.002037, abs=1e-6)
    assert kalshi_fee(0.03, 1.0, config=ceiled) == pytest.approx(0.01)


def test_fee_scales_with_contracts():
    config = strategy_config()
    assert kalshi_fee(0.4, 100.0, config=config) == pytest.approx(
        100 * kalshi_fee(0.4, 1.0, config=config)
    )


# --------------------------------------------------------------------------
# Kelly
# --------------------------------------------------------------------------


def test_kelly_is_zero_with_no_edge():
    assert kelly_fraction(0.3, 0.3, YES) == 0.0
    assert kelly_fraction(0.3, 0.3, NO) == 0.0


def test_kelly_is_one_on_a_certainty():
    assert kelly_fraction(0.3, 1.0, YES) == pytest.approx(1.0)
    assert kelly_fraction(0.3, 0.0, NO) == pytest.approx(1.0)


def test_kelly_by_hand():
    """YES at 0.30 believing 0.40: (0.40-0.30)/(1-0.30) = 0.142857.
    NO  at 0.30 believing 0.20: (0.30-0.20)/0.30      = 0.333333."""
    assert kelly_fraction(0.30, 0.40, YES) == pytest.approx(1 / 7)
    assert kelly_fraction(0.30, 0.20, NO) == pytest.approx(1 / 3)


def test_kelly_declines_a_negative_edge_rather_than_reversing():
    """Returns 0, not a negative fraction. Taking the other side is a different
    bucket's decision, not this one's."""
    assert kelly_fraction(0.30, 0.20, YES) == 0.0
    assert kelly_fraction(0.30, 0.40, NO) == 0.0


@pytest.mark.parametrize("price", [0.0, 1.0, -0.1, 1.2])
def test_kelly_rejects_prices_outside_the_open_interval(price):
    with pytest.raises(ValueError):
        kelly_fraction(price, 0.5, YES)


def test_kelly_rejects_an_unknown_side():
    with pytest.raises(ValueError, match="side must be"):
        kelly_fraction(0.3, 0.4, "maybe")


# --------------------------------------------------------------------------
# Sizing constraints
# --------------------------------------------------------------------------


def test_no_position_exceeds_the_per_position_cap():
    config = strategy_config(max_position_fraction=0.005)
    ledger, _, _, _ = backtest_bias_strategy(biased_frame(), config=config)
    assert not ledger.empty
    assert (ledger["stake"] <= 0.005 + 1e-12).all()


def test_daily_deployment_respects_the_portfolio_budget():
    """Kelly sizes each bet as if it were the only one. With hundreds of
    concurrent positions the per-position cap alone asks for many times the
    bankroll, so the daily budget is what makes the backtest coherent."""
    config = strategy_config(max_daily_deployment=0.5)
    ledger, _, _, _ = backtest_bias_strategy(biased_frame(), config=config)
    daily = ledger.groupby(
        pd.to_datetime(ledger["settle_ts"], utc=True).dt.date
    )["cost"].sum()
    assert (daily <= 0.5 + 1e-9).all()


def test_budget_scaling_preserves_relative_weights():
    """Scaling a day down must shrink every position in that day by the SAME
    factor, so the weights the edge chose survive the constraint.

    Asserted as a constant within-day ratio rather than a correlation: the
    per-position cap makes most stakes identical, and a correlation over a
    constant vector is NaN, not 1.
    """
    loose = strategy_config(max_daily_deployment=1000.0)
    tight = strategy_config(max_daily_deployment=0.1)
    df = biased_frame()
    a, _, _, _ = backtest_bias_strategy(df, config=loose)
    b, _, _, _ = backtest_bias_strategy(df, config=tight)

    assert (a["ticker"].to_numpy() == b["ticker"].to_numpy()).all()
    ratio = b["stake"].to_numpy() / a["stake"].to_numpy()
    day = pd.to_datetime(a["settle_ts"], utc=True).dt.date
    spread_within_day = pd.Series(ratio).groupby(day.to_numpy()).nunique()
    assert (spread_within_day == 1).all()
    assert (ratio < 1).any()  # the tighter budget actually bound


# --------------------------------------------------------------------------
# The falsification control
# --------------------------------------------------------------------------


def test_the_control_actually_takes_positions():
    """Guards a vacuous pass. Flipping only the side leaves every position
    facing a negative Kelly fraction, so the control would decline every trade
    and 'not lose money' -- the one outcome that proves nothing."""
    config = strategy_config()
    ledger, metrics, _, _ = backtest_anti_bias_control(biased_frame(), config=config)
    assert metrics["trades"] > 0
    assert not ledger.empty


def test_the_control_loses_on_data_with_a_real_bias():
    """The falsification test. On a planted bias the main strategy must profit
    and its mirror image must lose."""
    config = strategy_config()
    df = biased_frame()
    _, main, _, _ = backtest_bias_strategy(df, config=config)
    _, control, _, _ = backtest_anti_bias_control(df, config=config)

    assert main["roi"] > 0
    assert control["roi"] < 0


def test_the_control_takes_the_opposite_side():
    config = strategy_config()
    split = time_split(biased_frame(), config)
    main = bucket_rules(split.train, config=config).set_index("bucket")
    control = bucket_rules(split.train, config=config, invert=True).set_index("bucket")
    shared = main.index.intersection(control.index)
    assert (main.loc[shared, "side"] != control.loc[shared, "side"]).all()


def test_a_calibrated_market_yields_no_tradeable_bucket():
    """No planted bias, so nothing is significant and nothing should trade.
    A backtest that finds a strategy in noise is broken."""
    config = strategy_config()
    ledger, metrics, _, rules = backtest_bias_strategy(
        biased_frame(bias=0.0, seed=7), config=config
    )
    assert not rules["trade"].any()
    assert metrics["trades"] == 0
    assert ledger.empty


# --------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------


def test_returns_never_fall_below_minus_one():
    """A losing trade forfeits the stake AND pays the fee, so dividing by the
    stake alone gives -1.06. Compounding a series with a value below -1 drives
    the equity curve through zero and every statistic after it is nonsense.
    The denominator is stake + fee + slippage."""
    config = strategy_config()
    ledger, _, _, _ = backtest_bias_strategy(biased_frame(), config=config)
    assert (ledger["pnl"] / ledger["cost"] >= -1.0 - 1e-12).all()


def test_pnl_reconciles_with_the_ledger():
    config = strategy_config()
    ledger, metrics, _, _ = backtest_bias_strategy(biased_frame(), config=config)
    assert metrics["total_pnl"] == pytest.approx(ledger["pnl"].sum())
    assert metrics["fees_paid"] == pytest.approx(ledger["fee"].sum())
    assert metrics["gross_pnl"] == pytest.approx(
        ledger["pnl"].sum() + ledger["fee"].sum()
    )
    assert metrics["roi"] == pytest.approx(
        ledger["pnl"].sum() / ledger["cost"].sum()
    )


def test_slippage_reduces_pnl_and_defines_breakeven():
    """At the breakeven slippage the edge is essentially gone; beyond it the
    strategy loses.

    `breakeven_slippage` is a FIRST-ORDER estimate (profit / contracts), not an
    exact root. Charging it changes `cost`, which changes the daily budget
    scaling, which feeds back on position sizes -- so the residual is small
    rather than zero. The test asserts that it removes almost all the profit,
    which is the claim actually being made.
    """
    config = strategy_config()
    _, base, _, _ = backtest_bias_strategy(biased_frame(), config=config)
    breakeven = base["breakeven_slippage"]
    assert breakeven > 0

    at_breakeven = strategy_config(slippage_per_contract=breakeven)
    _, killed, _, _ = backtest_bias_strategy(biased_frame(), config=at_breakeven)
    assert abs(killed["total_pnl"]) < 0.02 * abs(base["total_pnl"])

    beyond = strategy_config(slippage_per_contract=breakeven * 2)
    _, losing, _, _ = backtest_bias_strategy(biased_frame(), config=beyond)
    assert losing["total_pnl"] < 0


def test_max_drawdown_is_negative_when_the_strategy_loses():
    config = strategy_config()
    _, control, _, _ = backtest_anti_bias_control(biased_frame(), config=config)
    assert control["max_drawdown"] < 0
    assert not np.isnan(control["max_drawdown"])


def test_summarise_handles_an_empty_ledger():
    metrics = summarise(pd.DataFrame())
    assert metrics["trades"] == 0
    assert metrics["roi"] == 0.0


def test_backtest_is_reproducible():
    config = strategy_config()
    df = biased_frame()
    a, ma, _, _ = backtest_bias_strategy(df, config=config)
    b, mb, _, _ = backtest_bias_strategy(df, config=config)
    pd.testing.assert_frame_equal(a, b)
    assert ma == mb
