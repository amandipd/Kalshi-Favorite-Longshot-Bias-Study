"""Trade the measured bias out-of-sample, with fees, and try to falsify it.

The measurement said the bias is real. This asks the only question a reader
actually cares about: does it survive Kalshi's fees when you are not allowed to
know the answer in advance?

**How lookahead is prevented, structurally rather than by care.** The single
failure that destroys a trading project's credibility is using information the
strategy could not have had. Three mechanisms, all of them structural:

1. `time_split` divides on *time*, not at random. Everything the strategy
   learns from comes from markets that had already **settled** before the split
   instant; everything it trades closes after it.

2. The gap between those two conditions is dropped, not straddled. A market
   that was priced before the split but settled after it belongs to neither
   side -- trading it would mean holding a position through the moment the
   training data was assembled. `Split.excluded` counts them.

3. `_bucket_rules` returns a table computed **only** from the training frame,
   and the trading loop reads nothing else. It never receives out-of-sample
   outcomes, so it cannot use them; `test_shuffling_outcomes_does_not_change_sizing`
   proves it by shuffling the answers and asserting every position size is
   byte-identical.

The anti-bias control exists for the same reason. If the finding is real, the
strategy that does the exact opposite must lose money out-of-sample. A result
that cannot fail is not evidence, so the falsification test is built in rather
than left as an exercise.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.analysis.calibration import assign_buckets, calibration_table
from src.config import Config, get_config

logger = logging.getLogger(__name__)

__all__ = [
    "Split",
    "time_split",
    "kalshi_fee",
    "kelly_fraction",
    "bucket_rules",
    "backtest_bias_strategy",
    "backtest_anti_bias_control",
    "summarise",
]

YES, NO = "yes", "no"


@dataclass(frozen=True)
class Split:
    """A leakage-safe division of the corpus into estimation and trading.

    Attributes:
        split_ts: The instant separating them.
        train: Markets that had fully settled before `split_ts`.
        test: Markets priced at or after `split_ts`.
        excluded: Markets in the gap -- priced before the split, settled after
            it. Reported rather than silently absorbed, because the size of
            this set is how a reader checks the split was honest.
    """

    split_ts: pd.Timestamp
    train: pd.DataFrame
    test: pd.DataFrame
    excluded: int


def time_split(df: pd.DataFrame, config: Config | None = None) -> Split:
    """Split into an estimation period and a later trading period.

    **Why by time and not at random.** A random split would put a market's
    siblings on both sides -- the same PGA field, the same CPI ladder -- so the
    "out-of-sample" set would contain contracts whose outcome is logically
    determined by one the model already saw. It would also let the strategy
    learn from June to trade January, which is not a thing anyone can do. A
    random split here would produce a *better-looking and meaningless* result,
    which is the most dangerous kind.

    **Where the boundary goes.** At the close-time quantile holding
    `train_fraction` of contracts, rather than at a fraction of the calendar.
    Volume ramps 3.5x across the window (7,561 contracts closed in December
    against 26,850 in May), so a calendar midpoint would leave the estimation
    period with a quarter of the data. This uses only close dates and counts --
    never a price and never an outcome -- so it cannot leak the label.

    **The gap.** Training requires an outcome, which is known at `settle_ts`.
    Trading happens at `close_ts - horizon`. A market that was priced before
    the split but settles after it satisfies neither condition and is dropped
    from both sides.
    """
    config = config or get_config()
    for column in ("close_ts", "settle_ts", "implied_price", "outcome"):
        if column not in df.columns:
            raise KeyError(f"missing required column: {column}")
    if df.empty:
        raise ValueError("cannot split an empty frame")

    close = pd.to_datetime(df["close_ts"], utc=True)
    settle = pd.to_datetime(df["settle_ts"], utc=True)
    split_ts = close.quantile(config.strategy.train_fraction)

    # The moment the position would be taken, which is what must fall after
    # the split for a market to be tradeable.
    priced_at = close - pd.Timedelta(hours=config.clean.price_horizon_hours)

    is_train = settle < split_ts
    is_test = priced_at >= split_ts

    train = df[is_train].reset_index(drop=True)
    test = df[is_test].reset_index(drop=True)
    excluded = int((~is_train & ~is_test).sum())

    logger.info(
        "event=time_split split=%s train=%d test=%d excluded=%d",
        split_ts, len(train), len(test), excluded,
    )
    return Split(split_ts=split_ts, train=train, test=test, excluded=excluded)


def kalshi_fee(
    price: float | np.ndarray,
    contracts: float | np.ndarray = 1.0,
    config: Config | None = None,
) -> float | np.ndarray:
    """Kalshi's taker fee, `ceil(k * C * P * (1 - P))`.

    Verified against Kalshi's CFTC rule filing and two independent 2026
    sources. There is no settlement or exercise fee, and this strategy holds to
    settlement, so the fee is paid **once** on entry -- there is no exit trade.

    The shape matters more than the level. The fee is a parabola peaking at
    P = 0.50, which is the single worst place to have an edge; it falls toward
    zero at both extremes, which is where longshots and heavy favorites live.
    That is why the 0.50-0.70 buckets are untradeable here even before their
    significance is considered, while the 0.10-0.40 buckets clear comfortably.

    **On the rounding.** The filed formula puts C *inside* the ceiling --
    `ceil(0.07 * C * P * (1-P))` -- so the round-up is per order and, spread
    over any realistic order, is worth less than a cent per contract. The
    default therefore charges the exact rate and lets that sub-cent remainder
    go, which is bounded and stated rather than hidden.

    Charging the ceiling *per contract* instead is a different and much more
    expensive claim: at P=0.03 the true rate is 0.21c and a per-contract
    ceiling charges 1c, a 4.7x overcharge that would wrongly rule the longshot
    bucket untradeable. It is retained behind `fee_ceiling_per_contract` as a
    pessimistic bound to report alongside, not as the base case.

    Symmetric in P and 1-P, so the same fee applies to either side of a trade.
    """
    config = config or get_config()
    settings = config.strategy
    price = np.asarray(price, dtype=float)
    rate = settings.fee_coefficient * price * (1.0 - price)

    if settings.fee_ceiling_per_contract:
        return (np.ceil(rate * 100.0) / 100.0) * contracts
    return rate * contracts


def kelly_fraction(price: float, true_prob: float, side: str) -> float:
    """Full-Kelly stake as a fraction of bankroll, before the fractional haircut.

    Kelly maximises the long-run growth rate of capital. For a bet paying net
    odds b with win probability w, it is `f* = (b*w - (1 - w)) / b`. Substituting
    a binary contract's economics gives two clean forms:

        buy YES at p, true probability q:   f* = (q - p) / (1 - p)
        buy NO  at p, true probability q:   f* = (p - q) / p

    Both are zero when q = p (no edge, no bet) and 1 when the outcome is
    certain, which is the sanity check worth remembering.

    Note what the denominators do. Buying YES at 0.98 divides by 0.02, so a
    small estimated edge produces an enormous Kelly number -- the estimate's
    own error is then levered up. `max_position_fraction` exists for that case
    and is not optional.

    Returns 0.0 for a non-positive edge rather than a negative fraction: the
    strategy declines the bet instead of taking the other side, since the other
    side is a different bucket's decision.
    """
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must lie strictly inside (0, 1), got {price}")
    if side == YES:
        fraction = (true_prob - price) / (1.0 - price)
    elif side == NO:
        fraction = (price - true_prob) / price
    else:
        raise ValueError(f"side must be {YES!r} or {NO!r}, got {side!r}")
    return float(max(0.0, fraction))


def bucket_rules(
    train: pd.DataFrame, config: Config | None = None, invert: bool = False
) -> pd.DataFrame:
    """Which buckets to trade, which side, and at what estimated edge.

    Computed from the training frame alone. This is the strategy's entire
    knowledge, and the reason the trading loop can be shown not to peek: it is
    handed this table and nothing else.

    A bucket is traded when it is **statistically real** (significant after the
    same clustered, corrected machinery the study reports) **and economically
    real** (its gross edge exceeds the fee at that price by `min_net_edge`).
    Both filters run on training data.

    The thresholds are therefore *derived*, never chosen. Picking "trade below
    20c" by eye after seeing the full-sample table would leak the answer
    through a parameter and make the split decorative -- the split would be
    honest while the rule that uses it was not.

    `invert` negates the estimated bias for the falsification control. Note it
    flips the *belief*, not merely the side: a trader who thinks longshots are
    underpriced both takes the other side AND expects the opposite edge. Only
    flipping the side would leave the position facing a negative Kelly
    fraction, so every trade would be declined and the control would take zero
    positions -- passing vacuously instead of losing money, which is the one
    outcome that would tell us nothing.
    """
    config = config or get_config()
    table = calibration_table(train, config=config)

    bias = table["bias"].to_numpy(dtype=float)
    if invert:
        bias = -bias

    fee = kalshi_fee(table["mean_price"].to_numpy(), config=config)
    gross = np.abs(bias)

    rules = pd.DataFrame(
        {
            "bucket": table["bucket"],
            "mean_price": table["mean_price"],
            "train_bias": bias,
            "significant": table["significant"],
            "fee": fee,
            "gross_edge": gross,
            "net_edge": gross - fee,
        }
    )
    # bias < 0 means the market priced it too high, so the profitable side is NO.
    rules["side"] = np.where(rules["train_bias"] < 0, NO, YES)
    rules["trade"] = rules["significant"] & (
        rules["net_edge"] > config.strategy.min_net_edge
    )
    return rules


def _ledger(
    test: pd.DataFrame, rules: pd.DataFrame, config: Config
) -> pd.DataFrame:
    """One row per trade taken. Sizing reads `rules` and price only."""
    settings = config.strategy
    n_buckets = config.analysis.n_buckets

    prices = test["implied_price"].to_numpy(dtype=float)
    buckets = assign_buckets(prices, n_buckets)
    by_bucket = rules.set_index("bucket")

    rows = []
    for index, (price, bucket) in enumerate(zip(prices, buckets)):
        if bucket not in by_bucket.index:
            continue
        rule = by_bucket.loc[bucket]
        if not rule["trade"]:
            continue
        if not 0.0 < price < 1.0:
            continue

        # The market's own price, corrected by the edge estimated in-sample for
        # this price level. Nothing about this contract's outcome is involved.
        estimate = float(np.clip(price + rule["train_bias"], 1e-6, 1 - 1e-6))
        side = str(rule["side"])
        raw_kelly = kelly_fraction(price, estimate, side)
        if raw_kelly <= 0.0:
            continue

        stake = min(raw_kelly * settings.kelly_fraction, settings.max_position_fraction)
        cost_per_contract = price if side == YES else 1.0 - price
        contracts = stake / cost_per_contract
        fee = float(kalshi_fee(price, contracts, config=config))
        # Crossing the spread. Unmeasurable from settled data, so 0 by default
        # and swept separately -- see `breakeven_slippage`.
        slippage = settings.slippage_per_contract * contracts

        outcome = int(test["outcome"].iloc[index])
        won = (outcome == 1) if side == YES else (outcome == 0)
        payout = contracts if won else 0.0
        pnl = payout - stake - fee - slippage

        # Cash actually leaving the account at entry: the contracts plus the
        # fee, which is charged on top of the stake rather than out of it.
        # This is the right denominator for a return -- dividing by `stake`
        # alone lets a total loss read as -1.06, and compounding a series with
        # a value below -1 drives the equity curve negative and makes every
        # statistic downstream of it meaningless.
        cost = stake + fee + slippage

        rows.append(
            {
                "ticker": test["ticker"].iloc[index] if "ticker" in test else index,
                "settle_ts": test["settle_ts"].iloc[index],
                "category": test["category"].iloc[index] if "category" in test else "?",
                "bucket": int(bucket),
                "price": price,
                "side": side,
                "estimate": estimate,
                "kelly_raw": raw_kelly,
                "stake": stake,
                "contracts": contracts,
                "fee": fee,
                "slippage": slippage,
                "cost": cost,
                "outcome": outcome,
                "won": bool(won),
                "pnl": pnl,
                "return_on_cost": pnl / cost if cost else 0.0,
            }
        )

    ledger = pd.DataFrame(rows)
    if ledger.empty:
        return ledger
    ledger = ledger.sort_values("settle_ts").reset_index(drop=True)
    return _apply_daily_budget(ledger, config)


def _apply_daily_budget(ledger: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Scale each day's positions to fit the portfolio's daily budget.

    Kelly sizes every bet as though it were the only one on the table. That is
    the right answer for a sequential gambler and the wrong one here: this
    strategy holds several hundred positions on a typical settlement day, so
    the per-position cap alone asks for roughly eleven times the bankroll. A
    backtest without this constraint is not optimistic, it is incoherent --
    it reports returns on capital nobody has.

    Days over budget are scaled **proportionally**, which preserves the
    relative Kelly weights within the day (the bets the edge liked most stay
    the largest) while making the total financeable. Fees scale with the
    position, since they are proportional to contracts.

    This is also what makes the equity curve meaningful. Without it, a day's
    return is a return on that day's deployed capital rather than on bankroll,
    and a total-loss day divides an equity curve by zero.
    """
    budget = config.strategy.max_daily_deployment
    day = pd.to_datetime(ledger["settle_ts"], utc=True).dt.date
    daily_cost = ledger.groupby(day)["cost"].transform("sum")
    scale = np.minimum(1.0, budget / daily_cost.to_numpy())

    scaled = ledger.copy()
    for column in ("stake", "contracts", "fee", "slippage", "cost", "pnl"):
        scaled[column] = ledger[column].to_numpy() * scale
    scaled["budget_scale"] = scale
    # Ratios are scale-invariant, so they are unchanged by construction.
    return scaled


