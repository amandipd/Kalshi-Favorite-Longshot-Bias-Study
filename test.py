"""Scratch exploration script -- NOT part of the ingestion pipeline in src/.

Answers the date_range research question from config.yaml: for each target
category, when does Kalshi have enough settled-market volume for the
calibration study to be meaningful? Run directly and eyeball the monthly
counts printed per category; pick config.yaml's date_range.start where all
categories have stabilized, non-sparse volume.

Only drills into the highest-volume series per category (not all of them --
Politics alone has 2,000+ series, and pulling full settlement history for
every single one takes far longer than this exploration needs).

    python test.py
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# Confirmed against the real /series category values as of this run.
TARGET_CATEGORIES = ["Politics", "Economics", "Sports", "Crypto"]

TOP_N_SERIES_PER_CATEGORY = 20  # a handful of series carry most of the volume
REQUEST_DELAY_SECONDS = 0.2  # polite pacing; these public endpoints need no auth
MAX_MARKETS_PER_SERIES = 500  # cap so one huge recurring series doesn't dominate runtime

# Sub-daily recurring markets (e.g. a Bitcoin price settling every 15 minutes)
# settle so often they dwarf every other series' monthly count and don't
# represent the same kind of "prediction" as a once-off election/game/CPI
# print. `frequency` is a clean server-side enum -- confirmed via a scratch
# query to be one of: custom, one_off, annual, monthly, weekly, daily,
# hourly, fifteen_min. Only the last two are sub-daily.
# NOTE: an earlier version of this filter matched ticker substrings like
# "1h", which false-positived on Sports' "1H" (= First Half) markets --
# a one-off in-game market, not an hourly-recurring one. Match on the
# `frequency` field only.
SUBDAILY_FREQUENCIES = {"hourly", "fifteen_min"}


def is_high_frequency(series: dict) -> bool:
    return series.get("frequency") in SUBDAILY_FREQUENCIES

client = httpx.Client(base_url=BASE_URL, timeout=30.0)


def get(path: str, params: dict | None = None) -> dict:
    """GET with a naive 429 Retry-After backoff. Good enough for a one-off script."""
    resp = client.get(path, params=params)
    if resp.status_code == 429:
        wait = float(resp.headers.get("Retry-After", 5))
        print(f"rate limited, waiting {wait}s")
        time.sleep(wait)
        return get(path, params)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json()


def get_cutoff() -> dict:
    """Live/historical boundary timestamps -- see docs.kalshi.com historical_data."""
    return get("/historical/cutoff")


def top_series_by_volume(category: str, top_n: int = TOP_N_SERIES_PER_CATEGORY) -> tuple[list[dict], list[dict]]:
    """(top_series, excluded_high_frequency_series) for one category, sorted by
    total traded volume, highest first, with sub-daily recurring series removed
    before ranking so they can't crowd out the top-N slots."""
    data = get("/series", params={"category": category, "include_volume": "true"})
    all_series = data.get("series", [])
    excluded = [s for s in all_series if is_high_frequency(s)]
    candidates = [s for s in all_series if not is_high_frequency(s)]
    candidates.sort(key=lambda s: s.get("volume_fp") or 0, reverse=True)
    return candidates[:top_n], excluded


def historical_markets_for_series(series_ticker: str, max_markets: int = MAX_MARKETS_PER_SERIES):
    """Paginate /historical/markets for one series. No date filter exists on this
    endpoint, so we page through and filter/bucket client-side."""
    cursor = None
    fetched = 0
    while fetched < max_markets:
        params = {"series_ticker": series_ticker, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get("/historical/markets", params=params)
        markets = data.get("markets", [])
        if not markets:
            break
        yield from markets
        fetched += len(markets)
        cursor = data.get("cursor")
        if not cursor:
            break


def month_bucket(iso_ts: str) -> str:
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    return f"{dt.year}-{dt.month:02d}"


def main() -> None:
    cutoff = get_cutoff()
    print("Historical cutoff timestamps:", cutoff)

    results: dict = {"cutoff": cutoff, "categories": {}}

    for category in TARGET_CATEGORIES:
        top_series, excluded = top_series_by_volume(category)
        if excluded:
            print(f"\n{category}: excluding {len(excluded)} sub-daily recurring series: "
                  f"{[s['ticker'] for s in excluded]}")
        print(f"{category}: top {len(top_series)} series by volume")
        for s in top_series:
            print(f"  {s['ticker']:<20} volume={s.get('volume_fp', 0)}")

        counts: Counter[str] = Counter()
        for s in top_series:
            for market in historical_markets_for_series(s["ticker"]):
                settlement_ts = market.get("settlement_ts")
                if settlement_ts:
                    counts[month_bucket(settlement_ts)] += 1

        print(f"  settled markets by month ({category}):")
        for month in sorted(counts):
            print(f"    {month}: {counts[month]}")

        results["categories"][category] = {
            "excluded_high_frequency_series": [s["ticker"] for s in excluded],
            "top_series": [{"ticker": s["ticker"], "volume_fp": s.get("volume_fp")} for s in top_series],
            "monthly_settled_counts": dict(sorted(counts.items())),
        }

    out_path = Path(__file__).parent / "date_range_exploration.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
