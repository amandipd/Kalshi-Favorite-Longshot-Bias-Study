"""How much the backtest result depends on choices that could have gone
differently.

A single ROI number invites an unfair question: was 1.23% a property of the
bias, or a property of the three thresholds picked to measure it? This module
answers by resweeping the backtest across each threshold and reporting the
whole curve rather than the one point that was reported as the headline.

Three axes, chosen because each is a real fork the design took:

    min_net_edge     How much margin above the fee a bucket must clear before
                      it is traded. The headline run uses 0.0 -- any positive
                      net edge. A results table that only holds up at exactly
                      0.0 would be a coincidence dressed as a finding.

    train_fraction    Where the estimation/trading boundary falls (design decision doc 006).
                      Moving it trades estimation precision for tradeable
                      sample, and moves the training and trading periods
                      through different parts of the 3.5x volume ramp.

    kelly_fraction    The risk dial. This one should NOT change the sign of
                      the result -- ROI is per-dollar-deployed and Kelly
                      sizing only rescales stakes, so sweeping it is a
                      sanity check on that invariance, not a search for a
                      better number.

Every run reuses `backtest_bias_strategy` and `backtest_anti_bias_control`
unmodified, so a sensitivity table can never disagree with the headline run
about what the strategy or the control *do* -- only about which config they
were run under.
"""

from __future__ import annotations

import pandas as pd

from src.config import Config, get_config
from src.strategy.backtest import backtest_anti_bias_control, backtest_bias_strategy

__all__ = ["sweep_min_net_edge", "sweep_train_fraction", "sweep_kelly_fraction"]


def _run_at(df: pd.DataFrame, config: Config, **overrides) -> dict:
    """One backtest and its control at a modified strategy config."""
    varied = config.model_copy(
        update={"strategy": config.strategy.model_copy(update=overrides)}
    )
    _, main, split, rules = backtest_bias_strategy(df, config=varied)
    _, control, _, _ = backtest_anti_bias_control(df, config=varied)
    return {
        **overrides,
        "buckets_traded": int(rules["trade"].sum()),
        "trades": main["trades"],
        "roi": main["roi"],
        "control_roi": control["roi"],
        "breakeven_slippage": main["breakeven_slippage"],
        "max_drawdown": main["max_drawdown"],
        "falsifies": control["roi"] < 0 < main["roi"],
    }


def sweep_min_net_edge(
    df: pd.DataFrame,
    thresholds: tuple[float, ...] = (0.0, 0.002, 0.005, 0.01, 0.015, 0.02),
    config: Config | None = None,
) -> pd.DataFrame:
    """Raise the bar for how much net edge a bucket must show to be traded.

    At 0.0 (the headline setting) every bucket with any positive fee-adjusted
    edge trades. Raising the bar drops the thinner-margin buckets first, so
    this is a direct test of whether the result depends on trading the buckets
    closest to breakeven -- if ROI holds or improves as the threshold rises
    while `buckets_traded` falls, the edge is concentrated in a few buckets
    with real margin, not manufactured by including every marginal one.
    """
    config = config or get_config()
    rows = [_run_at(df, config, min_net_edge=t) for t in thresholds]
    return pd.DataFrame(rows).rename(columns={"min_net_edge": "threshold"})


def sweep_train_fraction(
    df: pd.DataFrame,
    fractions: tuple[float, ...] = (0.4, 0.5, 0.6, 0.7, 0.8),
    config: Config | None = None,
) -> pd.DataFrame:
    """Move the estimation/trading boundary (design decision doc 006) and re-run both sides.

    A smaller fraction trades more contracts on a noisier in-sample estimate;
    a larger one trades fewer contracts on a tighter one. Volume ramps 3.5x
    across the window, so this also changes which regime each side sees --
    a low fraction trains mostly on the quiet months and trades the busy ones.
    If the sign of the result depends on exactly where 0.6 falls, the headline
    number is a property of the split, not of the market.
    """
    config = config or get_config()
    rows = [_run_at(df, config, train_fraction=f) for f in fractions]
    return pd.DataFrame(rows).rename(columns={"train_fraction": "threshold"})


def sweep_kelly_fraction(
    df: pd.DataFrame,
    fractions: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 1.0),
    config: Config | None = None,
) -> pd.DataFrame:
    """Vary the Kelly multiplier. Included as an invariance check, not a search.

    ROI is profit per dollar DEPLOYED, and Kelly sizing rescales every stake by
    the same multiplier before the daily budget clips it -- so unless the
    budget binds differently at different scales, ROI should be nearly flat
    across this sweep while `max_drawdown` widens with it. A result that only
    looks good at one specific fraction would mean the sizing rule, not the
    edge, was doing the work.
    """
    config = config or get_config()
    rows = [_run_at(df, config, kelly_fraction=f) for f in fractions]
    return pd.DataFrame(rows).rename(columns={"kelly_fraction": "threshold"})
