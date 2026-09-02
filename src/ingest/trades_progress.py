"""Progress dashboard for the horizon-price pass: how much of the pull is done?

    python -m src.ingest.trades_progress            # or: make trades-progress
    python -m src.ingest.trades_progress --watch    # live, while a pull runs

The horizon pass (`src/ingest/trades.py`) is a long job -- ~143k markets per
horizon at roughly 12 markets/second -- and it prints nothing but a per-2000
progress line, so a run that has been going for an hour gives no answer to the
only question worth asking: how far along is it, and when does it land?

This answers that from the files on disk rather than from a running process,
which means it works on a pull that is running, one that finished, and one
that was killed halfway. It is the same relationship `summary.py` has to
`IngestStats`: that reports what one run did, this reports what is *there*.

The denominator is `trades.iter_binary_markets` -- the exact iterator the pass
itself walks -- so "100%" here means "the pass would now fetch nothing," not
"some estimate was reached." The numerator counts recorded tickers the way
`trades._load_done` does, tolerating a truncated final line, so a series shows
as done precisely when a re-run would skip it.

Deliberately read-only and stateless: it never writes, and holds no checkpoint
of its own. Rate and ETA in `--watch` come from comparing successive scans.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

from src.config import get_config
from src.ingest.trades import VENUE, horizon_dirname, iter_binary_markets

# Width of the rendered bar, in characters. Narrow enough that a horizon line
# plus its counts fits an 80-column terminal.
BAR_WIDTH = 28

# Seconds between redraws in --watch. Each tick re-reads every JSONL in the
# horizon (tens of MB once a pull is well along), so this trades promptness
# against sitting on the disk -- and the pull itself is the priority.
DEFAULT_WATCH_INTERVAL = 10.0

# Window over which --watch averages its rate. Short enough that a stall shows
# up within a minute or two, long enough not to jitter with per-series pauses.
RATE_WINDOW_SECONDS = 180.0

# Refuse to quote a rate until the samples span at least this long. Measured
# throughput is lumpy over short spans -- the pass flushes on a counter rather
# than on a clock, and the API's own latency wanders (84ms/request for an hour,
# then 400ms for two minutes, observed 2026-08-29). A 20-second sample caught
# during either produces a confidently wrong ETA, and no ETA beats a wrong one.
MIN_RATE_ELAPSED_SECONDS = 60.0

SeriesKey = tuple[str, str]


@dataclass
class SeriesProgress:
    """One series at one horizon: how many of its markets have been priced."""

    category: str
    series: str
    expected: int
    done: int = 0
    no_trade: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.expected - self.done)

    @property
    def fraction(self) -> float:
        return (self.done / self.expected) if self.expected else 1.0


@dataclass
class CategoryProgress:
    """Per-category rollup within a horizon."""

    expected: int = 0
    done: int = 0
    no_trade: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.expected - self.done)

    @property
    def fraction(self) -> float:
        return (self.done / self.expected) if self.expected else 1.0


@dataclass
class HorizonProgress:
    """What one horizon's directory holds, against what the pass would fetch."""

    horizon_hours: float
    series: list[SeriesProgress] = field(default_factory=list)

    @property
    def name(self) -> str:
        return horizon_dirname(self.horizon_hours)

    @property
    def expected(self) -> int:
        return sum(s.expected for s in self.series)

    @property
    def done(self) -> int:
        return sum(s.done for s in self.series)

    @property
    def no_trade(self) -> int:
        return sum(s.no_trade for s in self.series)

    @property
    def remaining(self) -> int:
        return max(0, self.expected - self.done)

    @property
    def fraction(self) -> float:
        return (self.done / self.expected) if self.expected else 1.0

    @property
    def complete(self) -> bool:
        return self.remaining == 0 and self.expected > 0

    def by_category(self) -> dict[str, CategoryProgress]:
        out: dict[str, CategoryProgress] = {}
        for s in self.series:
            cat = out.setdefault(s.category, CategoryProgress())
            cat.expected += s.expected
            cat.done += s.done
            cat.no_trade += s.no_trade
        return out

    def incomplete_series(self) -> list[SeriesProgress]:
        """Unfinished series, most work left first -- what the pass has still to do."""
        return sorted(
            (s for s in self.series if s.remaining > 0),
            key=lambda s: s.remaining,
            reverse=True,
        )

    def in_flight(self) -> list[SeriesProgress]:
        """Series started but not finished.

        Normally at most one -- the pass works a series at a time -- so this
        naming the series is how you see *where* a running pull currently is.
        More than one means an earlier run was killed partway and this one has
        not come back round to those series yet.
        """
        return [s for s in self.series if 0 < s.done < s.expected]


