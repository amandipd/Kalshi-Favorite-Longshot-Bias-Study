"""Tests for src/ingest/trades_progress.py.

The dashboard's only real claim is that its percentage means something: "100%"
has to mean the pass would now fetch nothing. So the tests that matter are the
ones pinning its numerator and denominator to the pass's own -- everything
else is arithmetic and formatting.

Every test builds a raw layer and a trades layer under `tmp_path`; nothing
touches real `data/`.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.ingest.trades import _load_done, horizon_dirname
from src.ingest.trades_progress import (
    RateTracker,
    _bar,
    _format_duration,
    expected_by_series,
    measure,
    measure_horizon,
    scan_horizon_file,
)


def write_page(root: Path, category: str, series: str, page: int, markets: list[dict]) -> None:
    path = root / "kalshi" / category / series / f"page_{page:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"markets": markets, "cursor": ""}), encoding="utf-8")


def market(ticker: str, result: str = "yes", close: str = "2026-03-01T12:00:00Z") -> dict:
    return {"ticker": ticker, "result": result, "close_time": close}


def write_jsonl(root: Path, horizon: float, category: str, series: str, records: list[dict]) -> Path:
    path = root / "kalshi" / horizon_dirname(horizon) / category / f"{series}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


def record(ticker: str, trade: dict | None = None) -> dict:
    return {"ticker": ticker, "trade": trade or {"yes_price": "0.42"}}


# -- denominator ----------------------------------------------------------


def test_expected_counts_only_binary_markets_the_pass_would_fetch(tmp_path):
    """Scalar and result-less markets are never fetched, so they are not owed."""
    write_page(
        tmp_path,
        "Sports",
        "KXNBA",
        1,
        [market("A"), market("B", result="no"), market("C", result="scalar")],
    )

    counts = expected_by_series(tmp_path)

    assert counts[("Sports", "KXNBA")] == 2


def test_expected_counts_a_repeated_ticker_once(tmp_path):
    """The pass deduplicates by ticker, so the denominator must too -- otherwise
    a market returned by two pages makes 100% unreachable."""
    write_page(tmp_path, "Sports", "KXNBA", 1, [market("A"), market("B")])
    write_page(tmp_path, "Sports", "KXNBA", 2, [market("B"), market("C")])

    assert expected_by_series(tmp_path)[("Sports", "KXNBA")] == 3


def test_missing_raw_layer_is_empty_not_an_error(tmp_path):
    assert expected_by_series(tmp_path / "nope") == {}


# -- numerator ------------------------------------------------------------


def test_scan_agrees_with_the_resume_check(tmp_path):
    """The load-bearing test: done-ness here must equal skip-ness there.

    Includes both cases where a naive line count would disagree -- a duplicate
    ticker and a truncated final line from a killed run.
    """
    path = write_jsonl(tmp_path, 1, "Sports", "KXNBA", [record("A"), record("B"), record("A")])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ticker": "C", "tra')  # killed mid-append

    tickers, _ = scan_horizon_file(path)

    assert tickers == _load_done(path)
    assert tickers == {"A", "B"}


def test_scan_counts_explicit_nulls_as_no_trade(tmp_path):
    """A null is a fetched market with no trade before the cutoff -- done, but
    excluded. It must count toward progress and toward the exclusion rate."""
    path = write_jsonl(
        tmp_path, 1, "Sports", "KXNBA", [record("A"), {"ticker": "B", "trade": None}]
    )

    tickers, no_trade = scan_horizon_file(path)

    assert len(tickers) == 2
    assert no_trade == 1


def test_scan_of_a_missing_file_is_empty(tmp_path):
    tickers, no_trade = scan_horizon_file(tmp_path / "absent.jsonl")

    assert tickers == set()
    assert no_trade == 0


# -- measurement ----------------------------------------------------------


def test_partial_series_reports_its_share_and_shows_as_in_flight(tmp_path):
    raw, trades = tmp_path / "raw", tmp_path / "trades"
    write_page(raw, "Sports", "KXNBA", 1, [market(t) for t in "ABCD"])
    write_jsonl(trades, 1, "Sports", "KXNBA", [record("A"), record("B")])

    horizon = measure_horizon(1, expected_by_series(raw), trades)

    assert (horizon.done, horizon.expected, horizon.remaining) == (2, 4, 2)
    assert horizon.fraction == 0.5
    assert not horizon.complete
    assert [(s.category, s.series) for s in horizon.in_flight()] == [("Sports", "KXNBA")]


def test_a_fully_fetched_horizon_reads_as_complete(tmp_path):
    raw, trades = tmp_path / "raw", tmp_path / "trades"
    write_page(raw, "Sports", "KXNBA", 1, [market("A"), market("B")])
    write_jsonl(trades, 1, "Sports", "KXNBA", [record("A"), record("B")])

    horizon = measure_horizon(1, expected_by_series(raw), trades)

    assert horizon.complete
    assert horizon.fraction == 1.0
    assert horizon.in_flight() == []
    assert horizon.incomplete_series() == []


def test_an_untouched_series_is_zero_rather_than_absent(tmp_path):
    """A series with no file yet is 0/N of the work, not missing from the total
    -- otherwise a pull that has not reached it looks further along than it is."""
    raw, trades = tmp_path / "raw", tmp_path / "trades"
    write_page(raw, "Sports", "KXNBA", 1, [market("A")])
    write_page(raw, "Sports", "KXNHL", 1, [market("B"), market("C")])
    write_jsonl(trades, 1, "Sports", "KXNBA", [record("A")])

    horizon = measure_horizon(1, expected_by_series(raw), trades)

    assert (horizon.done, horizon.expected) == (1, 3)
    assert [s.series for s in horizon.incomplete_series()] == ["KXNHL"]
    assert horizon.in_flight() == []  # untouched is not in flight


def test_extra_tickers_are_clamped_rather_than_exceeding_100_percent(tmp_path):
    """A trades file can hold tickers the current raw layer no longer yields."""
    raw, trades = tmp_path / "raw", tmp_path / "trades"
    write_page(raw, "Sports", "KXNBA", 1, [market("A")])
    write_jsonl(trades, 1, "Sports", "KXNBA", [record("A"), record("STALE")])

    horizon = measure_horizon(1, expected_by_series(raw), trades)

    assert horizon.done == 1
    assert horizon.fraction == 1.0


def test_categories_roll_up_with_their_own_exclusion_rates(tmp_path):
    """no_trade falling unevenly across categories is a bias, not a smaller
    sample (ADR 003), so the dashboard reports it per category."""
    raw, trades = tmp_path / "raw", tmp_path / "trades"
    write_page(raw, "Sports", "KXNBA", 1, [market("A"), market("B")])
    write_page(raw, "Crypto", "KXBTC", 1, [market("C"), market("D")])
    write_jsonl(trades, 1, "Sports", "KXNBA", [record("A"), record("B")])
    write_jsonl(
        trades,
        1,
        "Crypto",
        "KXBTC",
        [{"ticker": "C", "trade": None}, {"ticker": "D", "trade": None}],
    )

    categories = measure_horizon(1, expected_by_series(raw), trades).by_category()

    assert categories["Sports"].no_trade == 0
    assert categories["Crypto"].no_trade == 2
    assert categories["Crypto"].fraction == 1.0  # fetched, but all excluded


def test_unstarted_horizon_counts_toward_the_total(tmp_path):
    """Every configured horizon is work owed, so T6h at 0% must drag the
    overall number down rather than be quietly omitted."""
    raw, trades = tmp_path / "raw", tmp_path / "trades"
    write_page(raw, "Sports", "KXNBA", 1, [market("A"), market("B")])
    write_jsonl(trades, 1, "Sports", "KXNBA", [record("A"), record("B")])

    progress = measure(raw, trades, [1, 6])

    assert (progress.done, progress.expected) == (2, 4)
    assert progress.fraction == 0.5
    assert progress.horizons[1].done == 0


def test_report_renders_the_headline_numbers(tmp_path):
    raw, trades = tmp_path / "raw", tmp_path / "trades"
    write_page(raw, "Sports", "KXNBA", 1, [market("A"), market("B"), market("C"), market("D")])
    write_jsonl(trades, 1, "Sports", "KXNBA", [record("A")])

    text = measure(raw, trades, [1]).format()

    assert "TRADES PULL PROGRESS" in text
    assert "T1h" in text
    assert "25.0%" in text
    assert "in flight: Sports/KXNBA" in text


def test_eta_is_shown_for_the_running_horizon_not_the_queued_ones(tmp_path):
    """Horizons run in turn, so an ETA on an unstarted one would be answering a
    question nobody asked -- and would read as the current run's finish time."""
    raw, trades = tmp_path / "raw", tmp_path / "trades"
    write_page(raw, "Sports", "KXNBA", 1, [market(t) for t in "ABCD"])
    write_jsonl(trades, 1, "Sports", "KXNBA", [record("A"), record("B")])

    text = measure(raw, trades, [1, 6]).format(rate=2.0, horizon_rates={"T1h": 2.0, "T6h": 0.0})

    assert text.count("markets/sec") == 2  # the overall line, plus T1h only
    assert "ETA 1s for 2 remaining" in text


