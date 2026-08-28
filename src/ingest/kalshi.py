"""Kalshi ingestion: settled binary markets -> immutable raw JSON on disk.

`KalshiClient` is the venue-specific half of the ingestion layer. `APIClient`
(src/ingest/base.py) already knows how to make a request that survives rate
limits; everything Kalshi-shaped lives here: which endpoints to hit, how a
category maps onto series, and where the bytes land.

How a run is structured
-----------------------
Kalshi has no "give me every settled market between two dates" endpoint.
`/historical/markets` is queried **per series**, with no date filter at all.
So a run is a nested walk:

    category -> its highest-volume series -> that series' pages of markets

and the date window is applied afterwards, in src/clean.py, against files that
are already on disk.

On-disk layout, one file per page::

    data/raw/kalshi/
      _cutoff.json                     <- live/historical boundary at run time
      Economics/
        _series.json                   <- which series were selected, and why
        KXCPI/
          page_0001.json               <- the server's response, verbatim
          page_0002.json

Idempotency
-----------
The milestone requirement is that a re-run skips work already done, so an
interrupted ingestion resumes instead of restarting. That lives here rather
than in `APIClient` because only this layer knows what a "unit of work" is
(one page of one series) and where its output goes.

The mechanism is just the filename: page N of a series always lands at the
same deterministic path, so `path.exists()` is the whole check. The subtlety
is that skipping a page still requires *reading* it -- the cursor for page N+1
is inside page N. So a resumed run reads cached pages off disk to recover its
place in the cursor chain, and makes no network call until it reaches the
first page it has never fetched. See docs/adr/002-ingestion-idempotency.md.

This is also why `APIClient.paginate()` is not used here: it always performs
the request. Recovering a cursor from disk means owning the loop.

Raw means raw
-------------
Each page is written exactly as the server sent it -- envelope, cursor, every
field, no filtering. Markets outside `date_range` are kept. Parsing into
`Contract` happens later in src/clean.py, reading these files, so a parsing
bug is fixable without re-hitting the API (docs/adr/001-storage-layers.md).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.config import Config, get_config
from src.ingest.base import APIClient

logger = logging.getLogger(__name__)

VENUE = "kalshi"

# Endpoints, confirmed against docs.kalshi.com and exercised in
# notebooks/00_api_scratch.ipynb.
CUTOFF_ENDPOINT = "/historical/cutoff"
SERIES_ENDPOINT = "/series"
HISTORICAL_MARKETS_ENDPOINT = "/historical/markets"

# Filenames that hold run metadata rather than a page of markets. The leading
# underscore keeps them sorting above the series directories.
CUTOFF_FILENAME = "_cutoff.json"
SERIES_FILENAME = "_series.json"


@dataclass
class IngestStats:
    """What a run actually did -- the "did it work" dashboard.

    `pages_skipped` is the idempotency counter: on a fresh run it is 0, and on
    a re-run of already-complete work it should equal the total page count
    with `pages_fetched` at 0.
    """

    pages_fetched: int = 0
    pages_skipped: int = 0
    markets_seen: int = 0
    series_walked: int = 0
    markets_per_category: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"series={self.series_walked} "
            f"pages_fetched={self.pages_fetched} pages_skipped={self.pages_skipped} "
            f"markets={self.markets_seen} by_category={self.markets_per_category}"
        )


def _parse_ts(raw: str | None) -> datetime | None:
    """Parse a Kalshi timestamp.

    They are RFC-3339 with a literal "Z", and `fromisoformat` wants a numeric
    offset. Returns None for a missing or malformed value rather than raising:
    one odd record must not abort a long ingestion run.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        logger.warning("event=unparsed_timestamp value=%r", raw)
        return None


def _volume(series: dict[str, Any]) -> float:
    """Total traded volume for a series, as a number.

    Kalshi returns `volume_fp` as a decimal *string* ("99935.00"). Sorting
    those without converting compares them lexicographically, which ranks
    "9917.00" above "10000000.00" -- silently selecting the series whose
    volume happens to start with a 9 rather than the largest ones. Always cast
    before comparing.
    """
    raw = series.get("volume_fp")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("event=unparsed_volume ticker=%s value=%r", series.get("ticker"), raw)
        return 0.0