@dataclass
class TradesProgress:
    """Every configured horizon, measured against the raw layer."""

    venue: str
    horizons: list[HorizonProgress] = field(default_factory=list)
    markets_available: int = 0

    @property
    def expected(self) -> int:
        return sum(h.expected for h in self.horizons)

    @property
    def done(self) -> int:
        return sum(h.done for h in self.horizons)

    @property
    def remaining(self) -> int:
        return max(0, self.expected - self.done)

    @property
    def fraction(self) -> float:
        return (self.done / self.expected) if self.expected else 1.0

    def format(
        self,
        top_series: int = 5,
        rate: float | None = None,
        horizon_rates: dict[str, float] | None = None,
    ) -> str:
        """Render the report. Printed by main(); returned so tests can assert on it.

        `rate` is markets/second, supplied by --watch; without it no ETA is
        shown, because a single scan cannot know how fast anything is moving.

        `horizon_rates` gives the same per horizon, which is the number worth
        reading: only one horizon runs at a time, so the overall ETA covers
        work that has not been started and answers a question nobody asked.
        The horizon's own ETA is when the pull you are watching actually lands.
        """
        if self.markets_available == 0:
            return (
                f"no binary markets in the raw layer for venue={self.venue}.\n"
                "Run `make ingest` first -- the horizon pass prices what pass 1 found."
            )

        lines = [
            f"TRADES PULL PROGRESS -- venue={self.venue}",
            "",
            f"  binary markets in raw layer   {self.markets_available:>12,}",
            f"  horizons configured           {len(self.horizons):>12,}",
            f"  market-prices to fetch        {self.expected:>12,}",
            f"  fetched                       {self.done:>12,}",
            f"  remaining                     {self.remaining:>12,}",
            "",
            f"  OVERALL  {_bar(self.fraction)} {self.fraction:>6.1%}",
        ]

        if rate is not None:
            lines.append(f"           {_rate_line(rate, self.remaining)}")
            lines.append("           (all horizons; each runs in turn, so see the horizon below)")

        for horizon in self.horizons:
            lines.append("")
            if horizon.complete:
                status = "complete"
            elif horizon.done:
                status = "in progress"
            else:
                status = "not started"
            lines.append(
                f"  {horizon.name:<6} {_bar(horizon.fraction)} {horizon.fraction:>6.1%}  "
                f"{horizon.done:>7,} / {horizon.expected:<7,} {status}"
            )

            # Only the horizon actually moving gets an ETA. A stationary one
            # is either finished or waiting its turn, and "ETA never" from a
            # zero rate would be noise on both.
            horizon_rate = (horizon_rates or {}).get(horizon.name)
            if horizon_rate and horizon.remaining:
                lines.append(f"         {_rate_line(horizon_rate, horizon.remaining)}")

            categories = horizon.by_category()
            for name in sorted(categories):
                cat = categories[name]
                # no_trade is a share of what has been fetched, not of what
                # exists -- it is an exclusion rate, and comparing it across
                # categories is how an uneven one shows up (see design decision doc 003).
                share = (cat.no_trade / cat.done) if cat.done else 0.0
                lines.append(
                    f"    {name:<12} {_bar(cat.fraction, 16)} {cat.fraction:>6.1%}  "
                    f"{cat.done:>7,} / {cat.expected:<7,}  no_trade {cat.no_trade:>6,} "
                    f"({share:>5.1%})"
                )

            for s in horizon.in_flight():
                lines.append(
                    f"    -> in flight: {s.category}/{s.series} "
                    f"({s.done:,}/{s.expected:,}, {s.remaining:,} left)"
                )

            pending = horizon.incomplete_series()
            if pending and top_series > 0:
                lines.append(f"    {len(pending):,} series incomplete, most work left first:")
                for s in pending[:top_series]:
                    lines.append(
                        f"      {s.remaining:>7,} left  {s.category}/{s.series} "
                        f"({s.done:,}/{s.expected:,})"
                    )
                if len(pending) > top_series:
                    lines.append(f"      ... and {len(pending) - top_series:,} more")

        return "\n".join(lines)


