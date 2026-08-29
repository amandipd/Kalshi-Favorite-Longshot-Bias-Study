"""Tests for src/ingest/summary.py.

The summary is the "did it actually work" dashboard, so what matters is that
it counts a hand-built raw layer correctly and that its anomaly counters fire
on the things that would otherwise pass unnoticed -- duplicates across series,
markets with no parsable settlement, empty pages.

Every test builds a raw layer under `tmp_path`; nothing touches real `data/`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.ingest.summary import summarize_raw


def write_page(root: Path, category: str, series: str, page: int, markets: list[dict]) -> None:
    path = root / "kalshi" / category / series / f"page_{page:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"markets": markets, "cursor": ""}), encoding="utf-8")


def market(ticker: str, settled: str = "2026-03-01T12:00:00Z", **overrides) -> dict:
    base = {
        "ticker": ticker,
        "settlement_ts": settled,
        "result": "yes",
        "volume_fp": "100.00",
    }
    base.update(overrides)
    return base


def test_empty_layer_reports_nothing_rather_than_crashing(tmp_path):
    summary = summarize_raw(tmp_path)

    assert summary.page_files == 0
    assert summary.markets == 0
    assert "empty" in summary.format()


def test_counts_pages_markets_and_categories(tmp_path):
    write_page(tmp_path, "Economics", "KXFED", 1, [market("A"), market("B")])
    write_page(tmp_path, "Economics", "KXFED", 2, [market("C")])
    write_page(tmp_path, "Sports", "KXNBA", 1, [market("D")])

    summary = summarize_raw(tmp_path)

    assert summary.page_files == 3
    assert summary.markets == 4
    assert summary.unique_tickers == 4
    assert summary.categories["Economics"].series == 1
    assert summary.categories["Economics"].pages == 2
    assert summary.categories["Economics"].markets == 3
    assert summary.categories["Sports"].markets == 1
    assert summary.total_volume == 400.0


def test_settlement_span_bounds_every_page(tmp_path):
    write_page(tmp_path, "Economics", "KXFED", 1, [market("A", "2026-01-15T00:00:00Z")])
    write_page(tmp_path, "Economics", "KXFED", 2, [market("B", "2025-11-02T00:00:00Z")])
    write_page(tmp_path, "Sports", "KXNBA", 1, [market("C", "2026-04-30T23:00:00Z")])

    summary = summarize_raw(tmp_path)

    assert summary.settlement_min == date(2025, 11, 2)
    assert summary.settlement_max == date(2026, 4, 30)


def test_duplicate_tickers_are_counted_not_deduplicated_away(tmp_path):
    # The same market surfacing under two series is exactly the kind of thing
    # a silent dedup would hide from the run that needs to know about it.
    write_page(tmp_path, "Economics", "KXFED", 1, [market("SAME")])
    write_page(tmp_path, "Sports", "KXNBA", 1, [market("SAME")])

    summary = summarize_raw(tmp_path)

    assert summary.markets == 2
    assert summary.unique_tickers == 1
    assert summary.duplicate_tickers == 1
    assert "duplicate tickers" in summary.format()


def test_unparsable_and_missing_settlements_are_counted_not_dropped(tmp_path):
    write_page(
        tmp_path,
        "Economics",
        "KXFED",
        1,
        [
            market("GOOD", "2026-03-01T00:00:00Z"),
            market("MISSING", settled=None),
            market("MALFORMED", settled="not-a-timestamp"),
        ],
    )

    summary = summarize_raw(tmp_path)

    assert summary.markets == 3
    assert summary.unparsed_settlements == 2
    # The one good record still bounds the span.
    assert summary.settlement_min == summary.settlement_max == date(2026, 3, 1)


def test_empty_pages_and_result_buckets(tmp_path):
    write_page(tmp_path, "Economics", "KXFED", 1, [])
    write_page(
        tmp_path,
        "Economics",
        "KXFED",
        2,
        [
            market("A", result="yes"),
            market("B", result="no"),
            market("C", result=""),  # voided
        ],
    )

    summary = summarize_raw(tmp_path)

    assert summary.empty_pages == 1
    assert summary.results["yes"] == 1
    assert summary.results["no"] == 1
    assert summary.results[""] == 1
    assert "(empty -- voided?)" in summary.format()


def test_metadata_files_are_not_counted_as_pages(tmp_path):
    write_page(tmp_path, "Economics", "KXFED", 1, [market("A")])
    venue = tmp_path / "kalshi"
    (venue / "_cutoff.json").write_text(
        json.dumps({"market_settled_ts": "2026-06-29T00:00:00Z"}), encoding="utf-8"
    )
    (venue / "Economics" / "_series.json").write_text(
        json.dumps({"selected": []}), encoding="utf-8"
    )

    summary = summarize_raw(tmp_path)

    assert summary.page_files == 1
    assert summary.metadata_files == 2
    assert summary.cutoff["market_settled_ts"] == "2026-06-29T00:00:00Z"
    assert "2026-06-29" in summary.format()


def test_unknown_venue_directory_is_empty_not_an_error(tmp_path):
    write_page(tmp_path, "Economics", "KXFED", 1, [market("A")])

    summary = summarize_raw(tmp_path, venue="polymarket")

    assert summary.venue == "polymarket"
    assert summary.page_files == 0
