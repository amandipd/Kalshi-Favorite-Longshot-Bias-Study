"""Pydantic schemas for a resolved prediction-market contract."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class Venue(str, Enum):
    """Exchange a contract traded on."""

    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Contract(BaseModel):
    """One resolved binary prediction-market contract, normalized across venues.

    Each row in the processed dataset is one settled contract, reframed so that
    implied_price always means P(event happens) -- never P(NO) or a raw YES quote
    that might actually represent the losing side. See
    docs/adr/003-implied-price-definition.md for how implied_price is computed
    and docs/adr/004-inclusion-criteria.md for the category taxonomy.

    Fields:
        venue: Exchange the contract traded on (kalshi or polymarket).
        ticker: Venue-native identifier for the market (e.g. Kalshi's market ticker).
        event_ticker: Identifier of the event the market belongs to. Contracts
            sharing one are not independent observations -- a 250-golfer field
            is one underlying outcome expressed as 250 contracts, and a
            threshold ladder is monotonically bound by construction. Carried on
            every row so Phase 3 can cluster standard errors on it; see
            docs/adr/004-inclusion-criteria.md, decision 6.
        category: Normalized topic taxonomy (e.g. politics, economics, sports) --
            not the venue's raw category string, which varies across venues.
        title: Human-readable market question, as quoted by the venue.
        implied_price: The market's quoted price for the event happening, read as
            a probability. A binary contract settles at $1 if the event happened
            or $0 if it didn't; a contract trading at $0.30 is the market's
            collective bet that there's a 30% chance it happens. Whether that
            reading is actually accurate -- whether implied_price is well
            calibrated against outcome -- is the entire empirical question this
            project measures.
        outcome: What actually happened -- 1 if the event occurred, 0 if it
            didn't. Always defined relative to the same event implied_price
            prices, never flipped for a NO-framed contract.
        volume: Total contracts traded -- a liquidity/confidence signal for how
            much weight to put on the price.
        open_ts: When the market opened for trading.
        close_ts: When trading closed. May precede settle_ts when settlement
            depends on an external event (e.g. an official data release) that
            takes time to confirm after trading stops.
        settle_ts: When the contract was resolved and outcome became known.
    """

    venue: Venue
    ticker: str
    event_ticker: str
    category: str
    title: str
    implied_price: float = Field(ge=0.0, le=1.0)
    outcome: int
    volume: float
    open_ts: datetime
    close_ts: datetime
    settle_ts: datetime

    @field_validator("outcome")
    @classmethod
    def outcome_is_binary(cls, v: int) -> int:
        if v not in (0, 1):
            raise ValueError(f"outcome must be 0 or 1, got {v}")
        return v

    @model_validator(mode="after")
    def settle_not_before_open(self) -> "Contract":
        if self.settle_ts < self.open_ts:
            raise ValueError(
                f"settle_ts ({self.settle_ts}) must be >= open_ts ({self.open_ts})"
            )
        return self
