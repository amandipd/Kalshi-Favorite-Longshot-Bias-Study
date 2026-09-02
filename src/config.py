"""Typed loader for config.yaml.

Every research decision and operational knob lives in config.yaml, not in code
(see PROPOSAL.MD Part 2.3, "Why config-driven"). This module is the single place
that file is read and validated, so a typo or a missing key fails loudly at
startup rather than silently as a `None` deep inside an ingestion loop.

    from src.config import load_config
    cfg = load_config()
    cfg.date_range.start          # datetime.date
    cfg.retry.max_attempts        # int
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

# config.yaml sits at the repo root, one level above src/.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class DateRange(BaseModel):
    """Inclusive window the study covers, applied to each market's `close_time`.

    Close rather than settlement: close is the anchor the horizon price is
    measured against (design decision doc 003), so it is the timestamp that says when the
    forecast was made. Settlement can lag it by an arbitrary settlement timer.
    Both bounds are inclusive whole days in UTC (design decision doc 004, decision 2).
    """

    start: date
    end: date

    @model_validator(mode="after")
    def end_not_before_start(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError(f"date_range.end ({self.end}) must be >= start ({self.start})")
        return self


class RetryConfig(BaseModel):
    """Backoff policy shared by every venue client.

    Attributes:
        max_attempts: Total tries including the first, so 5 means one attempt
            plus four retries.
        initial_backoff_seconds: Base of the exponential schedule.
        max_backoff_seconds: Ceiling on any single wait, including one derived
            from a server's Retry-After header.
        jitter_seconds: Upper bound on the uniform random noise added to each
            computed backoff, to keep concurrent retries from re-colliding.
        timeout_seconds: Per-request httpx timeout.
    """

    max_attempts: int = Field(ge=1)
    initial_backoff_seconds: float = Field(gt=0)
    max_backoff_seconds: float = Field(gt=0)
    jitter_seconds: float = Field(ge=0)
    timeout_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def max_backoff_at_least_initial(self) -> "RetryConfig":
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                f"retry.max_backoff_seconds ({self.max_backoff_seconds}) must be >= "
                f"initial_backoff_seconds ({self.initial_backoff_seconds})"
            )
        return self


class TradesConfig(BaseModel):
    """The second ingestion pass: a price per market at a horizon before close.

    The settled-market snapshot cannot supply an implied probability -- 92.5%
    of its last prices are pinned to the settlement value, because the market
    had stopped being uncertain. See docs/adr/003-implied-price-definition.md.
    This pass fetches the last trade at or before `close_time - horizon`.

    Attributes:
        trades_dir: Root for this pass, kept separate from the market pages so
            the two are independently re-runnable.
        horizons_hours: Hours before close to price at. Each is a separate,
            independently resumable run; the first is the study's primary
            horizon and the rest are sensitivity analyses.
        request_limit: Trades per request. 1 is all that is needed -- the
            endpoint returns newest-first, so the first row *is* the last
            trade at or before the cutoff.
    """

    trades_dir: str
    horizons_hours: list[float] = Field(min_length=1)
    request_limit: int = Field(default=1, ge=1, le=1000)

    @model_validator(mode="after")
    def horizons_positive_and_unique(self) -> "TradesConfig":
        if any(h <= 0 for h in self.horizons_hours):
            raise ValueError(f"horizons_hours must all be > 0, got {self.horizons_hours}")
        if len(set(self.horizons_hours)) != len(self.horizons_hours):
            raise ValueError(f"horizons_hours must be unique, got {self.horizons_hours}")
        return self

    @property
    def primary_horizon_hours(self) -> float:
        """The horizon the headline result is computed at -- the first listed."""
        return self.horizons_hours[0]


class IngestConfig(BaseModel):
    """Scope and on-disk layout for an ingestion run.

    Attributes:
        raw_dir: Root of the immutable raw layer, relative to the repo root.
            Venue clients write under `<raw_dir>/<venue>/`.
        top_n_series_per_category: How many of a category's highest-volume
            series to walk. Kalshi's historical endpoint is queried per series,
            so this is what bounds the size of the sample.
        page_limit: Markets requested per page (Kalshi's maximum is 200).
        subdaily_frequencies: Values of a series' `frequency` field that mark it
            as recurring more often than daily, and therefore excluded.
        trades: Settings for the horizon-price pass.
    """

    raw_dir: str
    top_n_series_per_category: int = Field(ge=1)
    page_limit: int = Field(ge=1, le=200)
    subdaily_frequencies: frozenset[str] = frozenset()
    trades: TradesConfig


class CleanConfig(BaseModel):
    """How raw JSON becomes the analysis-ready table.

    Attributes:
        price_method: Which quoted number is read as P(event). Only
            `horizon_trade` is defensible for this study; the others exist so
            the writeup can *show* the curve they produce rather than assert
            they are wrong. See docs/adr/003-implied-price-definition.md.
        price_horizon_hours: Which ingested horizon to price at. Must be one
            of ingest.trades.horizons_hours.
        interim_path: Parsed, typed, one row per contract.
        processed_path: Filtered and normalised; what the analysis reads.
    """

    price_method: str
    price_horizon_hours: float = Field(gt=0)
    interim_path: str
    processed_path: str


class AnalysisConfig(BaseModel):
    """How calibration is measured and how uncertainty around it is computed.

    Attributes:
        n_buckets: Equal-width buckets on implied_price for the reliability
            diagram.
        confidence: Two-sided coverage of every reported interval.
        cluster_on: Column whose groups are resampled as blocks. Contracts
            sharing an event are one outcome expressed many times; treating
            them as independent understates every interval. Design decision doc 004 decision 6
            forbids reporting an unclustered interval.
        bootstrap_reps: Block-bootstrap replications.
        bootstrap_seed: Fixed so the same processed table always yields the
            same intervals.
        fdr_alpha: Benjamini-Hochberg level across the per-bucket tests.
        segment_n_buckets: Buckets used for per-category and per-lifetime
            tables. Coarser than the headline deciles because the slices
            are thinner; fixed from segment sizes, not segment results.
        min_events_per_bucket: A segment bucket with fewer events is
            reported and flagged rather than tested, and is excluded from
            the correction family.
    """

    n_buckets: int = Field(ge=2)
    confidence: float = Field(gt=0.0, lt=1.0)
    cluster_on: str
    bootstrap_reps: int = Field(ge=100)
    bootstrap_seed: int
    fdr_alpha: float = Field(gt=0.0, lt=1.0)
    segment_n_buckets: int = Field(ge=2)
    min_events_per_bucket: int = Field(ge=1)


class StrategyConfig(BaseModel):
    """The trading rule, its sizing, and what a trade costs.

    Attributes:
        train_fraction: Share of contracts in the estimation period.
        fee_coefficient: Kalshi's taker fee coefficient in
            fee = ceil(k * C * P * (1 - P)).
        fee_ceiling_per_contract: Round the fee up per contract (expensive
            reading) rather than per order.
        min_net_edge: Minimum fee-adjusted in-sample edge to trade a bucket.
        kelly_fraction: Multiplier on full Kelly.
        max_position_fraction: Cap on one position as a share of bankroll.
        slippage_per_contract: Adverse fill cost per contract, on top of
            the fee. 0.0 makes the result a no-spread upper bound.
        max_daily_deployment: Cap on total capital deployed on one
            settlement day, as a multiple of bankroll. Kelly sizes each
            bet in isolation; this is the portfolio constraint that
            stops hundreds of concurrent positions summing past 100%.
    """

    train_fraction: float = Field(gt=0.0, lt=1.0)
    fee_coefficient: float = Field(ge=0.0)
    fee_ceiling_per_contract: bool
    min_net_edge: float = Field(ge=0.0)
    kelly_fraction: float = Field(gt=0.0, le=1.0)
    max_position_fraction: float = Field(gt=0.0, le=1.0)
    max_daily_deployment: float = Field(gt=0.0)
    slippage_per_contract: float = Field(ge=0.0)


class Config(BaseModel):
    """The whole of config.yaml, validated."""

    date_range: DateRange
    categories: list[str] = Field(min_length=1)
    kalshi_base_url: str
    polymarket_base_url: str
    rate_limit_per_second: float = Field(gt=0)
    retry: RetryConfig
    ingest: IngestConfig
    clean: CleanConfig
    analysis: AnalysisConfig
    strategy: StrategyConfig

    @model_validator(mode="after")
    def price_horizon_was_ingested(self) -> "Config":
        """Fail at startup, not after a parse, if the study prices at a horizon
        nothing ever fetched."""
        horizons = self.ingest.trades.horizons_hours
        if self.clean.price_method == "horizon_trade" and (
            self.clean.price_horizon_hours not in horizons
        ):
            raise ValueError(
                f"clean.price_horizon_hours ({self.clean.price_horizon_hours}) is not in "
                f"ingest.trades.horizons_hours ({horizons}) -- that horizon was never ingested"
            )
        return self


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Read and validate config.yaml. Raises on a missing or malformed key."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """load_config() memoized against the default path, for call sites that just
    want the config and don't care about re-reading it."""
    return load_config()
