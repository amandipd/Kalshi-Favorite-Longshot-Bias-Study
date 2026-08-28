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
    """Inclusive window of settlement dates the study covers."""

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


class Config(BaseModel):
    """The whole of config.yaml, validated."""

    date_range: DateRange
    categories: list[str] = Field(min_length=1)
    kalshi_base_url: str
    polymarket_base_url: str
    rate_limit_per_second: float = Field(gt=0)
    retry: RetryConfig


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