def _supports_unicode() -> bool:
    """Whether stdout can render block characters.

    A Windows console still on a legacy code page raises UnicodeEncodeError on
    them, and a progress bar is not worth crashing a report over.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "█░".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _bar(fraction: float, width: int = BAR_WIDTH) -> str:
    fraction = min(1.0, max(0.0, fraction))
    filled = int(fraction * width)
    # Never render a bar as visibly full until it actually is: at 99.9% of
    # 143k markets there are still 140 to go, and a full bar would say done.
    if filled == width and fraction < 1.0:
        filled = width - 1
    full, empty = ("█", "░") if _supports_unicode() else ("#", "-")
    return f"[{full * filled}{empty * (width - filled)}]"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def _rate_line(rate: float, remaining: int) -> str:
    if rate <= 0:
        return "stalled -- no markets fetched over the rate window"
    eta = remaining / rate
    return f"{rate:.1f} markets/sec, ETA {_format_duration(eta)} for {remaining:,} remaining"


def expected_by_series(raw_dir: Path | str, venue: str = VENUE) -> Counter[SeriesKey]:
    """Binary markets per (category, series) that the horizon pass would fetch.

    Delegates to the pass's own iterator so the denominator cannot drift from
    what actually gets requested -- including its deduplication by ticker,
    which assigns a market appearing under two series to the first one seen.
    """
    counts: Counter[SeriesKey] = Counter()
    for market in iter_binary_markets(raw_dir, venue):
        counts[(market["category"], market["series"])] += 1
    return counts


def scan_horizon_file(path: Path) -> tuple[set[str], int]:
    """Tickers recorded in one JSONL, and how many carried no trade.

    Mirrors `trades._load_done`: a truncated final line from a killed run is
    skipped rather than trusted, and tickers are a set, so a ticker written
    twice counts once -- exactly as the pass's resume check counts it.
    `tests/test_ingest_trades_progress.py` pins the two against each other.
    """
    tickers: set[str] = set()
    no_trade = 0
    if not path.exists():
        return tickers, no_trade

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                ticker = record["ticker"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if ticker in tickers:
                continue
            tickers.add(ticker)
            if record.get("trade") is None:
                no_trade += 1
    return tickers, no_trade


def measure_horizon(
    horizon_hours: float,
    expected: Counter[SeriesKey],
    trades_dir: Path | str,
    venue: str = VENUE,
) -> HorizonProgress:
    """Compare one horizon's directory against the expected series counts."""
    root = Path(trades_dir) / venue / horizon_dirname(horizon_hours)
    progress = HorizonProgress(horizon_hours=horizon_hours)

    for (category, series), count in sorted(expected.items()):
        entry = SeriesProgress(category=category, series=series, expected=count)
        tickers, no_trade = scan_horizon_file(root / category / f"{series}.jsonl")
        # Clamp: a series can hold tickers the current raw layer no longer
        # yields (a page rewritten, a filter changed), and "104% done" reads
        # as a bug in the dashboard rather than as the drift it is.
        entry.done = min(len(tickers), count)
        entry.no_trade = no_trade
        progress.series.append(entry)

    return progress


