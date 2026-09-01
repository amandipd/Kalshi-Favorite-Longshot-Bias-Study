"""Publication figures for the calibration study.

Figures are generated from the same functions `make analyze` prints, never from
a notebook's own arithmetic, so a chart and the table beneath it cannot drift
apart. `make figures` regenerates every PNG in reports/figures/ deterministically.

Design rules followed here, and why each one is a rule rather than a taste:

    one encoding per job     Magnitude gets position, polarity gets a diverging
                             pair (blue <-> red across a neutral gray zero), and
                             identity gets nothing, because every figure below
                             is a single series. No hue is spent on information
                             the axis already shows.

    never colour alone       Significance is carried by fill *and* by an
                             annotation, so a reader who cannot separate the
                             two poles still gets the finding. Underpowered
                             heatmap cells are hatched and labelled, not just
                             greyed.

    no dual axes             Anywhere two quantities would compete, the figure
                             becomes small multiples instead.

The diverging pair (#2a78d6 / #e34948 over a #f0efec midpoint) was checked with
the palette validator: worst-pair CVD delta-E 21.6 (protan), normal-vision 32.3,
both clear of the 8 / 15 floors, and both poles clear 3:1 against the surface.
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")  # no display on a headless run
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from src.analysis.calibration import calibration_table
from src.analysis.segmentation import bias_by_category, bias_by_lifetime
from src.config import Config, get_config

logger = logging.getLogger(__name__)

FIGURES_DIR = Path("reports/figures")

# Chart chrome. Grid and axis are solid hairlines one shade off the surface --
# dashed gridlines read as "threshold" when they are only a grid.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

SERIES = "#2a78d6"  # single-series blue
OVERPRICED = "#e34948"  # negative bias: the market charged more than it was worth
UNDERPRICED = "#2a78d6"  # positive bias
NEUTRAL = "#f0efec"  # the diverging midpoint reads as "nothing"
NOT_SIGNIFICANT = "#c3c2b7"

DIVERGING = LinearSegmentedColormap.from_list(
    "overpriced_underpriced", [OVERPRICED, NEUTRAL, UNDERPRICED]
)


def _style() -> None:
    """Thin marks, recessive chrome, one sans everywhere."""
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "font.family": "sans-serif",
            "font.sans-serif": ["Segoe UI", "DejaVu Sans", "sans-serif"],
            "text.color": INK,
            "axes.labelcolor": INK_SECONDARY,
            "axes.edgecolor": AXIS,
            "axes.linewidth": 0.8,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "grid.linestyle": "-",
            "xtick.color": INK_MUTED,
            "ytick.color": INK_MUTED,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "figure.dpi": 150,
        }
    )


def _despine(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    logger.info("event=figure_written path=%s", path)
    return path


def reliability_diagram(table: pd.DataFrame, path: Path) -> Path:
    """Predicted vs realized, with the clustered interval and the 45-degree ideal.

    The identity line is the whole reference frame, so it is direct-labelled
    rather than pushed into a legend box: with one data series there is nothing
    for a legend to disambiguate.

    Error bars are the block-bootstrap interval clustered on `event_ticker`.
    They are drawn asymmetric because the bootstrap percentiles are, and
    forcing them symmetric would quietly misreport the tail buckets, which are
    the ones the hypothesis is about.
    """
    fig, ax = plt.subplots(figsize=(6.2, 6.0))

    ax.plot([0, 1], [0, 1], color=AXIS, linewidth=1.2, zorder=1)
    ax.annotate(
        "perfect calibration",
        xy=(0.62, 0.62),
        xytext=(0.66, 0.55),
        color=INK_MUTED,
        fontsize=9,
        rotation=0,
        ha="left",
    )

    x = table["mean_price"].to_numpy()
    y = table["realized_freq"].to_numpy()
    lower = np.clip(y - table["ci_low"].to_numpy(), 0, None)
    upper = np.clip(table["ci_high"].to_numpy() - y, 0, None)

    ax.errorbar(
        x, y, yerr=[lower, upper], fmt="none", ecolor=SERIES, elinewidth=1.6,
        capsize=3, capthick=1.6, alpha=0.85, zorder=2,
    )
    ax.plot(x, y, color=SERIES, linewidth=1.8, zorder=3)
    ax.plot(
        x, y, "o", markersize=8, color=SERIES,
        markeredgecolor=SURFACE, markeredgewidth=2, zorder=4,
    )

    # Selective direct labels: the two extremes carry the story, and a number
    # on every point would be noise.
    for index, (dx, dy), align in (
        # Both labels sit inside the axes: the top one hangs below-left of its
        # marker, since above-right runs off the corner.
        (0, (14, -16), "left"),
        (len(table) - 1, (-10, -30), "right"),
    ):
        row = table.iloc[index]
        ax.annotate(
            f"{row['bucket_low']:.1f}–{row['bucket_high']:.1f}\n"
            f"{row['bias']:+.3f}",
            xy=(row["mean_price"], row["realized_freq"]),
            xytext=(dx, dy),
            textcoords="offset points",
            fontsize=9,
            color=INK_SECONDARY,
            ha=align,
        )

    # The clustered intervals ARE drawn -- they are simply narrower than the
    # markers at this scale, which is itself the finding and the reason
    # figure 02 rescales to the deviation.
    ax.annotate(
        "95% clustered intervals are drawn, and are narrower\n"
        "than the markers at this scale. Figure 02 rescales.",
        xy=(0.03, 0.90),
        xycoords="axes fraction",
        fontsize=8.5,
        color=INK_MUTED,
        va="top",
    )

    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.set_xlabel("Market's implied probability at T-1h")
    ax.set_ylabel("Fraction that actually resolved yes")
    ax.set_title(
        "Kalshi prices are close to calibrated, and bend below the line "
        "among longshots",
        loc="left", color=INK, pad=14,
    )
    _despine(ax)
    return _save(fig, path)


def bias_by_bucket(table: pd.DataFrame, path: Path) -> Path:
    """The same result as a deviation chart, where the effect is legible.

    The reliability diagram above is honest and nearly flat -- a 3-cent gap is
    invisible against a full 0-to-1 axis. Rescaling to the deviation from zero
    is the point of this figure, so the y-axis is the bias itself.

    Colour is diverging because the quantity is: red where the market charged
    more than the event turned out to be worth, blue where it charged less,
    grey where the corrected test cannot tell. Significance is *also* carried
    by the hatch and the legend, never by hue alone.
    """
    fig, ax = plt.subplots(figsize=(8.4, 4.8))

    centres = (table["bucket_low"] + table["bucket_high"]) / 2
    bias = table["bias"].to_numpy()
    significant = table["significant"].to_numpy()
    colors = [
        NOT_SIGNIFICANT if not sig else (OVERPRICED if b < 0 else UNDERPRICED)
        for b, sig in zip(bias, significant)
    ]

    width = float(table["bucket_high"].iloc[0] - table["bucket_low"].iloc[0]) * 0.72
    bars = ax.bar(centres, bias, width=width, color=colors, zorder=2)
    for bar, sig in zip(bars, significant):
        if not sig:
            bar.set_hatch("///")
            bar.set_edgecolor(SURFACE)
            bar.set_linewidth(0)

    # The interval on the bias, from the same clustered bootstrap.
    lower = bias - (table["ci_low"].to_numpy() - table["mean_price"].to_numpy())
    upper = (table["ci_high"].to_numpy() - table["mean_price"].to_numpy()) - bias
    ax.errorbar(
        centres, bias, yerr=[np.clip(lower, 0, None), np.clip(upper, 0, None)],
        fmt="none", ecolor=INK_SECONDARY, elinewidth=1.2, capsize=3, alpha=0.7,
        zorder=3,
    )

    ax.axhline(0, color=AXIS, linewidth=1.0, zorder=1)
    ax.set_xlim(0, 1)
    ax.set_xticks(np.arange(0, 1.01, 0.1))
    ax.set_xlabel("Implied probability bucket")
    ax.set_ylabel("Realized frequency − mean price")
    ax.set_title(
        "Longshots overpriced, favorites underpriced – by two to three cents",
        loc="left", color=INK, pad=14,
    )
    ax.legend(
        handles=[
            Patch(facecolor=OVERPRICED, label="overpriced (market charged too much)"),
            Patch(facecolor=UNDERPRICED, label="underpriced"),
            Patch(facecolor=NOT_SIGNIFICANT, hatch="///", edgecolor=SURFACE,
                  label="not significant after correction"),
        ],
        loc="upper left", ncol=1,
    )
    _despine(ax)
    return _save(fig, path)


def category_heatmap(table: pd.DataFrame, path: Path) -> Path:
    """Bias by category and price band, with untestable cells struck out.

    A heatmap earns its cell labels -- the numbers are the table view, not the
    "value on every point" anti-pattern, which is about scatters and lines.

    Cells with too few events are hatched and labelled `thin` rather than
    coloured, because a colour would invite the eye to compare them with cells
    that were actually tested. Politics is almost entirely thin, and showing
    that is the finding: 16 events cannot answer this question.
    """
    pivot_bias = table.pivot(index="segment", columns="bucket", values="bias")
    pivot_thin = table.pivot(index="segment", columns="bucket", values="underpowered")
    pivot_sig = table.pivot(index="segment", columns="bucket", values="significant")
    bands = (
        table.drop_duplicates("bucket")
        .sort_values("bucket")
        .apply(lambda r: f"{r['bucket_low']:.1f}–{r['bucket_high']:.1f}", axis=1)
        .tolist()
    )

    values = pivot_bias.to_numpy(dtype=float)
    masked = np.ma.masked_where(pivot_thin.to_numpy(), values)
    limit = float(np.nanmax(np.abs(masked))) if masked.count() else 0.1
    norm = TwoSlopeNorm(vmin=-limit, vcenter=0.0, vmax=limit)

    fig, ax = plt.subplots(figsize=(8.0, 3.4))
    ax.set_facecolor(SURFACE)
    image = ax.imshow(masked, cmap=DIVERGING, norm=norm, aspect="auto")
    ax.grid(False)

    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            thin = bool(pivot_thin.to_numpy()[row, column])
            if thin:
                ax.add_patch(
                    plt.Rectangle(
                        (column - 0.5, row - 0.5), 1, 1,
                        facecolor=SURFACE, edgecolor=AXIS, hatch="///",
                        linewidth=0.6,
                    )
                )
                ax.text(column, row, "thin", ha="center", va="center",
                        fontsize=8, color=INK_MUTED)
                continue
            value = values[row, column]
            # Ink stays readable on both poles; the mark carries identity.
            shade = INK if abs(value) < limit * 0.55 else "#ffffff"
            star = "*" if pivot_sig.to_numpy()[row, column] else ""
            ax.text(column, row, f"{value:+.3f}{star}", ha="center", va="center",
                    fontsize=9, color=shade)

    # A surface gap between fills, never a border drawn around each mark.
    ax.set_xticks(np.arange(-0.5, values.shape[1], 1), minor=True)
    ax.set_yticks(np.arange(-0.5, values.shape[0], 1), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2.0)
    ax.tick_params(which="minor", length=0)

    ax.set_xticks(range(len(bands)), bands)
    ax.set_yticks(range(len(pivot_bias.index)), list(pivot_bias.index))
    ax.set_xlabel("Implied probability band")
    ax.set_title(
        "The bias is far larger away from Sports – where there is least data",
        loc="left", color=INK, pad=14,
    )
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)

    bar = fig.colorbar(image, ax=ax, pad=0.02, fraction=0.03)
    bar.set_label("realized − priced", fontsize=9, color=INK_SECONDARY)
    bar.ax.tick_params(labelsize=8, color=AXIS)
    bar.outline.set_visible(False)
    fig.text(0.01, -0.06, "* significant after family-wide correction; hatched "
             "cells have too few events to test", fontsize=8, color=INK_MUTED)
    return _save(fig, path)


def bias_by_lifetime_figure(table: pd.DataFrame, path: Path) -> Path:
    """Small multiples: bias against market lifetime, one panel per price band.

    Five price bands on one axis would need five categorical hues, which cannot
    clear the all-pairs colour-separation floor. Faceting removes the need for
    hue entirely -- every panel is a single series, and the panel title carries
    the identity that colour would otherwise have to.

    All panels share one y-axis so the panels are comparable; that is the whole
    reason to facet rather than draw five separate charts.
    """
    bands = (
        table.drop_duplicates("bucket")
        .sort_values("bucket")
        .apply(lambda r: (r["bucket"], f"{r['bucket_low']:.1f}–{r['bucket_high']:.1f}"), axis=1)
        .tolist()
    )
    segments = list(dict.fromkeys(table["segment"]))
    limit = float(np.nanmax(np.abs(table["bias"]))) * 1.35

    fig, axes = plt.subplots(
        1, len(bands), figsize=(2.5 * len(bands), 3.6), sharey=True
    )
    for ax, (bucket, label) in zip(np.atleast_1d(axes), bands):
        rows = table[table["bucket"] == bucket].set_index("segment").reindex(segments)
        x = np.arange(len(segments))
        bias = rows["bias"].to_numpy(dtype=float)
        significant = rows["significant"].fillna(False).to_numpy()

        ax.axhline(0, color=AXIS, linewidth=1.0, zorder=1)
        ax.plot(x, bias, color=SERIES, linewidth=1.8, zorder=2)
        # Filled = significant, hollow = not. Shape, not colour, carries it.
        for xi, value, sig in zip(x, bias, significant):
            ax.plot(
                xi, value, "o", markersize=8,
                color=SERIES if sig else SURFACE,
                markeredgecolor=SERIES, markeredgewidth=1.8, zorder=3,
            )
        ax.set_ylim(-limit, limit)
        ax.set_xticks(x, [s.split(". ")[0] for s in segments])
        ax.set_title(label, fontsize=10, color=INK_SECONDARY)
        _despine(ax)

    axes[0].set_ylabel("Realized frequency − mean price")
    fig.supxlabel(
        "Market lifetime quartile (1 = shortest-lived, "
        f"{segments[0].split('. ')[1]}; 4 = longest)",
        fontsize=9, color=INK_MUTED, y=-0.04,
    )
    fig.suptitle(
        "Miscalibration concentrates in the shortest-lived markets",
        x=0.02, ha="left", fontsize=12, color=INK, y=1.02,
    )
    fig.legend(
        handles=[
            Line2D([], [], marker="o", linestyle="", markersize=8, color=SERIES,
                   label="significant"),
            Line2D([], [], marker="o", linestyle="", markersize=8, color=SURFACE,
                   markeredgecolor=SERIES, markeredgewidth=1.8,
                   label="not significant"),
        ],
        loc="lower right", bbox_to_anchor=(1.0, -0.12), ncol=2,
    )
    return _save(fig, path)


def build_all(config: Config | None = None) -> list[Path]:
    """Regenerate every figure from the processed table."""
    config = config or get_config()
    _style()
    df = pd.read_parquet(config.clean.processed_path)

    written = [
        reliability_diagram(
            calibration_table(df, config=config),
            FIGURES_DIR / "01_reliability_diagram.png",
        ),
        bias_by_bucket(
            calibration_table(df, config=config),
            FIGURES_DIR / "02_bias_by_bucket.png",
        ),
        category_heatmap(
            bias_by_category(df, config=config),
            FIGURES_DIR / "03_bias_by_category.png",
        ),
        bias_by_lifetime_figure(
            bias_by_lifetime(df, config=config),
            FIGURES_DIR / "04_bias_by_lifetime.png",
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
