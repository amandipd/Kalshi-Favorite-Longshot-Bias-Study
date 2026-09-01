"""Tests for src/ingest/kalshi.py.

Like the base-client tests, everything runs through an `httpx.MockTransport`
against a `tmp_path` raw directory: no network, no real `data/` writes. Each
handler counts the requests it serves, because most of what is worth asserting
here is about calls *not* made -- a resumed run must read from disk instead of
re-fetching.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from src.config import (
    AnalysisConfig,
    CleanConfig,
    Config,
    DateRange,
    IngestConfig,
    RetryConfig,
    TradesConfig,
)
from src.ingest.kalshi import KalshiClient, _volume

FAST_RETRY = RetryConfig(
    max_attempts=3,
    initial_backoff_seconds=0.01,
    max_backoff_seconds=0.02,
    jitter_seconds=0.0,
    timeout_seconds=5.0,
)


# The analysis layer is not what these tests exercise, but Config validates as a
# whole, so every fixture needs one. Few bootstrap reps: any test that reaches
# the bootstrap here cares that it ran, not how tight its interval is.
ANALYSIS = AnalysisConfig(
    n_buckets=10,
    confidence=0.95,
    cluster_on="event_ticker",
    bootstrap_reps=100,
    bootstrap_seed=1,
    fdr_alpha=0.05,
    segment_n_buckets=5,
    min_events_per_bucket=30,
)


def make_config(tmp_path, top_n: int = 2, page_limit: int = 2) -> Config:
    return Config(
        date_range=DateRange(start=date(2026, 1, 1), end=date(2026, 6, 6)),
        categories=["Economics"],
        kalshi_base_url="https://example.test",
        polymarket_base_url="https://poly.test",
        rate_limit_per_second=1000.0,
        retry=FAST_RETRY,
        ingest=IngestConfig(
            raw_dir=str(tmp_path),
            top_n_series_per_category=top_n,
            page_limit=page_limit,
            subdaily_frequencies=frozenset({"hourly", "fifteen_min"}),
            trades=TradesConfig(
                trades_dir=str(tmp_path / "trades"),
                horizons_hours=[1.0],
            ),
        ),
        analysis=ANALYSIS,
        clean=CleanConfig(
            price_method="horizon_trade",
            price_horizon_hours=1.0,
            interim_path=str(tmp_path / "interim.parquet"),
            processed_path=str(tmp_path / "processed.parquet"),
        ),
    )


def market(ticker: str, settled: str) -> dict:
    """A minimal historical-market record, shaped like the real one."""
    return {
        "ticker": ticker,
        "settlement_ts": f"{settled}T12:00:00Z",
        "result": "yes",
        "market_type": "binary",
    }


class FakeKalshi:
    """Serves the three endpoints a run touches, counting every request.

    `series` maps a ticker to its list of pages; each page is a list of
    markets. The cursor is just the next page index, which is enough to
    exercise the client's cursor chain.
    """

    def __init__(self, series_catalog: list[dict], pages_by_series: dict[str, list[list[dict]]]):
        self.series_catalog = series_catalog
        self.pages_by_series = pages_by_series
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)

        if path == "/historical/cutoff":
            return httpx.Response(200, json={"market_settled_ts": "2026-06-06T00:00:00Z"})

        if path == "/series":
            return httpx.Response(200, json={"series": self.series_catalog})

        if path == "/historical/markets":
            ticker = request.url.params["series_ticker"]
            cursor = request.url.params.get("cursor")
            index = 0 if cursor is None else int(cursor)
            pages = self.pages_by_series[ticker]
            markets = pages[index]
            next_cursor = str(index + 1) if index + 1 < len(pages) else ""
            return httpx.Response(200, json={"markets": markets, "cursor": next_cursor})

        return httpx.Response(404)

    def market_requests(self) -> int:
        return self.requests.count("/historical/markets")


def make_client(fake: FakeKalshi, config: Config) -> KalshiClient:
    return KalshiClient(config, transport=httpx.MockTransport(fake.handler))


# -- series selection ------------------------------------------------------


def test_volume_is_compared_numerically_not_lexicographically():
    """Regression: volume_fp is a decimal string, so a raw sort ranks
    "9917.00" above "10000000.00". Selecting the top series by volume must
    cast first, or the sample is "series whose volume starts with a 9"."""
    big = {"ticker": "BIG", "volume_fp": "10000000.00"}
    small = {"ticker": "SMALL", "volume_fp": "9917.00"}

    assert _volume(big) > _volume(small)
    assert sorted([small, big], key=_volume, reverse=True)[0]["ticker"] == "BIG"


def test_volume_handles_missing_and_malformed_values():
    assert _volume({"ticker": "X"}) == 0.0
    assert _volume({"ticker": "X", "volume_fp": "not-a-number"}) == 0.0


def test_select_series_ranks_by_volume_and_drops_subdaily(tmp_path):
    """Sub-daily series are removed before ranking, so they cannot occupy a
    top-N slot no matter how much volume they carry."""
    catalog = [
        {"ticker": "HUGE_HOURLY", "volume_fp": "99999999.00", "frequency": "hourly"},
        {"ticker": "BIG", "volume_fp": "10000000.00", "frequency": "weekly"},
        {"ticker": "MID", "volume_fp": "500000.00", "frequency": "one_off"},
        {"ticker": "SMALL", "volume_fp": "9917.00", "frequency": "daily"},
    ]
    fake = FakeKalshi(catalog, {})

    with make_client(fake, make_config(tmp_path, top_n=2)) as client:
        selected = client.select_series("Economics")

    assert [s["ticker"] for s in selected] == ["BIG", "MID"]


def test_select_series_records_exclusions_for_audit(tmp_path):
    """The cache file explains the selection, not just its result."""
    catalog = [
        {"ticker": "KEEP", "volume_fp": "100.00", "frequency": "weekly"},
        {"ticker": "DROP", "volume_fp": "999.00", "frequency": "fifteen_min"},
    ]
    fake = FakeKalshi(catalog, {})

    with make_client(fake, make_config(tmp_path)) as client:
        client.select_series("Economics")

    saved = json.loads((tmp_path / "kalshi" / "Economics" / "_series.json").read_text())
    assert saved["total_series_in_category"] == 2
    assert saved["excluded_subdaily"] == [{"ticker": "DROP", "frequency": "fifteen_min"}]
    assert [s["ticker"] for s in saved["selected"]] == ["KEEP"]


def test_select_series_is_cached_across_runs(tmp_path):
    """Re-ranking mid-study would silently change the sample as volumes shift,
    so a second run reuses the recorded selection and re-queries nothing."""
    catalog = [{"ticker": "A", "volume_fp": "100.00", "frequency": "weekly"}]
    fake = FakeKalshi(catalog, {})
    config = make_config(tmp_path)

    with make_client(fake, config) as client:
        first = client.select_series("Economics")
    with make_client(fake, config) as client:
        second = client.select_series("Economics")

    assert first == second
    assert fake.requests.count("/series") == 1


# -- the page walk ---------------------------------------------------------


SERIES_CATALOG = [{"ticker": "KXCPI", "volume_fp": "100.00", "frequency": "monthly"}]


def three_pages() -> dict[str, list[list[dict]]]:
    """Three pages, descending by settlement, all inside the config window."""
    return {
        "KXCPI": [
            [market("KXCPI-1", "2026-05-01"), market("KXCPI-2", "2026-04-01")],
            [market("KXCPI-3", "2026-03-01"), market("KXCPI-4", "2026-02-01")],
            [market("KXCPI-5", "2026-01-15")],
        ]
    }


def test_pages_land_at_deterministic_paths(tmp_path):
    """The filename is the whole idempotency mechanism, so it must be stable
    and derived only from (category, series, page number)."""
    fake = FakeKalshi(SERIES_CATALOG, three_pages())

    with make_client(fake, make_config(tmp_path)) as client:
        client.fetch_settled_markets()

    series_dir = tmp_path / "kalshi" / "Economics" / "KXCPI"
    assert sorted(p.name for p in series_dir.glob("page_*.json")) == [
        "page_0001.json",
        "page_0002.json",
        "page_0003.json",
    ]


def test_pages_are_written_verbatim(tmp_path):
    """Raw means raw: the envelope and cursor survive, nothing is filtered."""
    fake = FakeKalshi(SERIES_CATALOG, three_pages())

    with make_client(fake, make_config(tmp_path)) as client:
        client.fetch_settled_markets()

    page = json.loads(
        (tmp_path / "kalshi" / "Economics" / "KXCPI" / "page_0001.json").read_text()
    )
    assert page["cursor"] == "1"
    assert [m["ticker"] for m in page["markets"]] == ["KXCPI-1", "KXCPI-2"]


def test_cutoff_is_recorded(tmp_path):
    """The live/historical boundary moves, so each run records the one it saw."""
    fake = FakeKalshi(SERIES_CATALOG, three_pages())

    with make_client(fake, make_config(tmp_path)) as client:
        client.fetch_settled_markets()

    saved = json.loads((tmp_path / "kalshi" / "_cutoff.json").read_text())
    assert saved["market_settled_ts"] == "2026-06-06T00:00:00Z"


def test_no_temp_files_survive(tmp_path):
    """Pages are written atomically; a completed run leaves no .tmp behind."""
    fake = FakeKalshi(SERIES_CATALOG, three_pages())

    with make_client(fake, make_config(tmp_path)) as client:
        client.fetch_settled_markets()

    assert list(tmp_path.rglob("*.tmp")) == []


# -- idempotency -----------------------------------------------------------


def test_rerun_fetches_nothing_and_skips_every_page(tmp_path):
    """The headline requirement: re-running completed work costs no requests."""
    fake = FakeKalshi(SERIES_CATALOG, three_pages())
    config = make_config(tmp_path)

    with make_client(fake, config) as client:
        first = client.fetch_settled_markets()
    requests_after_first = fake.market_requests()

    with make_client(fake, config) as client:
        second = client.fetch_settled_markets()

    assert first.pages_fetched == 3 and first.pages_skipped == 0
    assert second.pages_fetched == 0 and second.pages_skipped == 3
    assert fake.market_requests() == requests_after_first  # no new market calls
    # The same markets are still counted, so downstream sees an identical run.
    assert second.markets_seen == first.markets_seen


def test_interrupted_run_resumes_at_the_first_missing_page(tmp_path):
    """A partial run leaves pages 1-2 on disk; the resume re-reads those to
    recover the cursor chain and fetches only page 3."""
    fake = FakeKalshi(SERIES_CATALOG, three_pages())
    config = make_config(tmp_path)

    # Simulate the interruption by deleting the last page of a complete run.
    with make_client(fake, config) as client:
        client.fetch_settled_markets()
    (tmp_path / "kalshi" / "Economics" / "KXCPI" / "page_0003.json").unlink()

    before = fake.market_requests()
    with make_client(fake, config) as client:
        stats = client.fetch_settled_markets()

    assert stats.pages_skipped == 2
    assert stats.pages_fetched == 1
    assert fake.market_requests() == before + 1


def test_resume_requests_the_correct_cursor(tmp_path):
    """Recovering the cursor from disk is the subtle part -- a resumed run must
    ask for the page it is missing, not restart the chain at page 1."""
    fake = FakeKalshi(SERIES_CATALOG, three_pages())
    config = make_config(tmp_path)

    with make_client(fake, config) as client:
        client.fetch_settled_markets()
    (tmp_path / "kalshi" / "Economics" / "KXCPI" / "page_0003.json").unlink()

    cursors: list[str | None] = []
    original = fake.handler

    def recording_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/historical/markets":
            cursors.append(request.url.params.get("cursor"))
        return original(request)

    client = KalshiClient(config, transport=httpx.MockTransport(recording_handler))
    with client:
        client.fetch_settled_markets()

    # Page 3's cursor is "2", carried in page 2's body on disk.
    assert cursors == ["2"]


# -- window handling -------------------------------------------------------


def test_walk_stops_once_a_page_predates_the_window(tmp_path):
    """/historical/markets is descending by settlement, so the first page that
    runs past start_date means every later page is older still. Without this a
    six-month window would drag a series' entire history."""
    pages = {
        "KXCPI": [
            [market("in-1", "2026-05-01")],
            [market("in-2", "2026-02-01")],
            [market("old-1", "2025-11-01")],  # crosses start_date=2026-01-01
            [market("old-2", "2025-10-01")],  # must never be requested
            [market("old-3", "2025-09-01")],
        ]
    }
    fake = FakeKalshi(SERIES_CATALOG, pages)

    with make_client(fake, make_config(tmp_path)) as client:
        client.fetch_settled_markets()

    assert fake.market_requests() == 3
    series_dir = tmp_path / "kalshi" / "Economics" / "KXCPI"
    assert len(list(series_dir.glob("page_*.json"))) == 3


