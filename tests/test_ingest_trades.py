"""Tests for src/ingest/trades.py.

Same shape as the other ingestion tests: an `httpx.MockTransport` over a
`tmp_path` layout, no network. The handler counts requests, because most of
what matters here is about calls *not* made -- a resumed run must read the
JSONL back rather than re-price a market, and a market with no trade before
its horizon must be recorded so it is never asked about twice.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

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
from src.ingest.trades import (
    KalshiTradesClient,
    _load_done,
    horizon_dirname,
    iter_binary_markets,
)

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


def make_config(tmp_path, horizons=(1.0,)) -> Config:
    return Config(
        date_range=DateRange(start=date(2026, 1, 1), end=date(2026, 6, 6)),
        categories=["Economics"],
        kalshi_base_url="https://example.test",
        polymarket_base_url="https://poly.test",
        rate_limit_per_second=1000.0,
        retry=FAST_RETRY,
        ingest=IngestConfig(
            raw_dir=str(tmp_path / "raw"),
            top_n_series_per_category=2,
            page_limit=2,
            subdaily_frequencies=frozenset(),
            trades=TradesConfig(
                trades_dir=str(tmp_path / "trades"),
                horizons_hours=list(horizons),
            ),
        ),
        analysis=ANALYSIS,
        clean=CleanConfig(
            price_method="horizon_trade",
            # Must match an ingested horizon -- Config validates the pair.
            price_horizon_hours=list(horizons)[0],
            interim_path=str(tmp_path / "interim.parquet"),
            processed_path=str(tmp_path / "processed.parquet"),
        ),
    )


def write_market_page(root: Path, category: str, series: str, markets: list[dict]) -> None:
    path = root / "kalshi" / category / series / "page_0001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"markets": markets, "cursor": ""}), encoding="utf-8")


def market(ticker: str, result: str = "yes", close: str = "2026-03-01T18:00:00Z") -> dict:
    return {"ticker": ticker, "result": result, "close_time": close}


def trade(price: str = "0.4200", created: str = "2026-03-01T16:00:00Z") -> dict:
    return {
        "ticker": "T",
        "yes_price_dollars": price,
        "no_price_dollars": "0.5800",
        "count_fp": "10.00",
        "created_time": created,
        "trade_id": "abc",
    }


def make_client(tmp_path, handler, horizons=(1.0,)) -> KalshiTradesClient:
    cfg = make_config(tmp_path, horizons)
    return KalshiTradesClient(cfg, transport=httpx.MockTransport(handler))


# -- horizon naming --------------------------------------------------------


@pytest.mark.parametrize(
    "hours,expected",
    [(1.0, "T1h"), (6, "T6h"), (24.0, "T24h"), (0.5, "T0.5h")],
)
def test_horizon_dirname_is_stable(hours, expected):
    # The directory name IS the resume key, so it must not drift between an
    # int and a float spelling of the same horizon.
    assert horizon_dirname(hours) == expected


# -- enumerating the markets to price --------------------------------------


def test_iter_binary_markets_skips_scalar_and_deduplicates(tmp_path):
    raw = tmp_path / "raw"
    write_market_page(
        raw,
        "Economics",
        "KXFED",
        [market("A"), market("B", result="no"), market("S", result="scalar")],
    )
    # Same ticker surfacing under a second series must be priced once.
    write_market_page(raw, "Sports", "KXNBA", [market("A"), market("C")])

    tickers = [m["ticker"] for m in iter_binary_markets(raw)]

    assert sorted(tickers) == ["A", "B", "C"]


def test_iter_binary_markets_skips_markets_without_a_close_time(tmp_path):
    raw = tmp_path / "raw"
    write_market_page(raw, "Economics", "KXFED", [market("A"), market("B", close=None)])

    assert [m["ticker"] for m in iter_binary_markets(raw)] == ["A"]


# -- the cutoff --------------------------------------------------------------


def test_cutoff_is_close_time_minus_the_horizon(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["max_ts"] = int(request.url.params["max_ts"])
        seen["ticker"] = request.url.params["ticker"]
        return httpx.Response(200, json={"trades": [trade()]})

    write_market_page(tmp_path / "raw", "Economics", "KXFED", [market("A")])
    with make_client(tmp_path, handler, horizons=(6.0,)) as client:
        client.fetch_horizon(6.0)

    # close 2026-03-01T18:00Z minus 6h == 12:00Z
    assert datetime.fromtimestamp(seen["max_ts"], tz=timezone.utc) == datetime(
        2026, 3, 1, 12, 0, tzinfo=timezone.utc
    )
    assert seen["ticker"] == "A"


# -- writing ---------------------------------------------------------------


def test_writes_one_line_per_market_with_the_trade_verbatim(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"trades": [trade(price="0.3300")]})

    write_market_page(tmp_path / "raw", "Economics", "KXFED", [market("A"), market("B")])
    with make_client(tmp_path, handler) as client:
        stats = client.fetch_horizon(1.0)

    out = tmp_path / "trades" / "kalshi" / "T1h" / "Economics" / "KXFED.jsonl"
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]

    assert stats.fetched == 2
    assert [x["ticker"] for x in lines] == ["A", "B"]
    assert lines[0]["trade"]["yes_price_dollars"] == "0.3300"
    assert lines[0]["horizon_hours"] == 1.0


def test_no_trade_is_recorded_as_an_explicit_null(tmp_path):
    # A market with no trade before its horizon is an exclusion to count, not
    # an absence to infer -- and recording it stops a re-run asking again.
    def handler(request):
        return httpx.Response(200, json={"trades": []})

    write_market_page(tmp_path / "raw", "Economics", "KXFED", [market("A")])
    with make_client(tmp_path, handler) as client:
        stats = client.fetch_horizon(1.0)

    out = tmp_path / "trades" / "kalshi" / "T1h" / "Economics" / "KXFED.jsonl"
    line = json.loads(out.read_text(encoding="utf-8").strip())

    assert line["trade"] is None
    assert stats.no_trade == 1
    assert stats.no_trade_per_category == {"Economics": 1}


def test_categories_and_series_land_in_separate_files(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"trades": [trade()]})

    raw = tmp_path / "raw"
    write_market_page(raw, "Economics", "KXFED", [market("A")])
    write_market_page(raw, "Sports", "KXNBA", [market("B")])
    with make_client(tmp_path, handler) as client:
        client.fetch_horizon(1.0)

    root = tmp_path / "trades" / "kalshi" / "T1h"
    assert (root / "Economics" / "KXFED.jsonl").exists()
    assert (root / "Sports" / "KXNBA.jsonl").exists()


def test_horizons_are_isolated_on_disk(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"trades": [trade()]})

    write_market_page(tmp_path / "raw", "Economics", "KXFED", [market("A")])
    with make_client(tmp_path, handler, horizons=(1.0, 6.0)) as client:
        client.fetch_all_horizons()

    root = tmp_path / "trades" / "kalshi"
    assert (root / "T1h" / "Economics" / "KXFED.jsonl").exists()
    assert (root / "T6h" / "Economics" / "KXFED.jsonl").exists()


# -- resumption ------------------------------------------------------------


def test_rerun_fetches_nothing(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"trades": [trade()]})

    write_market_page(tmp_path / "raw", "Economics", "KXFED", [market("A"), market("B")])
    with make_client(tmp_path, handler) as client:
        first = client.fetch_horizon(1.0)
    assert first.fetched == 2 and calls["n"] == 2

    with make_client(tmp_path, handler) as client:
        second = client.fetch_horizon(1.0)

    assert second.fetched == 0
    assert second.skipped_cached == 2
    assert calls["n"] == 2, "a re-run must not hit the API"


def test_resume_prices_only_the_markets_still_missing(tmp_path):
    asked = []

    def handler(request):
        asked.append(request.url.params["ticker"])
        return httpx.Response(200, json={"trades": [trade()]})

    write_market_page(
        tmp_path / "raw", "Economics", "KXFED", [market("A"), market("B"), market("C")]
    )
    # Simulate a run that died after pricing A.
    out = tmp_path / "trades" / "kalshi" / "T1h" / "Economics" / "KXFED.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"ticker": "A", "trade": trade()}) + "\n", encoding="utf-8")

    with make_client(tmp_path, handler) as client:
        stats = client.fetch_horizon(1.0)

    assert asked == ["B", "C"]
    assert stats.skipped_cached == 1


def test_a_market_recorded_as_no_trade_is_not_asked_again(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"trades": []})

    write_market_page(tmp_path / "raw", "Economics", "KXFED", [market("A")])
    with make_client(tmp_path, handler) as client:
        client.fetch_horizon(1.0)
    with make_client(tmp_path, handler) as client:
        client.fetch_horizon(1.0)

    assert calls["n"] == 1


def test_truncated_final_line_is_tolerated_and_refetched(tmp_path):
    # A run killed mid-append leaves a partial line. That one market should be
    # re-priced; the rest of the file must still count as done.
    path = tmp_path / "x.jsonl"
    path.write_text(
        json.dumps({"ticker": "A", "trade": None}) + "\n" + '{"ticker": "B", "tra',
        encoding="utf-8",
    )

    assert _load_done(path) == {"A"}


# -- failure handling ------------------------------------------------------


def test_one_failing_market_does_not_end_the_run(tmp_path):
    def handler(request):
        if request.url.params["ticker"] == "B":
            return httpx.Response(404, json={"error": "gone"})
        return httpx.Response(200, json={"trades": [trade()]})

    write_market_page(
        tmp_path / "raw", "Economics", "KXFED", [market("A"), market("B"), market("C")]
    )
    with make_client(tmp_path, handler) as client:
        stats = client.fetch_horizon(1.0)

    assert stats.fetched == 2
    assert stats.errors == 1

    out = tmp_path / "trades" / "kalshi" / "T1h" / "Economics" / "KXFED.jsonl"
    tickers = [json.loads(x)["ticker"] for x in out.read_text(encoding="utf-8").splitlines()]
    assert tickers == ["A", "C"]


def test_failed_market_is_retried_on_the_next_run(tmp_path):
    state = {"fail": True}

    def handler(request):
        if request.url.params["ticker"] == "B" and state["fail"]:
            return httpx.Response(404, json={"error": "gone"})
        return httpx.Response(200, json={"trades": [trade()]})

    write_market_page(tmp_path / "raw", "Economics", "KXFED", [market("A"), market("B")])
    with make_client(tmp_path, handler) as client:
        client.fetch_horizon(1.0)

    state["fail"] = False
    with make_client(tmp_path, handler) as client:
        stats = client.fetch_horizon(1.0)

    assert stats.fetched == 1
    assert stats.skipped_cached == 1
