"""raw -> interim -> processed transforms.

Pure functions: paths in, files out, no network. Everything here is a
deterministic re-derivation of the immutable raw layer, so deleting
`data/interim/` and `data/processed/` and re-running must reproduce them
exactly (docs/adr/001-storage-layers.md).

The split between the two steps is the split between *parsing* and
*judgment*:

    parse_raw_to_interim   what the API said, typed. Drops only records that
                           cannot be turned into a Contract at all.
    interim_to_processed   what is in the study. Every exclusion here is a
                           research decision, config-driven and logged.

Both log every dropped row with a reason and a running tally. Silent
filtering is the failure mode that matters: a row that vanishes without a
count cannot be argued about, and the rows most likely to vanish -- illiquid,
extreme-priced, short-lived -- are exactly the ones the favorite-longshot
hypothesis is about.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from src.config import Config, get_config
from src.ingest.trades import horizon_dirname
from src.models import Contract, Venue

logger = logging.getLogger(__name__)

VENUE = "kalshi"

# `result` values that describe a genuinely binary settlement.
BINARY_RESULTS = frozenset({"yes", "no"})

# Every settled market the historical endpoints return is `finalized`. This is
# asserted rather than filtered (design decision doc 004, decision 9): a row in another state
# means the ingestion layer picked up a market that has not resolved, which is
# a bug that would quietly contaminate `outcome`, not a row to drop quietly.
FINALIZED_STATUS = "finalized"

PRICE_METHODS = frozenset({"horizon_trade", "last_trade", "bid_ask_mid", "close_price"})


@dataclass
class DropLog:
    """Running tally of what was dropped and why.

    Reasons are coarse and fixed rather than free text so they can be counted,
    compared between runs, and reported in docs/cleaning-log.md. `by_category`
    is tracked alongside the total because an exclusion that falls unevenly
    across categories is a bias, not just a loss of sample.
    """

    kept: int = 0
    reasons: Counter = field(default_factory=Counter)
    by_reason_category: dict[str, Counter] = field(default_factory=dict)
    examples: dict[str, str] = field(default_factory=dict)

    def drop(self, reason: str, category: str = "?", example: str | None = None) -> None:
        self.reasons[reason] += 1
        self.by_reason_category.setdefault(reason, Counter())[category] += 1
        if example and reason not in self.examples:
            self.examples[reason] = example

    @property
    def dropped(self) -> int:
        return sum(self.reasons.values())

    @property
    def seen(self) -> int:
        return self.kept + self.dropped

    def format(self) -> str:
        if not self.seen:
            return "nothing seen"
        lines = [
            f"seen={self.seen:,}  kept={self.kept:,} ({self.kept / self.seen:.1%})  "
            f"dropped={self.dropped:,}",
        ]
        for reason, count in self.reasons.most_common():
            cats = self.by_reason_category[reason]
            spread = " ".join(f"{c}={n:,}" for c, n in cats.most_common())
            lines.append(f"    {reason:<28} {count:>8,}  ({spread})")
            if reason in self.examples:
                lines.append(f"      e.g. {self.examples[reason]}")
        return "\n".join(lines)

    def log(self, step: str) -> None:
        logger.info("event=%s_complete kept=%d dropped=%d", step, self.kept, self.dropped)
        for reason, count in self.reasons.most_common():
            logger.info("event=dropped step=%s reason=%s count=%d", step, reason, count)


# -- prices ----------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """Kalshi returns every price and quantity as a decimal string."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_implied_price(
    market: dict[str, Any],
    method: str = "horizon_trade",
    trade: dict[str, Any] | None = None,
) -> float | None:
    """The market's implied P(event), or None if this method cannot supply one.

    Always the YES leg. Kalshi reports `yes_price_dollars` on every trade
    regardless of which side the taker took, so no flip is needed here;
    NO-framing normalisation is a separate concern (design decision doc 004).

    Methods, and the trade-off each makes:

    `horizon_trade` (the study's choice)
        Price of the last trade at or before `close_time - horizon`, taken
        from the second ingestion pass. The only method that samples the
        market while it was still uncertain. Costs an extra API pass, and is
        None for markets that had not traded that early -- about 11% at T-1h.
        A last trade can also be stale in a thin market, and with no order
        book in the historical data there is no way to detect that per market.

    `last_trade`
        The settled snapshot's `last_price_dollars`. Free, present on every
        record, and **degenerate**: 92.5% of binary markets have it pinned to
        the settlement value, because the market had stopped being uncertain
        by close. Retained so the writeup can plot the near-perfect curve it
        produces as a demonstration of the trap, never as a headline result.

    `bid_ask_mid`
        Midpoint of the YES book. Standard practice on live data and
        **unavailable** here: settled snapshots carry no book, with 54.9% of
        markets quoting bid 0.0000 / ask 1.0000 and open interest zero
        everywhere. Returns None rather than a fabricated 0.50 when the quote
        is that wide.

    `close_price`
        `previous_price_dollars`, the settled-day reference. Identical to
        `last_trade` on 96.5% of records, so it inherits the same defect.

    Args:
        market: One market record from the raw layer, verbatim.
        method: One of PRICE_METHODS.
        trade: The horizon trade record for this market, required by
            `horizon_trade` and ignored by the others. None means the market
            had no trade before the horizon.

    Returns:
        A price in [0, 1], or None when this method has no usable price.
    """
    if method not in PRICE_METHODS:
        raise ValueError(f"unknown price_method {method!r}; expected one of {sorted(PRICE_METHODS)}")

    if method == "horizon_trade":
        if not trade:
            return None
        price = _as_float(trade.get("yes_price_dollars"))
    elif method == "last_trade":
        price = _as_float(market.get("last_price_dollars"))
    elif method == "close_price":
        price = _as_float(market.get("previous_price_dollars"))
    else:  # bid_ask_mid
        bid = _as_float(market.get("yes_bid_dollars"))
        ask = _as_float(market.get("yes_ask_dollars"))
        if bid is None or ask is None:
            return None
        # A 0/1 quote is the absence of a book, not a 50c view. Returning None
        # keeps a fabricated 0.50 out of the calibration curve.
        if bid <= 0.0 and ask >= 1.0:
            return None
        price = (bid + ask) / 2.0

    if price is None or not (0.0 <= price <= 1.0):
        return None
    return price


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


