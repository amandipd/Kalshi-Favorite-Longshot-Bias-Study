"""Tests for src/clean.py.

Synthetic raw fixtures under `tmp_path` -- a voided market, a duplicate, a
scalar settlement, a market with no trade before its horizon -- asserting that
each is parsed or rejected for the *named* reason. The reason matters as much
as the count: docs/cleaning-log.md reports drops by reason, and a row that
disappears into the wrong bucket is a row nobody can argue about.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.clean import (
    DropLog,
    compute_implied_price,
    interim_to_processed,
    load_horizon_trades,
    parse_raw_to_interim,
)
from src.config import (
    AnalysisConfig,
    CleanConfig,
    Config,
    DateRange,
    IngestConfig,
    RetryConfig,
    StrategyConfig,
    TradesConfig,
)

RETRY = RetryConfig(
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

STRATEGY = StrategyConfig(
    train_fraction=0.6,
    fee_coefficient=0.07,
    fee_ceiling_per_contract=False,
    min_net_edge=0.0,
    kelly_fraction=0.5,
    max_position_fraction=0.02,
    max_daily_deployment=1.0,
    slippage_per_contract=0.0,
)



def make_config(tmp_path, method="horizon_trade", horizon=1.0) -> Config:
    return Config(
        date_range=DateRange(start=date(2026, 1, 1), end=date(2026, 6, 6)),
        categories=["Economics"],
        kalshi_base_url="https://example.test",
        polymarket_base_url="https://poly.test",
        rate_limit_per_second=1000.0,
        retry=RETRY,
        ingest=IngestConfig(
            raw_dir=str(tmp_path / "raw"),
            top_n_series_per_category=2,
            page_limit=2,
            subdaily_frequencies=frozenset(),
            trades=TradesConfig(trades_dir=str(tmp_path / "trades"), horizons_hours=[horizon]),
        ),
        analysis=ANALYSIS,
        strategy=STRATEGY,
        clean=CleanConfig(
            price_method=method,
            price_horizon_hours=horizon,
            interim_path=str(tmp_path / "interim.parquet"),
            processed_path=str(tmp_path / "processed.parquet"),
        ),
    )


def raw_market(
    ticker: str,
    result: str = "yes",
    event_ticker: str | None = None,
    status: str = "finalized",
    last_price: str = "0.9900",
    bid: str = "0.0000",
    ask: str = "1.0000",
    volume: str = "500.00",
    open_time: str = "2026-02-28T12:00:00Z",
    close_time: str = "2026-03-01T18:00:00Z",
    settlement_ts: str = "2026-03-01T18:05:00Z",
    **overrides,
) -> dict:
    """A market record shaped like the real historical one."""
    m = {
        "ticker": ticker,
        # Kalshi tickers are `<event>-<strike>`, so siblings of one event share
        # everything but the last segment. Pass `event_ticker` to make two
        # markets siblings explicitly.
        "event_ticker": ticker.rsplit("-", 1)[0] if event_ticker is None else event_ticker,
        "status": status,
        "title": f"Will {ticker} happen?",
        "result": result,
        "last_price_dollars": last_price,
        "previous_price_dollars": last_price,
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "volume_fp": volume,
        "open_time": open_time,
        "close_time": close_time,
        "settlement_ts": settlement_ts,
    }
    m.update(overrides)
    return m


def write_page(root: Path, category: str, series: str, markets: list[dict], page: int = 1) -> None:
    path = root / "kalshi" / category / series / f"page_{page:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"markets": markets, "cursor": ""}), encoding="utf-8")


def write_trades(
    root: Path, category: str, series: str, records: list[tuple[str, str | None]], horizon="T1h"
) -> None:
    """`records` is (ticker, yes_price or None-for-no-trade)."""
    path = root / "kalshi" / horizon / category / f"{series}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ticker, price in records:
            trade = None if price is None else {"yes_price_dollars": price, "ticker": ticker}
            fh.write(json.dumps({"ticker": ticker, "trade": trade}) + "\n")


def run(tmp_path, config=None):
    config = config or make_config(tmp_path)
    return parse_raw_to_interim(
        tmp_path / "raw", tmp_path / "trades", tmp_path / "out.parquet", config
    )


def run_processed(tmp_path, markets, config=None, category="Economics"):
    """Parse synthetic markets, then apply the inclusion criteria to them."""
    config = config or make_config(tmp_path)
    write_page(tmp_path / "raw", category, "KXFED", markets)
    write_trades(
        tmp_path / "trades",
        category,
        "KXFED",
        [(m["ticker"], "0.3000") for m in markets],
    )
    parse_raw_to_interim(
        tmp_path / "raw", tmp_path / "trades", tmp_path / "interim.parquet", config
    )
    return interim_to_processed(
        tmp_path / "interim.parquet", tmp_path / "processed.parquet", config
    )


# -- compute_implied_price -------------------------------------------------


def test_horizon_trade_reads_the_yes_leg():
    price = compute_implied_price(
        raw_market("A"), "horizon_trade", {"yes_price_dollars": "0.3700"}
    )
    assert price == pytest.approx(0.37)


def test_horizon_trade_is_none_without_a_trade():
    assert compute_implied_price(raw_market("A"), "horizon_trade", None) is None


def test_last_trade_reads_the_snapshot_price():
    assert compute_implied_price(raw_market("A", last_price="0.6200"), "last_trade") == pytest.approx(0.62)


def test_bid_ask_mid_refuses_a_zero_one_book():
    # 54.9% of settled books look like this. Returning 0.50 would put a
    # fabricated price into the calibration curve for half the corpus.
    assert compute_implied_price(raw_market("A", bid="0.0000", ask="1.0000"), "bid_ask_mid") is None


def test_bid_ask_mid_works_on_a_real_quote():
    m = raw_market("A", bid="0.4000", ask="0.4400")
    assert compute_implied_price(m, "bid_ask_mid") == pytest.approx(0.42)


def test_close_price_reads_previous_price():
    m = raw_market("A", last_price="0.1000")
    m["previous_price_dollars"] = "0.1500"
    assert compute_implied_price(m, "close_price") == pytest.approx(0.15)


@pytest.mark.parametrize("bad", ["1.5000", "-0.2000", "abc", None])
def test_prices_outside_the_unit_interval_are_rejected(bad):
    assert compute_implied_price(raw_market("A", last_price=bad), "last_trade") is None


def test_unknown_method_raises():
    with pytest.raises(ValueError, match="unknown price_method"):
        compute_implied_price(raw_market("A"), "vwap")


# -- load_horizon_trades ---------------------------------------------------


def test_absent_and_null_trades_are_different_things(tmp_path):
    # "asked, nothing was trading" is a documented exclusion; "never fetched"
    # is missing work. They must not collapse into one bucket.
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("A", "0.4000"), ("B", None)])

    trades = load_horizon_trades(tmp_path / "trades", 1.0)

    assert trades["A"]["yes_price_dollars"] == "0.4000"
    assert trades["B"] is None
    assert "C" not in trades


def test_malformed_trade_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "trades" / "kalshi" / "T1h" / "Economics" / "KXFED.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ticker": "A", "trade": None}) + "\n" + '{"ticker": "B", "tra',
        encoding="utf-8",
    )

    assert set(load_horizon_trades(tmp_path / "trades", 1.0)) == {"A"}


# -- parse_raw_to_interim --------------------------------------------------


def test_parses_a_binary_market_into_a_typed_row(tmp_path):
    write_page(tmp_path / "raw", "Economics", "KXFED", [raw_market("A", result="yes")])
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("A", "0.3000")])

    df, log = run(tmp_path)

    assert len(df) == 1
    row = df.iloc[0]
    assert row["ticker"] == "A"
    assert row["venue"] == "kalshi"
    assert row["category"] == "Economics"
    assert row["implied_price"] == pytest.approx(0.30)
    assert row["outcome"] == 1
    assert row["volume"] == pytest.approx(500.0)
    assert log.kept == 1 and log.dropped == 0


def test_no_result_maps_to_outcome_zero(tmp_path):
    write_page(tmp_path / "raw", "Economics", "KXFED", [raw_market("A", result="no")])
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("A", "0.3000")])

    df, _ = run(tmp_path)

    assert df.iloc[0]["outcome"] == 0


def test_voided_and_scalar_markets_are_dropped_with_named_reasons(tmp_path):
    write_page(
        tmp_path / "raw",
        "Economics",
        "KXFED",
        [
            raw_market("GOOD"),
            raw_market("VOID", result=""),
            raw_market("SCALAR", result="scalar"),
        ],
    )
    write_trades(
        tmp_path / "trades",
        "Economics",
        "KXFED",
        [("GOOD", "0.4000"), ("VOID", "0.4000"), ("SCALAR", "0.4000")],
    )

    df, log = run(tmp_path)

    assert list(df["ticker"]) == ["GOOD"]
    assert log.reasons["non_binary_result:empty"] == 1
    assert log.reasons["non_binary_result:scalar"] == 1


def test_market_with_no_trade_before_the_horizon_is_dropped_and_counted(tmp_path):
    write_page(tmp_path / "raw", "Economics", "KXFED", [raw_market("A"), raw_market("B")])
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("A", "0.4000"), ("B", None)])

    df, log = run(tmp_path)

    assert list(df["ticker"]) == ["A"]
    assert log.reasons["no_trade_before_horizon"] == 1
    assert log.by_reason_category["no_trade_before_horizon"]["Economics"] == 1


def test_market_never_fetched_is_a_separate_reason(tmp_path):
    write_page(tmp_path / "raw", "Economics", "KXFED", [raw_market("A"), raw_market("MISSING")])
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("A", "0.4000")])

    _, log = run(tmp_path)

    assert log.reasons["horizon_not_ingested"] == 1
    assert log.reasons["no_trade_before_horizon"] == 0


def test_duplicates_survive_interim_so_the_layer_can_report_them(tmp_path):
    # Deduplication is a processed-layer judgment. If interim silently deduped,
    # the cleaning log could never say how many duplicates there were.
    raw = tmp_path / "raw"
    write_page(raw, "Economics", "KXFED", [raw_market("DUP")])
    write_page(raw, "Sports", "KXNBA", [raw_market("DUP")])
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("DUP", "0.4000")])

    df, log = run(tmp_path)

    assert len(df) == 2
    assert log.kept == 2


def test_missing_timestamps_are_dropped(tmp_path):
    write_page(
        tmp_path / "raw",
        "Economics",
        "KXFED",
        [raw_market("A"), raw_market("B", settlement_ts=None), raw_market("C", open_time="junk")],
    )
    write_trades(
        tmp_path / "trades",
        "Economics",
        "KXFED",
        [("A", "0.4"), ("B", "0.4"), ("C", "0.4")],
    )

    df, log = run(tmp_path)

    assert list(df["ticker"]) == ["A"]
    assert log.reasons["missing_timestamp"] == 2


def test_model_rejection_is_counted_not_raised(tmp_path):
    # settle_ts before open_ts violates a Contract invariant. One anomalous
    # record must not abort a 145k-row parse.
    write_page(
        tmp_path / "raw",
        "Economics",
        "KXFED",
        [
            raw_market("A"),
            raw_market("BACKWARDS", open_time="2026-03-05T00:00:00Z"),
        ],
    )
    write_trades(
        tmp_path / "trades", "Economics", "KXFED", [("A", "0.4"), ("BACKWARDS", "0.4")]
    )

    df, log = run(tmp_path)

    assert list(df["ticker"]) == ["A"]
    assert log.reasons["model_validation_failed"] == 1


def test_parquet_is_written_and_round_trips(tmp_path):
    write_page(tmp_path / "raw", "Economics", "KXFED", [raw_market("A")])
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("A", "0.4000")])

    df, _ = run(tmp_path)
    reloaded = pd.read_parquet(tmp_path / "out.parquet")

    assert len(reloaded) == len(df) == 1
    assert reloaded.iloc[0]["ticker"] == "A"


def test_last_trade_method_needs_no_trades_directory(tmp_path):
    # The degenerate method must still run end to end -- the writeup plots its
    # curve as the demonstration of why design decision doc 003 rejects it.
    write_page(tmp_path / "raw", "Economics", "KXFED", [raw_market("A", last_price="0.9900")])

    df, log = run(tmp_path, make_config(tmp_path, method="last_trade"))

    assert df.iloc[0]["implied_price"] == pytest.approx(0.99)
    assert log.reasons["horizon_not_ingested"] == 0


# -- event structure and status (design decision doc 004) ------------------


def test_event_ticker_is_carried_so_siblings_can_be_clustered(tmp_path):
    """The 250 golfers in one field are one outcome, not 250 draws."""
    write_page(
        tmp_path / "raw",
        "Sports",
        "KXPGA",
        [
            raw_market("KXPGA-26-SCHEFFLER", event_ticker="KXPGA-26", result="yes"),
            raw_market("KXPGA-26-MCILROY", event_ticker="KXPGA-26", result="no"),
        ],
    )
    write_trades(
        tmp_path / "trades",
        "Sports",
        "KXPGA",
        [("KXPGA-26-SCHEFFLER", "0.2000"), ("KXPGA-26-MCILROY", "0.1500")],
    )

    df, _ = run(tmp_path)

    assert len(df) == 2
    assert set(df["event_ticker"]) == {"KXPGA-26"}


def test_market_without_an_event_ticker_is_dropped_and_counted(tmp_path):
    """An unclusterable row is worse than a missing one: it pools under one key."""
    write_page(
        tmp_path / "raw",
        "Economics",
        "KXFED",
        [raw_market("A"), raw_market("B", event_ticker="")],
    )
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("A", "0.3000"), ("B", "0.4000")])

    df, log = run(tmp_path)

    assert list(df["ticker"]) == ["A"]
    assert log.reasons["missing_event_ticker"] == 1
    assert log.by_reason_category["missing_event_ticker"]["Economics"] == 1


def test_unfinalized_market_raises_rather_than_being_dropped(tmp_path):
    """Status is asserted, not filtered: an open market's `result` is not an outcome."""
    write_page(
        tmp_path / "raw", "Economics", "KXFED", [raw_market("A", status="active")]
    )
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("A", "0.3000")])

    with pytest.raises(ValueError, match="finalized"):
        run(tmp_path)

