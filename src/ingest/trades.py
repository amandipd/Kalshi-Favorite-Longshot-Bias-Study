"""Second ingestion pass: the price each market was forecasting before close.

    python -m src.ingest.trades                  # every configured horizon
    python -m src.ingest.trades --horizon 1      # just T-1h

The first pass (`src/ingest/kalshi.py`) gives one post-settlement snapshot per
market, and its `last_price` cannot be used as an implied probability: 92.5%
of binary markets have it pinned to the settlement value. That is not a bug in
the data -- the market had genuinely stopped being uncertain by then, since the
median Kalshi market lives only ~24 hours. A price sampled at the moment of
resolution is not a forecast.

This pass asks a different endpoint, `/historical/trades`, for the last trade
at or before `close_time - horizon`, which is a price the market held while it
was still forecasting. Full reasoning and the horizon sweep that chose 1h:
docs/adr/003-implied-price-definition.md.

Layout, one JSONL file per series per horizon:

    data/raw_trades/kalshi/T1h/<category>/<series>.jsonl

One line per market. JSONL rather than a file per market because there are
~143k markets: 80-odd append-only files sync and copy sanely where 143k tiny
ones do not. Resumption reads back the tickers already in the file, so a
killed run resumes at the market it was on and a re-run fetches nothing.

Like the first pass, this writes what the API returned and defers every
judgment to src/clean.py -- including which trade becomes `implied_price`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config import Config, get_config
from src.ingest.base import APIClient

logger = logging.getLogger(__name__)

VENUE = "kalshi"
TRADES_ENDPOINT = "/historical/trades"

# Settlement values that mark a genuinely binary contract. `scalar` markets
# settle at fractional values (0.45, 0.53, ...) and are not the object this
# study is about, so they are never fetched -- see ADR 004.
BINARY_RESULTS = frozenset({"yes", "no"})


def horizon_dirname(hours: float) -> str:
    """`1.0 -> 'T1h'`, `0.5 -> 'T0.5h'`. Stable, so the path is the resume key."""
    trimmed = int(hours) if float(hours).is_integer() else hours
    return f"T{trimmed}h"


@dataclass
class TradeStats:
    """What a horizon run did.

    `no_trade` is not a failure: a market with no trade before the cutoff
    simply did not exist, or had not traded, that far ahead of its close. It is
    a documented exclusion with a count (ADR 003), which is why it is recorded
    on disk as an explicit null rather than left absent.
    """

    horizon_hours: float
    markets_total: int = 0
    fetched: int = 0
    skipped_cached: int = 0
    no_trade: int = 0
    errors: int = 0
    no_trade_per_category: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        pct = (100.0 * self.no_trade / self.fetched) if self.fetched else 0.0
        return (
            f"horizon={horizon_dirname(self.horizon_hours)} "
            f"markets={self.markets_total} fetched={self.fetched} "
            f"cached={self.skipped_cached} no_trade={self.no_trade} ({pct:.1f}% of fetched) "
            f"errors={self.errors}"
        )


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def iter_binary_markets(raw_dir: Path | str, venue: str = VENUE) -> Iterator[dict[str, Any]]:
    """Yield every settled binary market in the raw layer, once each.

    Reads the first pass's pages and emits `{ticker, category, series,
    close_time}`. Deduplicates by ticker, since a market appearing under two
    series would otherwise be priced twice.
    """
    venue_dir = Path(raw_dir) / venue
    if not venue_dir.exists():
        return

    seen: set[str] = set()
    for category_dir in sorted(p for p in venue_dir.iterdir() if p.is_dir()):
        for series_dir in sorted(p for p in category_dir.iterdir() if p.is_dir()):
            for page_path in sorted(series_dir.glob("page_*.json")):
                page = json.loads(page_path.read_text(encoding="utf-8"))
                for market in page.get("markets", []):
                    ticker = market.get("ticker")
                    if not ticker or ticker in seen:
                        continue
                    if market.get("result") not in BINARY_RESULTS:
                        continue
                    if not market.get("close_time"):
                        continue
                    seen.add(ticker)
                    yield {
                        "ticker": ticker,
                        "category": category_dir.name,
                        "series": series_dir.name,
                        "close_time": market["close_time"],
                    }


def _load_done(path: Path) -> set[str]:
    """Tickers already recorded in a JSONL file.

    Tolerates a truncated final line: a run killed mid-append can leave one,
    and the fix is to re-fetch that single market rather than to distrust the
    file. Anything malformed is simply not counted as done.
    """
    if not path.exists():
        return set()
    done: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["ticker"])
            except (json.JSONDecodeError, KeyError):
                logger.warning("event=malformed_line path=%s", path)
    return done


class KalshiTradesClient(APIClient):
    """Prices every settled binary market at one or more horizons before close.

        with KalshiTradesClient.from_config() as client:
            for stats in client.fetch_all_horizons():
                print(stats.summary())
    """

    def __init__(
        self,
        config: Config,
        raw_dir: Path | None = None,
        trades_dir: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url=config.kalshi_base_url,
            rate_limit_per_second=config.rate_limit_per_second,
            retry=config.retry,
            **kwargs,
        )
        self.config = config
        self.raw_dir = Path(raw_dir) if raw_dir is not None else Path(config.ingest.raw_dir)
        root = (
            Path(trades_dir) if trades_dir is not None else Path(config.ingest.trades.trades_dir)
        )
        self.venue_dir = root / VENUE

    @classmethod
    def from_config(cls, **kwargs: Any) -> "KalshiTradesClient":
        return cls(get_config(), **kwargs)

    def horizon_dir(self, hours: float) -> Path:
        return self.venue_dir / horizon_dirname(hours)

    def fetch_last_trade(self, ticker: str, cutoff: datetime) -> dict[str, Any] | None:
        """The last trade on `ticker` at or before `cutoff`, or None.

        `/historical/trades` returns newest-first, so `limit=1` with
        `max_ts=<cutoff>` gives exactly the trade we want in one request --
        no paging back through a market's history.
        """
        payload = self.get(
            TRADES_ENDPOINT,
            {
                "ticker": ticker,
                "limit": self.config.ingest.trades.request_limit,
                "max_ts": int(cutoff.timestamp()),
            },
        )
        trades = payload.get("trades") or []
        return trades[0] if trades else None

    def fetch_horizon(self, hours: float) -> TradeStats:
        """Price every binary market at `close_time - hours`.

        Markets are grouped by series so each series' JSONL is opened once and
        appended to as the run proceeds -- a killed run keeps everything it had
        already written.
        """
        stats = TradeStats(horizon_hours=hours)
        out_root = self.horizon_dir(hours)
        delta = timedelta(hours=hours)

        by_series: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for market in iter_binary_markets(self.raw_dir):
            by_series.setdefault((market["category"], market["series"]), []).append(market)
            stats.markets_total += 1

        logger.info(
            "event=horizon_start horizon=%s markets=%d series=%d",
            horizon_dirname(hours),
            stats.markets_total,
            len(by_series),
        )

        for (category, series), markets in sorted(by_series.items()):
            out_path = out_root / category / f"{series}.jsonl"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            done = _load_done(out_path)

            pending = [m for m in markets if m["ticker"] not in done]
            stats.skipped_cached += len(markets) - len(pending)
            if not pending:
                continue

            with out_path.open("a", encoding="utf-8") as fh:
                for market in pending:
                    close = _parse_ts(market["close_time"])
                    if close is None:
                        stats.errors += 1
                        continue
                    cutoff = close - delta

                    try:
                        trade = self.fetch_last_trade(market["ticker"], cutoff)
                    except Exception as exc:  # noqa: BLE001 -- one bad market must not end the run
                        stats.errors += 1
                        logger.warning(
                            "event=trade_fetch_failed ticker=%s error=%s", market["ticker"], exc
                        )
                        continue

                    if trade is None:
                        stats.no_trade += 1
                        stats.no_trade_per_category[category] = (
                            stats.no_trade_per_category.get(category, 0) + 1
                        )

                    # An explicit null records "we asked, there was nothing" --
                    # so a re-run does not ask again, and clean.py can count
                    # the exclusion instead of inferring it from an absence.
                    fh.write(
                        json.dumps(
                            {
                                "ticker": market["ticker"],
                                "category": category,
                                "series": series,
                                "horizon_hours": hours,
                                "close_time": market["close_time"],
                                "cutoff_ts": cutoff.isoformat(),
                                "fetched_at": datetime.now(timezone.utc).isoformat(),
                                "trade": trade,
                            }
                        )
                        + "\n"
                    )
                    stats.fetched += 1

                    if stats.fetched % 2000 == 0:
                        fh.flush()
                        os.fsync(fh.fileno())
                        logger.info("event=progress %s", stats.summary())

        logger.info("event=horizon_complete %s", stats.summary())
        return stats

    def fetch_all_horizons(self, horizons: list[float] | None = None) -> list[TradeStats]:
        """Run every configured horizon in order, primary first."""
        horizons = horizons or list(self.config.ingest.trades.horizons_hours)
        return [self.fetch_horizon(h) for h in horizons]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch horizon prices for settled markets.")
    parser.add_argument(
        "--horizon",
        type=float,
        action="append",
        default=None,
        help="Hours before close. Repeatable. Defaults to config.yaml's horizons_hours.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    with KalshiTradesClient.from_config() as client:
        for stats in client.fetch_all_horizons(args.horizon):
            print(stats.summary())


if __name__ == "__main__":
    main()