# -- raw -> interim --------------------------------------------------------


def load_horizon_trades(
    trades_dir: Path | str, horizon_hours: float, venue: str = VENUE
) -> dict[str, dict[str, Any] | None]:
    """Map ticker -> horizon trade record (or None) for one horizon.

    A ticker present with a None value is meaningful and distinct from a
    ticker that is absent: the first means "asked, no trade existed that
    early", the second means "never fetched". They are dropped for different
    reasons and counted separately.
    """
    root = Path(trades_dir) / venue / horizon_dirname(horizon_hours)
    trades: dict[str, dict[str, Any] | None] = {}
    if not root.exists():
        return trades

    for path in sorted(root.rglob("*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # A run killed mid-append can leave one partial line.
                    logger.warning("event=malformed_trade_line path=%s", path)
                    continue
                trades[record["ticker"]] = record.get("trade")
    return trades


def iter_raw_markets(raw_dir: Path | str, venue: str = VENUE) -> Iterator[tuple[str, dict]]:
    """Yield `(category, market)` for every market page in the raw layer.

    Yields duplicates as they appear on disk; deduplication is a processed-layer
    decision so the interim layer can report how many there were.
    """
    venue_dir = Path(raw_dir) / venue
    if not venue_dir.exists():
        return
    for category_dir in sorted(p for p in venue_dir.iterdir() if p.is_dir()):
        for series_dir in sorted(p for p in category_dir.iterdir() if p.is_dir()):
            for page_path in sorted(series_dir.glob("page_*.json")):
                page = json.loads(page_path.read_text(encoding="utf-8"))
                for market in page.get("markets", []):
                    yield category_dir.name, market


def parse_raw_to_interim(
    raw_dir: Path | str,
    trades_dir: Path | str,
    out_path: Path | str,
    config: Config | None = None,
) -> tuple[pd.DataFrame, DropLog]:
    """Parse the raw layer into typed Contract rows and write Parquet.

    Drops only what cannot become a Contract: a non-binary settlement, a
    missing price, a missing timestamp, a value the model rejects. Research
    filtering -- the date window, volume floors, deduplication -- belongs to
    `interim_to_processed`, so that the two kinds of loss can be reported
    separately.

    Returns the DataFrame and the drop log, so a caller can inspect both
    without re-reading the file.
    """
    config = config or get_config()
    method = config.clean.price_method
    horizon = config.clean.price_horizon_hours

    trades = load_horizon_trades(trades_dir, horizon) if method == "horizon_trade" else {}
    if method == "horizon_trade":
        logger.info(
            "event=trades_loaded horizon=%s markets=%d with_trade=%d",
            horizon_dirname(horizon),
            len(trades),
            sum(1 for t in trades.values() if t),
        )

    log = DropLog()
    rows: list[dict[str, Any]] = []

    for category, market in iter_raw_markets(raw_dir):
        ticker = market.get("ticker") or "?"
        result = market.get("result")

        status = market.get("status")
        if status != FINALIZED_STATUS:
            raise ValueError(
                f"{ticker}: expected status={FINALIZED_STATUS!r}, got {status!r}. "
                "An unsettled market has no trustworthy `result`; see design decision doc 004."
            )

        # Not a binary contract. `scalar` markets settle at fractional values
        # (0.45, 0.53, ...) and are a different object entirely.
        if result not in BINARY_RESULTS:
            log.drop(f"non_binary_result:{result or 'empty'}", category, ticker)
            continue

        # Without it the row cannot be clustered, and an unclustered interval is
        # wrong rather than merely wide (design decision doc 004, decision 6). Zero rows are
        # missing it today; this counts any that ever are instead of pooling
        # them under one empty key.
        event_ticker = market.get("event_ticker")
        if not event_ticker:
            log.drop("missing_event_ticker", category, ticker)
            continue

        if method == "horizon_trade" and ticker not in trades:
            log.drop("horizon_not_ingested", category, ticker)
            continue

        price = compute_implied_price(market, method, trades.get(ticker))
        if price is None:
            reason = "no_trade_before_horizon" if method == "horizon_trade" else "no_usable_price"
            log.drop(reason, category, ticker)
            continue

        open_ts = _parse_ts(market.get("open_time"))
        close_ts = _parse_ts(market.get("close_time"))
        settle_ts = _parse_ts(market.get("settlement_ts"))
        if open_ts is None or close_ts is None or settle_ts is None:
            log.drop("missing_timestamp", category, ticker)
            continue

        volume = _as_float(market.get("volume_fp"))
        if volume is None:
            log.drop("unparsable_volume", category, ticker)
            continue

        try:
            contract = Contract(
                venue=Venue.KALSHI,
                ticker=ticker,
                event_ticker=event_ticker,
                category=category,
                title=market.get("title") or "",
                implied_price=price,
                outcome=1 if result == "yes" else 0,
                volume=volume,
                open_ts=open_ts,
                close_ts=close_ts,
                settle_ts=settle_ts,
            )
        except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError and friends
            # The model's own invariants (price in [0,1], settle >= open) are
            # the last gate. A rejection here is a data anomaly worth counting,
            # not a crash worth aborting 145k rows for.
            log.drop("model_validation_failed", category, f"{ticker}: {exc}")
            continue

        row = contract.model_dump()
        row["venue"] = contract.venue.value
        rows.append(row)
        log.kept += 1

    df = pd.DataFrame(rows)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)

    log.log("parse_raw_to_interim")
    logger.info("event=interim_written path=%s rows=%d", out_path, len(df))
    return df, log


# -- interim -> processed --------------------------------------------------


def interim_to_processed(
    interim_path: Path | str,
    out_path: Path | str,
    config: Config | None = None,
) -> tuple[pd.DataFrame, DropLog]:
    """Apply the study's inclusion criteria and write the analysis table.

    Every exclusion here is a research decision from
    docs/adr/004-inclusion-criteria.md, not a data defect. Two filters do the
    work -- the close-time window and a non-zero volume -- and the design decision doc's other
    decisions are deliberately *absent* filters: no volume floor, no
    de-duplication by event, no NO-framing flip. Each was considered and
    rejected because it would select on something downstream of the effect
    being measured, so their absence is as much a decision as the two that run.

    `ticker` uniqueness and `event_ticker` presence are asserted rather than
    filtered: both are invariants the ingest and parse layers already
    guarantee, so a violation is a bug rather than a row to quietly drop.
    """
    config = config or get_config()
    df = pd.read_parquet(interim_path)
    log = DropLog()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if df.empty:
        df.to_parquet(out_path, index=False)
        return df, log

    # Decision 8. Zero duplicates observed across 145,047 ingested markets, so
    # this keeps a property true rather than doing work. A market priced twice
    # would be counted twice in every bucket it lands in.
    duplicates = df["ticker"].duplicated()
    if duplicates.any():
        offenders = ", ".join(df.loc[duplicates, "ticker"].head(3))
        raise ValueError(
            f"{int(duplicates.sum())} duplicate ticker(s) in the interim layer, "
            f"e.g. {offenders}. De-duplication is an ingest-layer invariant; "
            "see design decision doc 004, decision 8."
        )

    # Decision 6. A row with no event cannot be clustered, and an unclustered
    # interval is wrong rather than merely narrow.
    if (df["event_ticker"].fillna("") == "").any():
        raise ValueError(
            "interim rows are missing `event_ticker`, so they cannot be clustered. "
            "See design decision doc 004, decision 6."
        )

    # Decision 2: the window is on close_time, the anchor the horizon price is
    # measured against. Both bounds are inclusive whole days, so `end` covers
    # everything up to that day's final second.
    start = pd.Timestamp(config.date_range.start, tz="UTC")
    end = pd.Timestamp(config.date_range.end, tz="UTC") + pd.Timedelta(days=1)
    close = pd.to_datetime(df["close_ts"], utc=True)
    in_window = (close >= start) & (close < end)

    # Decision 3. Closer to definitional than discretionary: a market that
    # never traded has no trade at any horizon, so rows reaching here should
    # almost all pass. It stays an explicit criterion because it is one, and
    # because it would bite if the price method were switched to a snapshot
    # field, which needs no trade to report a number.
    traded = df["volume"] > 0

    # Reasons are exclusive and ordered, so the counts sum to the rows removed.
    for reason, mask in (
        ("close_outside_window", ~in_window),
        ("no_volume", in_window & ~traded),
    ):
        if not mask.any():
            continue
        for category, count in df.loc[mask, "category"].value_counts().items():
            log.reasons[reason] += int(count)
            log.by_reason_category.setdefault(reason, Counter())[str(category)] += int(count)
        log.examples.setdefault(reason, str(df.loc[mask, "ticker"].iloc[0]))

    kept = df[in_window & traded].reset_index(drop=True)
    log.kept = len(kept)

    kept.to_parquet(out_path, index=False)

    log.log("interim_to_processed")
    logger.info(
        "event=processed_written path=%s rows=%d events=%d",
        out_path,
        len(kept),
        kept["event_ticker"].nunique() if len(kept) else 0,
    )
    return kept, log


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    config = get_config()
    df, log = parse_raw_to_interim(
        config.ingest.raw_dir,
        config.ingest.trades.trades_dir,
        config.clean.interim_path,
        config,
    )
    print("raw -> interim")
    print(log.format())
    print(f"  wrote {len(df):,} rows to {config.clean.interim_path}")

    processed, plog = interim_to_processed(
        config.clean.interim_path, config.clean.processed_path, config
    )
    print("\ninterim -> processed")
    print(plog.format())
    events = processed["event_ticker"].nunique() if len(processed) else 0
    print(
        f"  wrote {len(processed):,} rows ({events:,} events) "
        f"to {config.clean.processed_path}"
    )


if __name__ == "__main__":
    main()