def summarise(ledger: pd.DataFrame) -> dict[str, float]:
    """Headline metrics for a ledger.

    `roi` is total PnL over total capital **deployed** -- stake plus fee, the
    cash that actually leaves the account. Dividing by the stake alone lets a
    total loss read as -1.06 (you forfeit the stake *and* paid the fee), and a
    return below -1 compounds an equity curve straight through zero into
    negative territory, after which the drawdown is not merely wrong but
    unreadable. This is profit per dollar at risk, and it is the claim;
    everything else is shape.

    **Returns are aggregated to settlement days before any risk statistic.**
    Per-trade annualisation would treat 16,000 near-simultaneous positions as
    16,000 independent sequential bets and report a Sharpe around 8, which is
    an artefact of the arithmetic rather than a property of the strategy. A
    daily series respects the fact that a day's trades resolve together, and
    `sharpe_like` annualises it by sqrt(365).

    It is still not a portfolio Sharpe. Positions inside one day are correlated
    (the same events, the same sport), so even the daily series overstates
    diversification. Quote it as a shape statistic, not a performance claim.

    The equity curve compounds each day's return on the running bankroll, which
    is what makes `max_drawdown` mean anything -- summing raw PnL against a
    fixed notional produces a number with no interpretation.
    """
    if ledger.empty:
        return {
            "trades": 0, "roi": 0.0, "total_pnl": 0.0, "staked": 0.0,
            "hit_rate": 0.0, "sharpe_like": 0.0, "max_drawdown": 0.0,
            "fees_paid": 0.0, "gross_pnl": 0.0, "trading_days": 0,
            "deployed": 0.0, "daily_mean": 0.0, "daily_std": 0.0,
            "final_equity": 1.0, "breakeven_slippage": 0.0,
        }

    staked = float(ledger["stake"].sum())
    deployed = float(ledger["cost"].sum())
    pnl = float(ledger["pnl"].sum())
    fees = float(ledger["fee"].sum())

    # One row per settlement day: PnL earned that day per dollar deployed that
    # day. Days the strategy did not trade contribute nothing and are absent.
    daily = ledger.assign(day=pd.to_datetime(ledger["settle_ts"], utc=True).dt.date)
    grouped = daily.groupby("day").agg(pnl=("pnl", "sum"), cost=("cost", "sum"))

    # Return on the day's deployed capital -- the efficiency of the edge.
    daily_return = (grouped["pnl"] / grouped["cost"]).to_numpy()
    # Return on BANKROLL, which is what compounds. The daily budget caps
    # deployment at <= 1x bankroll, so this is bounded below by -1 and the
    # curve cannot pass through zero.
    daily_bankroll_return = grouped["pnl"].to_numpy()

    equity = np.cumprod(1.0 + daily_bankroll_return)
    running_peak = np.maximum.accumulate(equity)
    drawdown = float(np.min(equity / running_peak - 1.0))

    sharpe = (
        float(
            daily_bankroll_return.mean()
            / daily_bankroll_return.std(ddof=1)
            * math.sqrt(365.0)
        )
        if len(daily_bankroll_return) > 1 and daily_bankroll_return.std(ddof=1) > 0
        else 0.0
    )

    return {
        "trades": len(ledger),
        "roi": pnl / deployed if deployed else 0.0,
        "total_pnl": pnl,
        "gross_pnl": pnl + fees,
        "fees_paid": fees,
        "staked": staked,
        "deployed": deployed,
        "hit_rate": float(ledger["won"].mean()),
        "sharpe_like": sharpe,
        "daily_mean": float(daily_bankroll_return.mean()),
        "daily_std": (
            float(daily_bankroll_return.std(ddof=1))
            if len(daily_bankroll_return) > 1 else 0.0
        ),
        "final_equity": float(equity[-1]),
        "max_drawdown": drawdown,
        "trading_days": int(len(daily_return)),
        # The number that decides tradeability. Total profit divided by total
        # contracts bought: the adverse fill per contract that would erase the
        # entire edge. Compare it against a plausible half-spread before
        # quoting the ROI at anyone.
        #
        # First-order: charging it changes `cost`, which changes the daily
        # budget scaling, which feeds back on sizing, so the true root is
        # slightly different. Accurate to a couple of percent of PnL, which is
        # far finer than the spread uncertainty it is being compared against.
        "breakeven_slippage": (
            pnl / float(ledger["contracts"].sum())
            if ledger["contracts"].sum() else 0.0
        ),
    }