# -- interim -> processed (design decision doc 004) ------------------------


def test_window_is_applied_to_close_time_and_both_bounds_are_inclusive(tmp_path):
    """The window runs 2026-01-01 to 2026-06-06, whole days, UTC."""
    markets = [
        raw_market("BEFORE", close_time="2025-12-31T23:00:00Z"),
        raw_market("FIRST_DAY", close_time="2026-01-01T00:00:00Z"),
        raw_market("LAST_DAY", close_time="2026-06-06T23:59:00Z"),
        raw_market("AFTER", close_time="2026-06-07T00:30:00Z"),
    ]
    df, log = run_processed(tmp_path, markets)

    assert set(df["ticker"]) == {"FIRST_DAY", "LAST_DAY"}
    assert log.reasons["close_outside_window"] == 2


def test_window_ignores_settlement_lag(tmp_path):
    """A market closing inside the window stays even if settlement lands outside."""
    markets = [
        raw_market(
            "LAGGED",
            close_time="2026-06-06T12:00:00Z",
            settlement_ts="2026-06-20T12:00:00Z",
        )
    ]
    df, log = run_processed(tmp_path, markets)

    assert list(df["ticker"]) == ["LAGGED"]
    assert log.dropped == 0


def test_untraded_market_is_excluded_and_counted(tmp_path):
    markets = [raw_market("TRADED"), raw_market("NEVER", volume="0.00")]
    df, log = run_processed(tmp_path, markets)

    assert list(df["ticker"]) == ["TRADED"]
    assert log.reasons["no_volume"] == 1
    assert log.by_reason_category["no_volume"]["Economics"] == 1