def test_page_that_crosses_the_boundary_is_kept_whole(tmp_path):
    """Trimming the straddling page to the window would break the raw layer's
    verbatim guarantee -- and the window is meant to be changeable in
    src/clean.py without re-fetching."""
    pages = {"KXCPI": [[market("in", "2026-02-01"), market("out", "2025-12-01")]]}
    fake = FakeKalshi(SERIES_CATALOG, pages)

    with make_client(fake, make_config(tmp_path)) as client:
        client.fetch_settled_markets()

    page = json.loads(
        (tmp_path / "kalshi" / "Economics" / "KXCPI" / "page_0001.json").read_text()
    )
    assert [m["ticker"] for m in page["markets"]] == ["in", "out"]


def test_walk_stops_when_series_is_exhausted(tmp_path):
    """An empty cursor ends the series even though the window is not reached."""
    pages = {"KXCPI": [[market("only", "2026-05-01")]]}
    fake = FakeKalshi(SERIES_CATALOG, pages)

    with make_client(fake, make_config(tmp_path)) as client:
        stats = client.fetch_settled_markets()

    assert fake.market_requests() == 1
    assert stats.markets_seen == 1


def test_empty_series_does_not_loop(tmp_path):
    """A series with no historical markets terminates instead of spinning."""
    pages = {"KXCPI": [[]]}
    fake = FakeKalshi(SERIES_CATALOG, pages)

    with make_client(fake, make_config(tmp_path)) as client:
        stats = client.fetch_settled_markets()

    assert stats.markets_seen == 0
    assert fake.market_requests() == 1