def test_no_eta_on_a_finished_horizon(tmp_path):
    raw, trades = tmp_path / "raw", tmp_path / "trades"
    write_page(raw, "Sports", "KXNBA", 1, [market("A")])
    write_jsonl(trades, 1, "Sports", "KXNBA", [record("A")])

    text = measure(raw, trades, [1]).format(rate=5.0, horizon_rates={"T1h": 5.0})

    assert text.count("markets/sec") == 1  # overall only; T1h has nothing left


def test_report_on_an_empty_raw_layer_says_what_to_run(tmp_path):
    text = measure(tmp_path / "raw", tmp_path / "trades", [1]).format()

    assert "make ingest" in text


# -- rate and formatting --------------------------------------------------


def test_rate_needs_two_samples_then_reports_markets_per_second(tmp_path):
    tracker = RateTracker()

    assert tracker.update(100, now=0.0) is None
    assert tracker.update(1300, now=100.0) == 12.0


def test_rate_withholds_an_eta_until_the_sample_is_long_enough(tmp_path):
    """Throughput is lumpy over short spans -- the pass flushes on a counter,
    and API latency wanders. A 20s sample yields a confidently wrong ETA."""
    tracker = RateTracker(min_elapsed_seconds=60.0)
    tracker.update(0, now=0.0)

    assert tracker.update(400, now=20.0) is None
    assert tracker.update(1200, now=100.0) == 12.0


def test_rate_reports_zero_when_a_pull_has_stalled(tmp_path):
    tracker = RateTracker()
    tracker.update(500, now=0.0)

    assert tracker.update(500, now=90.0) == 0.0


def test_rate_window_forgets_the_speed_a_pull_used_to_have(tmp_path):
    """After a stall, the trailing window must report the stall, not the
    healthy average from before it."""
    tracker = RateTracker(window_seconds=120.0, min_elapsed_seconds=60.0)
    tracker.update(0, now=0.0)
    tracker.update(1200, now=100.0)  # fast, but ages out of the window below

    assert tracker.update(1200, now=230.0) == 0.0


def test_bar_is_not_full_until_the_work_is(tmp_path):
    """A bar that reads full at 99.9% is a bar that lies about being done."""
    assert _bar(0.999).count("]") == 1
    assert _bar(1.0) != _bar(0.999)
    assert set(_bar(0.0)) <= {"[", "]", "-", "░"}


def test_duration_formats_by_magnitude(tmp_path):
    assert _format_duration(45) == "45s"
    assert _format_duration(150) == "2m30s"
    assert _format_duration(5400) == "1h30m"
