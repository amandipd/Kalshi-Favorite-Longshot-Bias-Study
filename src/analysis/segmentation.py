"""Where the bias lives: calibration broken out by category and by lifetime.

The pooled table in `calibration.py` answers "is the market biased?". This
module answers "biased *where*?", which is the more useful question and the
more dangerous one, because slicing a dataset ten ways and reporting the
slice that looks best is how noise gets published.

Three guards, all applied before any segment is looked at:

    one correction family     Every segment x bucket test goes into a single
                              Benjamini-Hochberg family. Correcting inside
                              each segment separately would let the number of
                              segments grow for free, which is the whole
                              problem the correction exists to stop.

                              Worth knowing and easy to state wrongly: pooling
                              is not uniformly *stricter*. BH is a step-up
                              procedure, so a segment carrying strong signal
                              lifts the others' ranks faster than it lifts m,
                              and a marginal test can end up with a smaller q
                              pooled than alone. The guarantee is about the
                              share of false discoveries across the family,
                              not about any single q moving one way.

    a power floor             A bucket with fewer than `min_events_per_bucket`
                              events keeps its estimate and interval but is
                              never tested and never enters the family. Sports
                              has 29,471 events and Politics has 16; without a
                              floor, Politics contributes a dozen meaningless
                              tests that make every real test pay for them.

    coarser buckets           Quintiles rather than deciles, fixed from the
                              segment SIZES before any segment result was
                              computed.

Clustering on `event_ticker` continues to apply inside every segment, and
matters more here than in the pooled table: Economics has 2,743 contracts but
186 events, so the naive sample size overstates the real one fifteenfold.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.analysis.calibration import calibration_table
from src.analysis.statistics import benjamini_hochberg
from src.config import Config, get_config

__all__ = [
    "segment_calibration",
    "bias_by_category",
    "lifetime_hours",
    "bias_by_lifetime",
]

LIFETIME_COL = "lifetime_hours"


def segment_calibration(
    df: pd.DataFrame,
    segment_col: str,
    config: Config | None = None,
    n_buckets: int | None = None,
) -> pd.DataFrame:
    """Calibration table per level of `segment_col`, corrected as one family.

    Each segment's table is computed independently -- its own buckets, its own
    clustered intervals, its own bootstrap -- and then the p-values from every
    segment are pooled and corrected together, so `q_value` and `significant`
    mean the same thing across the whole result.

    Segments are processed in sorted order and each gets the configured
    bootstrap seed, so the output is reproducible and does not depend on how
    many segments happen to precede it.

    Args:
        df: Processed contracts.
        segment_col: Column to split on.
        config: Overrides the loaded config.
        n_buckets: Overrides config.analysis.segment_n_buckets.

    Returns:
        The stacked per-segment tables with three columns added or replaced:
        `segment`, `underpowered` (fewer than min_events_per_bucket events,
        so not tested), and family-wide `q_value` / `significant`. Rows that
        are underpowered carry NaN q and are never significant.
    """
    config = config or get_config()
    settings = config.analysis
    n_buckets = n_buckets or settings.segment_n_buckets
    if segment_col not in df.columns:
        raise KeyError(f"missing segment column {segment_col!r}")

    tables = []
    for segment in sorted(df[segment_col].dropna().unique()):
        subset = df[df[segment_col] == segment]
        if subset.empty:
            continue
        table = calibration_table(subset, n_buckets=n_buckets, config=config)
        table.insert(0, "segment", segment)
        tables.append(table)

    if not tables:
        raise ValueError(f"no non-empty segments in {segment_col!r}")

    stacked = pd.concat(tables, ignore_index=True)
    stacked["underpowered"] = stacked["n_events"] < settings.min_events_per_bucket

    # calibration_table corrected within each segment; that is the wrong family
    # here, so both columns are recomputed over the pooled tests.
    p_values = stacked["p_value"].to_numpy(dtype=float).copy()
    p_values[stacked["underpowered"].to_numpy()] = np.nan
    rejected, q_values = benjamini_hochberg(p_values, alpha=settings.fdr_alpha)
    stacked["q_value"] = q_values
    stacked["significant"] = rejected
    return stacked


def bias_by_category(
    df: pd.DataFrame, config: Config | None = None, n_buckets: int | None = None
) -> pd.DataFrame:
    """Calibration by market category.

    The segmentation that matters most, because the pooled corpus is 95.9%
    Sports -- the headline table is a chart about sports betting with a
    rounding error of other markets attached, and this is the only way to see
    whether the effect is a property of prediction markets or of one category.

    Expect most non-Sports cells to come back underpowered. That is the
    finding, not a failure: category coverage is set by what Kalshi actually
    settles, and the ingestion window was chosen (journal, 2026-08-05) knowing
    Politics was thin in every window tried.
    """
    return segment_calibration(df, "category", config=config, n_buckets=n_buckets)


def lifetime_hours(df: pd.DataFrame) -> pd.Series:
    """Hours a market was open for trading, `open_ts` to `close_ts`.

    **Not** time-to-resolution, and the difference is the point. The proposal
    asked for bias by time-to-resolution, which does not vary in this dataset:
    design decision doc 003 prices every market at exactly one hour before its close, so the
    gap between the forecast and the outcome is ~1 hour for all 100,210 rows
    by construction. There is nothing to segment.

    What does vary is how long the market had existed *before* that moment --
    median 25 hours, quartiles at 15.6 / 25.0 / 45.1, tail out past two years.
    That supports a real question with the same spirit: does a contract that
    has been trading for a month price better than one that opened yesterday?
    More time is more opportunity to aggregate information, so if the bias is
    an information-aggregation failure it should shrink with lifetime.
    """
    for column in ("open_ts", "close_ts"):
        if column not in df.columns:
            raise KeyError(f"missing required column: {column}")
    delta = pd.to_datetime(df["close_ts"], utc=True) - pd.to_datetime(
        df["open_ts"], utc=True
    )
    return delta.dt.total_seconds() / 3600.0


def bias_by_lifetime(
    df: pd.DataFrame,
    n_time_buckets: int = 4,
    config: Config | None = None,
    n_buckets: int | None = None,
) -> pd.DataFrame:
    """Calibration by how long the market traded before it was priced.

    Lifetime buckets are **quantiles**, not equal width, which is the opposite
    of the choice made for price buckets -- and for the opposite reason. Price
    bands are the hypothesis itself, so their edges must be fixed in advance;
    lifetime is a nuisance dimension with a two-year tail and no theory about
    where its edges belong, so equal-width bins would put 99% of the corpus in
    the first one and measure nothing. Quantiles keep every lifetime bucket
    populated enough to carry a clustered interval.

    Labels carry the actual hour ranges so a reader never has to guess what
    "Q2" covers.
    """
    df = df.copy()
    df[LIFETIME_COL] = lifetime_hours(df)

    edges = np.unique(
        np.quantile(df[LIFETIME_COL], np.linspace(0.0, 1.0, n_time_buckets + 1))
    )
    if edges.size < 2:
        raise ValueError("every market has the same lifetime; nothing to segment")

    codes = np.clip(np.searchsorted(edges, df[LIFETIME_COL], side="left") - 1, 0, edges.size - 2)
    labels = [
        f"{i + 1}. {edges[i]:.1f}-{edges[i + 1]:.1f}h" for i in range(edges.size - 1)
    ]
    df["lifetime_bucket"] = [labels[c] for c in codes]

    return segment_calibration(
        df, "lifetime_bucket", config=config, n_buckets=n_buckets
    )
