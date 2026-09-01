"""Backtest figures: the equity curve, its falsification control, and the
sensitivity sweeps.

Same rules as `src/analysis/figures.py`, whose palette and chrome this module
imports rather than restates -- one source of truth for what a chart looks
like in this repo. `make figures` regenerates everything here too.

The equity curve is the one figure in the project where two series belong on
one axes: the strategy and its anti-bias control **must** be compared directly,
because the claim is relative ("the real bias beats its own mirror image"),
not absolute. That is the one case the single-series rule in the sibling
module doesn't apply to, and it still gets a legend and fixed hue assignment
rather than an improvised one.
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.analysis.figures import (
    AXIS,
    GRID,
    INK,
    INK_MUTED,
    INK_SECONDARY,
    NOT_SIGNIFICANT,
    OVERPRICED,
    SERIES,
    SURFACE,
    UNDERPRICED,
    _despine,
    _save,
    _style,
)
from src.config import Config, get_config
from src.strategy.backtest import backtest_anti_bias_control, backtest_bias_strategy
from src.strategy.sensitivity import sweep_min_net_edge, sweep_train_fraction

logger = logging.getLogger(__name__)

FIGURES_DIR = Path("reports/figures")


def _daily_equity(ledger: pd.DataFrame) -> pd.Series:
    """Bankroll-relative equity curve, one point per settlement day.

    Mirrors `summarise`'s own aggregation exactly, so this curve and the
    `final_equity` / `max_drawdown` numbers in the printed report can never
    show a different story than the figure.
    """
    if ledger.empty:
        return pd.Series(dtype=float)
    daily = ledger.assign(day=pd.to_datetime(ledger["settle_ts"], utc=True).dt.date)
    daily_pnl = daily.groupby("day")["pnl"].sum()
    return (1.0 + daily_pnl).cumprod()


def equity_curve(
    ledger: pd.DataFrame, control_ledger: pd.DataFrame, path: Path
) -> Path:
    """Strategy bankroll against its falsification control, same axis.

    The control is not a footnote here -- it is drawn on the same axes at the
    same scale specifically so the asymmetry is visible without reading two
    separate numbers. A strategy that "worked" but looked like the control
    would be a strategy that didn't demonstrate anything.
    """
    equity = _daily_equity(ledger)
    control_equity = _daily_equity(control_ledger)

    fig, ax = plt.subplots(figsize=(8.0, 4.6))
    ax.axhline(1.0, color=AXIS, linewidth=1.0, zorder=1)

    if len(equity):
        days = np.arange(len(equity))
        ax.plot(days, equity.to_numpy(), color=SERIES, linewidth=2.0, zorder=3)
        ax.annotate(
            f"strategy\n{equity.iloc[-1]:.2f}x",
            xy=(days[-1], equity.iloc[-1]),
            xytext=(8, 4),
            textcoords="offset points",
            color=SERIES,
            fontsize=9,
            fontweight="bold",
        )
    if len(control_equity):
        days_c = np.arange(len(control_equity))
        ax.plot(
            days_c, control_equity.to_numpy(), color=OVERPRICED, linewidth=2.0,
            linestyle=(0, (5, 2)), zorder=2,
        )
        ax.annotate(
            f"anti-bias control\n{control_equity.iloc[-1]:.4f}x",
            xy=(days_c[-1], max(control_equity.iloc[-1], 0.02)),
            xytext=(8, -4),
            textcoords="offset points",
            color=OVERPRICED,
            fontsize=9,
            fontweight="bold",
            va="top",
        )

    ax.set_ylim(bottom=-0.05)
    ax.set_xlabel("Settlement day, out-of-sample period")
    ax.set_ylabel("Bankroll (start = 1.0)")
    ax.set_title(
        "The real bias profits; trading its mirror image is ruinous",
        loc="left", color=INK, pad=14,
    )
    _despine(ax)
    return _save(fig, path)


def return_distribution(ledger: pd.DataFrame, path: Path) -> Path:
    """Per-trade return on cost, split by win/loss.

    A histogram rather than a single hit-rate number, because Kelly sizing
    means wins and losses are not symmetric in size -- the shape of the two
    piles matters, not just their counts.
    """
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    returns = ledger["pnl"] / ledger["cost"]
    won = ledger["won"].to_numpy()

    bins = np.linspace(returns.min(), returns.max(), 40)
    ax.hist(
        returns[won], bins=bins, color=UNDERPRICED, alpha=0.85,
        label=f"won ({won.mean():.1%})", zorder=2,
    )
    ax.hist(
        returns[~won], bins=bins, color=OVERPRICED, alpha=0.85,
        label=f"lost ({(~won).mean():.1%})", zorder=2,
    )
    ax.axvline(0, color=AXIS, linewidth=1.0, zorder=1)

    ax.set_xlabel("Return on capital deployed for that trade")
    ax.set_ylabel("Trades")
    ax.set_title(
        "Every loss forfeits the stake; every win pays contracts minus stake",
        loc="left", color=INK, pad=14,
    )
    ax.legend(loc="upper left")
    _despine(ax)
    return _save(fig, path)


def sensitivity_panel(
    edge_sweep: pd.DataFrame, split_sweep: pd.DataFrame, path: Path
) -> Path:
    """ROI against the two thresholds that could have been chosen differently.

    Small multiples, one panel per swept parameter, sharing a y-axis so the
    reader can see both without a dual-axis chart. The vertical marker on each
    panel is the value actually used for the headline result -- so a reader
    can see, at the point that matters, whether the result sits in the middle
    of a stable region or balanced on an edge.
    """
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.0), sharey=True)

    panels = [
        (axes[0], edge_sweep, "min_net_edge", 0.0, "Minimum net edge to trade a bucket"),
        (axes[1], split_sweep, "threshold", 0.6, "Train-fraction split point"),
    ]
    for ax, sweep, xlabel, headline_value, title in panels:
        ax.axhline(0, color=AXIS, linewidth=1.0, zorder=1)
        colors = [
            UNDERPRICED if falsifies else NOT_SIGNIFICANT
            for falsifies in sweep["falsifies"]
        ]
        ax.plot(
            sweep["threshold"], sweep["roi"], color=INK_SECONDARY,
            linewidth=1.4, zorder=2,
        )
        ax.scatter(
            sweep["threshold"], sweep["roi"], c=colors, s=60,
            edgecolors=SURFACE, linewidths=1.5, zorder=3,
        )
        ax.axvline(headline_value, color=AXIS, linewidth=1.0, linestyle=(0, (2, 2)))
        ax.set_xlabel(xlabel, fontsize=9, color=INK_SECONDARY)
        ax.set_title(title, fontsize=10, color=INK_SECONDARY)
        _despine(ax)

    axes[0].set_ylabel("Out-of-sample ROI")
    fig.suptitle(
        "The result holds across a range of thresholds, not just the one reported",
        x=0.02, ha="left", fontsize=12, color=INK, y=1.04,
    )
    fig.text(
        0.02, -0.04,
        "blue = control loses while strategy profits (the falsification holds); "
        "grey = it does not",
        fontsize=8, color=INK_MUTED,
    )
    return _save(fig, path)


def build_all(config: Config | None = None) -> list[Path]:
    """Regenerate every strategy figure from the processed table."""
    config = config or get_config()
    _style()
    df = pd.read_parquet(config.clean.processed_path)

    ledger, _, _, _ = backtest_bias_strategy(df, config=config)
    control_ledger, _, _, _ = backtest_anti_bias_control(df, config=config)

    written = [
        equity_curve(ledger, control_ledger, FIGURES_DIR / "05_equity_curve.png"),
        return_distribution(ledger, FIGURES_DIR / "06_return_distribution.png"),
        sensitivity_panel(
            sweep_min_net_edge(df, config=config),
            sweep_train_fraction(df, config=config),
            FIGURES_DIR / "07_sensitivity.png",
        ),
    ]
    return written


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    for path in build_all():
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