def test_there_is_no_volume_floor_above_zero(tmp_path):
    """Liquidity is a segmentation axis, not a filter -- filtering on it would
    remove the thin markets the favorite-longshot bias lives in."""
    df, _ = run_processed(tmp_path, [raw_market("THIN", volume="1.00")])

    assert list(df["ticker"]) == ["THIN"]


def test_every_sibling_of_a_large_event_survives(tmp_path):
    """All 250 golfers stay: dropping siblings would delete the longshot tail."""
    markets = [
        raw_market(f"KXPGA-26-P{i}", event_ticker="KXPGA-26", result="yes" if i == 0 else "no")
        for i in range(5)
    ]
    df, log = run_processed(tmp_path, markets, category="Sports")

    assert len(df) == 5
    assert df["event_ticker"].nunique() == 1
    assert log.dropped == 0


def test_duplicate_tickers_raise_rather_than_being_deduplicated(tmp_path):
    """De-duplication is an ingest invariant; a duplicate here means it broke."""
    config = make_config(tmp_path)
    write_page(tmp_path / "raw", "Economics", "KXFED", [raw_market("A")])
    write_page(tmp_path / "raw", "Economics", "KXCPI", [raw_market("A")], page=1)
    write_trades(tmp_path / "trades", "Economics", "KXFED", [("A", "0.3000")])
    parse_raw_to_interim(
        tmp_path / "raw", tmp_path / "trades", tmp_path / "interim.parquet", config
    )

    with pytest.raises(ValueError, match="duplicate ticker"):
        interim_to_processed(
            tmp_path / "interim.parquet", tmp_path / "processed.parquet", config
        )


def test_processed_parquet_round_trips_with_the_clustering_key(tmp_path):
    df, _ = run_processed(tmp_path, [raw_market("KXFED-26MAR-A", event_ticker="KXFED-26MAR")])

    reloaded = pd.read_parquet(tmp_path / "processed.parquet")
    assert list(reloaded["ticker"]) == ["KXFED-26MAR-A"]
    assert list(reloaded["event_ticker"]) == ["KXFED-26MAR"]
    assert len(reloaded) == len(df)

# -- DropLog ---------------------------------------------------------------


def test_droplog_reports_share_kept_and_spread_by_category():
    log = DropLog()
    log.kept = 3
    log.drop("no_trade_before_horizon", "Sports")
    log.drop("no_trade_before_horizon", "Sports")
    log.drop("non_binary_result:scalar", "Crypto")

    text = log.format()

    assert log.seen == 6
    assert "kept=3" in text
    assert "Sports=2" in text
    assert "Crypto=1" in text