def test_malformed_settlement_timestamp_does_not_abort_the_run(tmp_path):
    """One bad record must not kill a long ingestion."""
    pages = {
        "KXCPI": [
            [{"ticker": "bad", "settlement_ts": "not-a-timestamp"}],
            [market("good", "2026-05-01")],
        ]
    }
    fake = FakeKalshi(SERIES_CATALOG, pages)

    with make_client(fake, make_config(tmp_path)) as client:
        stats = client.fetch_settled_markets()

    assert stats.markets_seen == 2


# -- run accounting --------------------------------------------------------


def test_stats_report_markets_per_category(tmp_path):
    """The summary is the health check the milestone asks for."""
    fake = FakeKalshi(SERIES_CATALOG, three_pages())

    with make_client(fake, make_config(tmp_path)) as client:
        stats = client.fetch_settled_markets()

    assert stats.markets_per_category == {"Economics": 5}
    assert stats.series_walked == 1
    assert "markets=5" in stats.summary()


def test_categories_are_isolated_on_disk(tmp_path):
    """Two categories selecting the same series must not share a directory, or
    one would be silently treated as already ingested."""
    catalog = [{"ticker": "SHARED", "volume_fp": "100.00", "frequency": "weekly"}]
    pages = {"SHARED": [[market("m", "2026-05-01")]]}
    fake = FakeKalshi(catalog, pages)
    config = make_config(tmp_path)

    with make_client(fake, config) as client:
        client.fetch_settled_markets(categories=["Economics", "Sports"])

    assert (tmp_path / "kalshi" / "Economics" / "SHARED" / "page_0001.json").exists()
    assert (tmp_path / "kalshi" / "Sports" / "SHARED" / "page_0001.json").exists()
    assert fake.market_requests() == 2