def _write_json(path: Path, payload: Any) -> None:
    """Write atomically.

    A run killed mid-write must not leave a truncated file behind, because the
    resume logic treats any existing page file as a complete cached page.
    Writing to a temp name and renaming makes the file appear whole or not at
    all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class KalshiClient(APIClient):
    """Fetches settled Kalshi markets into the raw layer, resumably.

        with KalshiClient.from_config() as client:
            stats = client.fetch_settled_markets()
            print(stats.summary())
    """

    def __init__(self, config: Config, raw_dir: Path | None = None, **kwargs: Any) -> None:
        """
        Args:
            config: Validated config.yaml.
            raw_dir: Override the raw-layer root. Tests point this at a
                tmp_path; production leaves it None and takes
                config.ingest.raw_dir.
            **kwargs: Forwarded to APIClient -- notably `transport`, which
                tests use to inject an httpx.MockTransport.
        """
        super().__init__(
            base_url=config.kalshi_base_url,
            rate_limit_per_second=config.rate_limit_per_second,
            retry=config.retry,
            **kwargs,
        )
        self.config = config
        root = Path(raw_dir) if raw_dir is not None else Path(config.ingest.raw_dir)
        self.venue_dir = root / VENUE

    @classmethod
    def from_config(cls, **kwargs: Any) -> "KalshiClient":
        return cls(get_config(), **kwargs)

    # -- metadata ----------------------------------------------------------

    def fetch_cutoff(self) -> dict[str, Any]:
        """Fetch and record the live/historical boundary.

        Kalshi splits settled markets across two API surfaces: recent activity
        stays on the live endpoints, and older data moves behind /historical/*.
        This client reads only the historical side, so the cutoff is the newest
        settlement it can see -- which is why config.yaml pins date_range.end
        to it.

        The boundary moves as Kalshi migrates data, so it is saved alongside
        the pages: it is the record of what "as much history as available"
        meant on the day this run happened.
        """
        cutoff = self.get(CUTOFF_ENDPOINT)
        _write_json(self.venue_dir / CUTOFF_FILENAME, cutoff)
        logger.info("event=cutoff %s", cutoff)
        return cutoff

    def select_series(self, category: str) -> list[dict[str, Any]]:
        """The top-N highest-volume series for `category`, sub-daily ones dropped.

        Sub-daily recurring series are excluded *before* ranking, so they
        cannot crowd out the top-N slots. Exclusion matches the server-side
        `frequency` enum, never the ticker text -- an earlier version of this
        filter matched the substring "1h" and threw away Sports "1H" (First
        Half) markets, which are one-off in-game markets rather than
        hourly-recurring ones (docs/journal.md, 2026-08-05).

        The selection is cached to `<category>/_series.json` and reused on
        later runs. That is deliberate: re-ranking mid-study would silently
        change the sample as volumes shift. Delete the file to re-select.
        """
        cache_path = self.venue_dir / category / SERIES_FILENAME
        if cache_path.exists():
            cached = _read_json(cache_path)
            logger.info(
                "event=series_selection_cached category=%s n=%d",
                category,
                len(cached["selected"]),
            )
            return cached["selected"]

        payload = self.get(SERIES_ENDPOINT, {"category": category, "include_volume": "true"})
        all_series = payload.get("series", [])

        subdaily = self.config.ingest.subdaily_frequencies
        excluded = [s for s in all_series if s.get("frequency") in subdaily]
        candidates = [s for s in all_series if s.get("frequency") not in subdaily]
        candidates.sort(key=_volume, reverse=True)
        selected = [
            {"ticker": s.get("ticker"), "volume": _volume(s), "title": s.get("title")}
            for s in candidates[: self.config.ingest.top_n_series_per_category]
        ]

        _write_json(
            cache_path,
            {
                "category": category,
                "selected_at": datetime.now(timezone.utc).isoformat(),
                "total_series_in_category": len(all_series),
                "top_n": self.config.ingest.top_n_series_per_category,
                "excluded_subdaily": [
                    {"ticker": s.get("ticker"), "frequency": s.get("frequency")} for s in excluded
                ],
                "selected": selected,
            },
        )
        logger.info(
            "event=series_selected category=%s total=%d excluded_subdaily=%d selected=%d",
            category,
            len(all_series),
            len(excluded),
            len(selected),
        )
        return selected

    # -- the page walk -----------------------------------------------------

    def _walk_series(
        self, series_ticker: str, category: str, start_date: date, stats: IngestStats
    ) -> int:
        """Page through one series' settled markets, writing each page to disk.

        Returns the number of markets seen.

        Two things end the walk:

        1. The server stops returning a cursor -- the series is exhausted.
        2. A page's oldest market settled before `start_date`.
           `/historical/markets` returns markets in *descending* settlement
           order (verified to be monotonic across pages against the live API),
           so once a page runs past the start of our window every later page is
           older still. Without this the walk would pull a series' entire
           history to cover a six-month window. The page that triggers the stop
           is still written whole -- trimming it would break the raw layer's
           verbatim guarantee.
        """
        series_dir = self.venue_dir / category / series_ticker
        cursor: str | None = None
        page_number = 0
        markets_seen = 0

        while True:
            page_number += 1
            page_path = series_dir / f"page_{page_number:04d}.json"

            if page_path.exists():
                # Cached, but still parsed: page N holds page N+1's cursor.
                page = _read_json(page_path)
                stats.pages_skipped += 1
            else:
                params: dict[str, Any] = {
                    "series_ticker": series_ticker,
                    "limit": self.config.ingest.page_limit,
                }
                if cursor:
                    params["cursor"] = cursor
                page = self.get(HISTORICAL_MARKETS_ENDPOINT, params)
                _write_json(page_path, page)
                stats.pages_fetched += 1

            markets = page.get("markets", [])
            markets_seen += len(markets)

            if not markets:
                break

            settlements = [ts for ts in (_parse_ts(m.get("settlement_ts")) for m in markets) if ts]
            oldest = min(settlements) if settlements else None
            if oldest is not None and oldest.date() < start_date:
                logger.info(
                    "event=series_window_reached series=%s pages=%d oldest=%s",
                    series_ticker,
                    page_number,
                    oldest.date(),
                )
                break

            cursor = page.get("cursor") or None
            if cursor is None:
                logger.info(
                    "event=series_exhausted series=%s pages=%d", series_ticker, page_number
                )
                break

        return markets_seen

    # -- the run -----------------------------------------------------------

    def fetch_settled_markets(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        categories: list[str] | None = None,
    ) -> IngestStats:
        """Ingest settled markets for each category into the raw layer.

        Args:
            start_date: Oldest settlement to reach before a series walk stops.
                Defaults to config.yaml's date_range.start.
            end_date: Recorded in the run log for provenance. It does *not*
                filter here -- markets newer than it are written to the raw
                layer like any other and dropped later, in src/clean.py, so the
                window can be narrowed without re-fetching.
            categories: Defaults to config.yaml's categories.

        Returns:
            IngestStats for the run.
        """
        start_date = start_date or self.config.date_range.start
        end_date = end_date or self.config.date_range.end
        categories = categories or list(self.config.categories)

        logger.info(
            "event=ingest_start venue=%s start=%s end=%s categories=%s",
            VENUE,
            start_date,
            end_date,
            categories,
        )

        stats = IngestStats()
        self.fetch_cutoff()

        for category in categories:
            category_markets = 0
            for series in self.select_series(category):
                ticker = series.get("ticker")
                if not ticker:
                    continue
                category_markets += self._walk_series(ticker, category, start_date, stats)
                stats.series_walked += 1

            stats.markets_per_category[category] = category_markets
            stats.markets_seen += category_markets
            logger.info(
                "event=category_complete category=%s markets=%d", category, category_markets
            )

        logger.info("event=ingest_complete %s", stats.summary())
        return stats


def main() -> None:
    """Entry point for `make ingest`."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    with KalshiClient.from_config() as client:
        stats = client.fetch_settled_markets()
    print(stats.summary())


if __name__ == "__main__":
    main()