def measure(
    raw_dir: Path | str,
    trades_dir: Path | str,
    horizons: list[float],
    venue: str = VENUE,
) -> TradesProgress:
    """Measure every horizon against the raw layer. Reads files, writes nothing."""
    expected = expected_by_series(raw_dir, venue)
    progress = TradesProgress(venue=venue, markets_available=sum(expected.values()))
    progress.horizons = [measure_horizon(h, expected, trades_dir, venue) for h in horizons]
    return progress


class RateTracker:
    """Markets/second over a trailing window, from successive scans.

    A trailing window rather than an average since start: a pull that stalls,
    or that gets killed and resumed, should show its *current* speed, where an
    all-time average would keep quoting the speed it used to have.
    """

    def __init__(
        self,
        window_seconds: float = RATE_WINDOW_SECONDS,
        min_elapsed_seconds: float = MIN_RATE_ELAPSED_SECONDS,
    ) -> None:
        self.window_seconds = window_seconds
        self.min_elapsed_seconds = min_elapsed_seconds
        self._samples: deque[tuple[float, int]] = deque()

    def update(self, done: int, now: float | None = None) -> float | None:
        """Record a scan; return markets/second, or None if it is too early to say."""
        now = time.monotonic() if now is None else now
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.window_seconds:
            self._samples.popleft()

        if len(self._samples) < 2:
            return None
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        elapsed = t1 - t0
        if elapsed < self.min_elapsed_seconds:
            return None
        return (d1 - d0) / elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Progress of the horizon-price pass.")
    parser.add_argument("--venue", default=VENUE)
    parser.add_argument("--raw-dir", default=None, help="Defaults to config.yaml's ingest.raw_dir.")
    parser.add_argument(
        "--trades-dir", default=None, help="Defaults to config.yaml's ingest.trades.trades_dir."
    )
    parser.add_argument(
        "--horizon",
        type=float,
        action="append",
        default=None,
        help="Hours before close. Repeatable. Defaults to config.yaml's horizons_hours.",
    )
    parser.add_argument(
        "--series", type=int, default=5, help="Incomplete series to list per horizon (0 for none)."
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        type=float,
        const=DEFAULT_WATCH_INTERVAL,
        default=None,
        metavar="SECONDS",
        help="Redraw until complete, adding a live rate and ETA "
        f"(default every {DEFAULT_WATCH_INTERVAL:.0f}s).",
    )
    args = parser.parse_args()

    config = get_config()
    raw_dir = args.raw_dir or config.ingest.raw_dir
    trades_dir = args.trades_dir or config.ingest.trades.trades_dir
    horizons = args.horizon or list(config.ingest.trades.horizons_hours)

    if args.watch is None:
        print(measure(raw_dir, trades_dir, horizons, args.venue).format(top_series=args.series))
        return

    # The raw layer is finished before this pass starts, so the denominator is
    # scanned once (772 pages) and only the horizon files are re-read per tick.
    expected = expected_by_series(raw_dir, args.venue)
    tracker = RateTracker()
    horizon_trackers: dict[str, RateTracker] = {}
    try:
        while True:
            progress = TradesProgress(venue=args.venue, markets_available=sum(expected.values()))
            progress.horizons = [
                measure_horizon(h, expected, trades_dir, args.venue) for h in horizons
            ]
            rate = tracker.update(progress.done)
            horizon_rates: dict[str, float] = {}
            for horizon in progress.horizons:
                tracked = horizon_trackers.setdefault(horizon.name, RateTracker())
                horizon_rate = tracked.update(horizon.done)
                if horizon_rate is not None:
                    horizon_rates[horizon.name] = horizon_rate

            print("\033[H\033[2J", end="")
            print(
                progress.format(
                    top_series=args.series, rate=rate, horizon_rates=horizon_rates
                )
            )
            measuring = (
                ""
                if rate is not None
                else f" -- rate/ETA after {MIN_RATE_ELAPSED_SECONDS:.0f}s of samples"
            )
            print(f"\n  refreshed {time.strftime('%H:%M:%S')} -- Ctrl-C to stop{measuring}")

            if progress.remaining == 0:
                print("\n  all configured horizons complete.")
                return
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\n  stopped watching; the pull is unaffected.")


if __name__ == "__main__":
    main()
