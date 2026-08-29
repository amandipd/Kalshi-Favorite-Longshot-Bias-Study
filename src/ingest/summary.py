"""Health check over the raw layer: did the ingestion run actually work?

    python -m src.ingest.summary          # or: make ingest-summary

Answers the questions you ask right after a long run finishes -- how many
pages landed, how many contracts they hold, what window they span, which
categories are represented -- by reading the files on disk rather than by
trusting the run's own log. `IngestStats` reports what a *single* run did;
this reports what is *there*, across every run that ever wrote to the layer.

Deliberately shallow. It touches only the fields it needs to count and bound
(`ticker`, `settlement_ts`, `result`, `volume_fp`) and applies no research
filtering, no deduplication across series, no window trimming -- that is
`src/clean.py`'s job (see docs/adr/001-storage-layers.md). A number here is
"what the API gave us," not "what is in the study." Expect the processed
dataset to be smaller.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.config import get_config

# Metadata files written alongside the pages; not pages of markets themselves.
METADATA_FILENAMES = {"_cutoff.json", "_series.json"}


@dataclass
class CategorySummary:
    """Per-category counts. `series` is directories walked, not series selected."""

    series: int = 0
    pages: int = 0
    markets: int = 0


@dataclass
class RawSummary:
    """What the raw layer holds, for one venue."""

    venue: str
    page_files: int = 0
    metadata_files: int = 0
    markets: int = 0
    unique_tickers: int = 0
    duplicate_tickers: int = 0
    settlement_min: date | None = None
    settlement_max: date | None = None
    unparsed_settlements: int = 0
    total_volume: float = 0.0
    results: Counter = field(default_factory=Counter)
    categories: dict[str, CategorySummary] = field(default_factory=dict)
    cutoff: dict[str, Any] | None = None
    empty_pages: int = 0

    def format(self) -> str:
        """Render as a report. Printed by main(); returned so tests can assert on it."""
        if self.page_files == 0:
            return (
                f"raw layer for venue={self.venue} is empty -- no page files found.\n"
                "Run `make ingest` first."
            )

        span = "unknown"
        if self.settlement_min and self.settlement_max:
            days = (self.settlement_max - self.settlement_min).days + 1
            span = f"{self.settlement_min} -> {self.settlement_max} ({days} days)"

        lines = [
            f"RAW LAYER SUMMARY -- venue={self.venue}",
            "",
            f"  page files        {self.page_files:>12,}",
            f"  metadata files    {self.metadata_files:>12,}",
            f"  markets           {self.markets:>12,}",
            f"  unique tickers    {self.unique_tickers:>12,}",
            f"  traded volume     {self.total_volume:>12,.0f}",
            f"  settlement span   {span}",
            "",
            f"  {'category':<14}{'series':>8}{'pages':>8}{'markets':>10}",
        ]
        for name in sorted(self.categories):
            c = self.categories[name]
            lines.append(f"  {name:<14}{c.series:>8,}{c.pages:>8,}{c.markets:>10,}")

        lines.append("")
        lines.append("  settlement results")
        for result, count in self.results.most_common():
            share = count / self.markets if self.markets else 0.0
            label = result or "(empty -- voided?)"
            lines.append(f"    {label:<20}{count:>10,}  {share:>6.1%}")

        # Anomalies. Each is legitimate in small numbers and a bug in large
        # ones, so they are reported as counts rather than suppressed or
        # raised on.
        notes = []
        if self.duplicate_tickers:
            notes.append(
                f"{self.duplicate_tickers:,} duplicate tickers "
                "(same market returned by more than one page or series)"
            )
        if self.unparsed_settlements:
            notes.append(
                f"{self.unparsed_settlements:,} markets with a missing or "
                "unparsable settlement_ts"
            )
        if self.empty_pages:
            notes.append(f"{self.empty_pages:,} pages holding zero markets")
        if notes:
            lines.append("")
            lines.append("  anomalies")
            lines.extend(f"    - {n}" for n in notes)

        if self.cutoff:
            lines.append("")
            lines.append(
                "  historical cutoff at ingest time: "
                f"{self.cutoff.get('market_settled_ts', 'unknown')}"
            )

        return "\n".join(lines)


def _settlement_date(market: dict[str, Any]) -> date | None:
    raw = market.get("settlement_ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except (ValueError, AttributeError):
        return None


def summarize_raw(raw_dir: Path | str, venue: str = "kalshi") -> RawSummary:
    """Scan `<raw_dir>/<venue>/` and count what is there.

    Walks `<category>/<series>/page_NNNN.json`. Pure: reads files, returns a
    summary, writes nothing.
    """
    venue_dir = Path(raw_dir) / venue
    summary = RawSummary(venue=venue)
    if not venue_dir.exists():
        return summary

    cutoff_path = venue_dir / "_cutoff.json"
    if cutoff_path.exists():
        summary.cutoff = json.loads(cutoff_path.read_text(encoding="utf-8"))
        summary.metadata_files += 1

    seen: set[str] = set()

    for category_dir in sorted(p for p in venue_dir.iterdir() if p.is_dir()):
        cat = CategorySummary()
        summary.metadata_files += sum(
            1 for f in category_dir.glob("*.json") if f.name in METADATA_FILENAMES
        )

        for series_dir in sorted(p for p in category_dir.iterdir() if p.is_dir()):
            cat.series += 1
            for page_path in sorted(series_dir.glob("page_*.json")):
                cat.pages += 1
                summary.page_files += 1
                markets = json.loads(page_path.read_text(encoding="utf-8")).get("markets", [])
                if not markets:
                    summary.empty_pages += 1

                for market in markets:
                    cat.markets += 1
                    summary.markets += 1

                    ticker = market.get("ticker")
                    if ticker in seen:
                        summary.duplicate_tickers += 1
                    elif ticker:
                        seen.add(ticker)

                    # `result` is "" on a voided market; keep it as its own
                    # bucket rather than dropping it -- voids are a number to
                    # watch, not noise to hide.
                    summary.results[market.get("result", "")] += 1

                    try:
                        summary.total_volume += float(market.get("volume_fp") or 0.0)
                    except (TypeError, ValueError):
                        pass

                    settled = _settlement_date(market)
                    if settled is None:
                        summary.unparsed_settlements += 1
                        continue
                    if summary.settlement_min is None or settled < summary.settlement_min:
                        summary.settlement_min = settled
                    if summary.settlement_max is None or settled > summary.settlement_max:
                        summary.settlement_max = settled

        summary.categories[category_dir.name] = cat

    summary.unique_tickers = len(seen)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize the raw ingestion layer.")
    parser.add_argument("--venue", default="kalshi")
    parser.add_argument("--raw-dir", default=None, help="Defaults to config.yaml's ingest.raw_dir.")
    args = parser.parse_args()

    raw_dir = args.raw_dir or get_config().ingest.raw_dir
    print(summarize_raw(raw_dir, args.venue).format())


if __name__ == "__main__":
    main()