def backtest_bias_strategy(
    df: pd.DataFrame, config: Config | None = None
) -> tuple[pd.DataFrame, dict[str, float], Split, pd.DataFrame]:
    """Estimate the bias on early markets, trade it on later ones, pay fees.

    The rule: for each out-of-sample contract, look up the bucket its price
    falls in; if that bucket's *in-sample* edge was significant and survived
    the fee, take the side the in-sample bias points to, sized at fractional
    Kelly on the in-sample edge. Hold to settlement.

    Returns (ledger, metrics, split, rules).
    """
    config = config or get_config()
    split = time_split(df, config)
    rules = bucket_rules(split.train, config=config)
    ledger = _ledger(split.test, rules, config)
    return ledger, summarise(ledger), split, rules


def backtest_anti_bias_control(
    df: pd.DataFrame, config: Config | None = None
) -> tuple[pd.DataFrame, dict[str, float], Split, pd.DataFrame]:
    """The same machinery with every side flipped: buy longshots, fade favorites.

    This is a falsification test, not an ablation. If the favorite-longshot
    bias is real and the main strategy is sound, this must lose money
    out-of-sample -- it is paying the same fees to take the wrong side of a
    real effect. A finding that cannot be made to fail is not evidence, so the
    control's *negative* result is what makes the main result credible.

    It should lose by more than the main strategy wins, because it forfeits the
    edge and pays the fee rather than merely forfeiting the edge.
    """
    config = config or get_config()
    split = time_split(df, config)
    rules = bucket_rules(split.train, config=config, invert=True)
    ledger = _ledger(split.test, rules, config)
    return ledger, summarise(ledger), split, rules
