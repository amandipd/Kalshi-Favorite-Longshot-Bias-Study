"""Scratch plotting script -- NOT part of the ingestion pipeline in src/.

Reads date_range_exploration.json (written by test.py) and renders the
monthly settled-market counts per category as small multiples, so the
date_range decision documented in docs/journal.d/ can be re-plotted without
re-hitting the Kalshi API.

    python plot_date_range_exploration.py
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
import seaborn as sns

DATA_PATH = Path(__file__).parent / "date_range_exploration.json"
OUT_PATH = Path(__file__).parent / "reports" / "figures" / "date_range_exploration.png"

CATEGORY_COLORS = {
    "Politics": "#2a78d6",
    "Economics": "#eb6834",
    "Sports": "#1baf7a",
    "Crypto": "#eda100",
}


def month_range(all_counts: dict[str, dict[str, int]]) -> list[str]:
    keys = [k for counts in all_counts.values() for k in counts]
    start = min(keys)
    end = max(keys)
    start_y, start_m = (int(x) for x in start.split("-"))
    end_y, end_m = (int(x) for x in end.split("-"))
    months = []
    y, m = start_y, start_m
    while (y, m) <= (end_y, end_m):
        months.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def main() -> None:
    data = json.loads(DATA_PATH.read_text())
    categories = data["categories"]
    all_counts = {cat: info["monthly_settled_counts"] for cat, info in categories.items()}
    months = month_range(all_counts)

    sns.set_style("whitegrid")
    fig, axes = plt.subplots(len(all_counts), 1, figsize=(11, 8), sharex=True)

    for ax, (category, counts) in zip(axes, all_counts.items()):
        values = [counts.get(m, 0) for m in months]
        ax.bar(months, values, color=CATEGORY_COLORS[category], width=0.8)
        ax.set_ylabel(category, rotation=0, ha="right", va="center", fontsize=10)
        ax.grid(axis="x", visible=False)
        sns.despine(ax=ax, left=False, bottom=True)

    axes[-1].set_xticks(months[::3])
    axes[-1].set_xticklabels(months[::3], rotation=45, ha="right", fontsize=8)
    fig.suptitle("Settled markets by month (top-20-by-volume series per category, sub-daily recurring series excluded)", fontsize=11)
    fig.tight_layout()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PATH, dpi=150)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
