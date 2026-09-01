"""`make analyze`: print the calibration result and write it to reports/.

The printed table IS the finding, so this module exists to make sure there is
exactly one way to produce it. A notebook that recomputes its own version of
the headline number is a notebook that will eventually disagree with the
report, and nobody will know which one is right.

Everything here is deterministic given `data/processed/contracts.parquet` and
config.yaml, bootstrap seed included, so two runs a month apart print the same
digits.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from src.analysis.calibration import (
    brier_decomposition,
    calibration_table,
    logistic_calibration,
)
from src.analysis.segmentation import bias_by_category, bias_by_lifetime
from src.strategy.backtest import (
    backtest_anti_bias_control,
    backtest_bias_strategy,
)
from src.config import Config, get_config

logger = logging.getLogger(__name__)

REPORTS_DIR = Path("reports")
TABLE_PATH = REPORTS_DIR / "calibration_table.csv"
CATEGORY_PATH = REPORTS_DIR / "bias_by_category.csv"
LIFETIME_PATH = REPORTS_DIR / "bias_by_lifetime.csv"
LEDGER_PATH = REPORTS_DIR / "backtest_ledger.csv"
RULES_PATH = REPORTS_DIR / "backtest_rules.csv"


def _stars(q_value: float, significant: bool) -> str:
    """Significance marks read off the *corrected* q-value, never the raw p."""
    if not significant:
        return ""
    if q_value < 0.001:
        return "***"
    if q_value < 0.01:
        return "**"
    return "*"


def format_table(table: pd.DataFrame) -> str:
    """The reliability diagram as text, with the reportable interval only.

    `wilson_low/high` are deliberately not shown. They exist to compute the
    design effect and would be a mistake to read as an interval, so the one
    place a reader looks at results is not the place to put them.
    """
    lines = [
        f"{'bucket':>12} {'n':>7} {'events':>7} {'price':>7} {'actual':>7} "
        f"{'bias':>8} {'95% CI (clustered)':>22} {'deff':>6} {'q':>9}",
        "-" * 96,
    ]
    for row in table.itertuples():
        interval = f"[{row.ci_low:.4f}, {row.ci_high:.4f}]"
        lines.append(
            f"{row.bucket_low:.2f}-{row.bucket_high:.2f}".rjust(12)
            + f" {row.n:>7,} {row.n_events:>7,} {row.mean_price:>7.4f} "
            f"{row.realized_freq:>7.4f} {row.bias:>+8.4f} {interval:>22} "
            f"{row.design_effect:>6.2f} {row.q_value:>9.2e}"
            + _stars(row.q_value, row.significant)
        )
    return "\n".join(lines)


def format_decomposition(parts: dict[str, float]) -> str:
    return "\n".join(
        [
            f"  Brier score       {parts['brier']:.6f}",
            f"    reliability     {parts['reliability']:.6f}   calibration error, lower is better",
            f"  - resolution      {parts['resolution']:.6f}   discrimination, higher is better",
            f"  + uncertainty     {parts['uncertainty']:.6f}   base rate {parts['base_rate']:.4f}, "
            "irreducible",
            f"  = binned Brier    {parts['binned_brier']:.6f}   the identity closes exactly on this",
            f"    binning residual {parts['binning_residual']:+.6f}  within-bucket price variation "
            f"the {parts['n_buckets']} buckets cannot see",
        ]
    )


def format_logistic(fit) -> str:
    """The bucket-free read. Perfect calibration is slope 1, intercept 0."""
    direction = (
        "longshots overpriced, favorites underpriced -- the favorite-longshot "
        "direction"
        if fit.slope > 1
        else "longshots underpriced -- the reverse of the hypothesis"
    )
    return "\n".join(
        [
            f"  slope      {fit.slope:.4f}  95% CI [{fit.slope_ci[0]:.4f}, "
            f"{fit.slope_ci[1]:.4f}]   vs 1: z={fit.slope_z:+.2f} p={fit.slope_p:.2e}",
            f"  intercept  {fit.intercept:+.4f}  95% CI [{fit.intercept_ci[0]:+.4f}, "
            f"{fit.intercept_ci[1]:+.4f}]   vs 0: z={fit.intercept_z:+.2f} "
            f"p={fit.intercept_p:.2e}",
            f"  joint test of (0, 1): chi2={fit.joint_chi2:.1f} on {fit.joint_df} df, "
            f"p={fit.joint_p:.2e}",
            f"  {fit.n:,} contracts, {fit.n_clusters:,} clusters, "
            f"pseudo-R2 {fit.pseudo_r2:.3f}",
            f"  slope {'>' if fit.slope > 1 else '<'} 1: {direction}.",
        ]
    )


def format_segments(table: pd.DataFrame, label: str) -> str:
    """Segment tables, with untestable cells marked rather than deleted."""
    lines = [
        f"{label:>18} {'band':>11} {'n':>7} {'events':>7} {'price':>7} "
        f"{'actual':>7} {'bias':>8} {'95% CI (clustered)':>22} {'q':>10}",
        "-" * 105,
    ]
    for segment, rows in table.groupby("segment", sort=False):
        for position, row in enumerate(rows.itertuples()):
            interval = f"[{row.ci_low:.4f}, {row.ci_high:.4f}]"
            if row.underpowered:
                verdict = "  (thin)"
            else:
                verdict = f"{row.q_value:>10.2e}" + _stars(row.q_value, row.significant)
            lines.append(
                f"{str(segment) if position == 0 else '':>18} "
                f"{row.bucket_low:.1f}-{row.bucket_high:.1f}".rjust(30)
                + f" {row.n:>7,} {row.n_events:>7,} {row.mean_price:>7.4f} "
                f"{row.realized_freq:>7.4f} {row.bias:>+8.4f} {interval:>22} "
                + verdict
            )
    return "\n".join(lines)


def format_backtest(
    main: dict, control: dict, split, rules: pd.DataFrame
) -> str:
    """Strategy against its own falsification control, side by side.

    The control is shown next to the strategy rather than in a footnote,
    because a strategy result without its control is an assertion.
    """
    lines = [
        f"Split {split.split_ts:%Y-%m-%d}: {len(split.train):,} contracts to "
        f"estimate on, {len(split.test):,} to trade, {split.excluded} dropped "
        "in the settle/price gap.",
        "",
        f"{'':>28} {'side':>5} {'gross':>8} {'fee':>8} {'net':>8}  trade",
        "-" * 70,
    ]
    for row in rules.itertuples():
        verdict = "YES" if row.trade else "no"
        lines.append(
            f"{row.mean_price:>28.4f} {row.side:>5} {row.gross_edge:>8.4f} "
            f"{row.fee:>8.4f} {row.net_edge:>+8.4f}  {verdict}"
        )

    lines += ["", f"{'metric':>22} {'STRATEGY':>14} {'CONTROL':>14}", "-" * 54]
    for key in ("trades", "roi", "hit_rate", "total_pnl", "gross_pnl",
                "fees_paid", "final_equity", "max_drawdown", "sharpe_like",
                "trading_days"):
        lines.append(f"{key:>22} {main[key]:>14,.4f} {control[key]:>14,.4f}")

    lines += [
        "",
        f"Breakeven slippage: {main['breakeven_slippage'] * 100:.2f}c per contract.",
        "That is the adverse fill that erases the entire edge. The backtest",
        "fills at the last traded price and no spread is modelled (settled",
        "snapshots carry no usable book), so it is a no-spread upper bound.",
        "Read this number before the ROI.",
    ]
    if control["roi"] < 0 < main["roi"]:
        lines.append("")
        lines.append(
            "Falsification: the inverted strategy loses on the same contracts "
            "and fees, as it must if the effect is real."
        )
    return "\n".join(lines)


def run(config: Config | None = None) -> pd.DataFrame:
    """Compute, print, and persist the headline calibration result."""
    config = config or get_config()
    df = pd.read_parquet(config.clean.processed_path)

    events = df["event_ticker"].nunique()
    print(
        f"{len(df):,} contracts across {events:,} events "
        f"({len(df) / events:.2f} per event), priced at "
        f"T-{config.clean.price_horizon_hours:g}h via {config.clean.price_method}"
    )
    print(f"base rate: {df['outcome'].mean():.4f}\n")

    print(format_decomposition(brier_decomposition(df, config=config)))

    table = calibration_table(df, config=config)
    print(f"\nCalibration, {config.analysis.n_buckets} equal-width price buckets.")
    print(
        f"Intervals and tests cluster on {config.analysis.cluster_on} "
        f"({config.analysis.bootstrap_reps:,} block-bootstrap reps, seed "
        f"{config.analysis.bootstrap_seed}); q is Benjamini-Hochberg-corrected "
        f"across buckets at alpha={config.analysis.fdr_alpha}."
    )
    print("deff > 1 means treating contracts as independent would have been too confident.\n")
    print(format_table(table))
    print("\n*** q<0.001  ** q<0.01  * q<0.05")

    # bias < 0 means the market priced it above what happened -- overpriced.
    below = table[table["bucket_high"] <= 0.5]
    above = table[table["bucket_low"] >= 0.5]
    print(
        f"\nBelow 50c: {int((below['bias'] < 0).sum())}/{len(below)} buckets "
        f"overpriced. Above 50c: {int((above['bias'] > 0).sum())}/{len(above)} "
        "underpriced."
    )

    print("\n\nLogistic calibration -- outcome ~ logit(price), no buckets.")
    print(format_logistic(logistic_calibration(df, config=config)))

    print(
        f"\n\nSegments. {config.analysis.segment_n_buckets} price bands each "
        f"(coarser than the headline {config.analysis.n_buckets}, because the "
        "slices are thinner).\nOne correction family spans every cell below. A "
        f"cell with fewer than {config.analysis.min_events_per_bucket} events "
        "is shown but not tested -- marked (thin)."
    )
    category = bias_by_category(df, config=config)
    print("\n" + format_segments(category, "category"))

    lifetime = bias_by_lifetime(df, config=config)
    print(
        "\n\nBy market lifetime (open to close). NOT time-to-resolution, which "
        "is ~1h\nfor every row by construction -- see segmentation.lifetime_hours."
    )
    print("\n" + format_segments(lifetime, "lifetime"))

    ledger, main, split, rules = backtest_bias_strategy(df, config=config)
    _, control, _, _ = backtest_anti_bias_control(df, config=config)
    print("\n\nOut-of-sample backtest, Kalshi taker fees deducted.")
    print(format_backtest(main, control, split, rules))

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(LEDGER_PATH, index=False)
    rules.to_csv(RULES_PATH, index=False)
    table.to_csv(TABLE_PATH, index=False)
    category.to_csv(CATEGORY_PATH, index=False)
    lifetime.to_csv(LIFETIME_PATH, index=False)
    print(
        f"\nwrote {TABLE_PATH}, {CATEGORY_PATH}, {LIFETIME_PATH}, "
        f"{RULES_PATH}, {LEDGER_PATH}"
    )
    return table


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    run()


if __name__ == "__main__":
    main()
