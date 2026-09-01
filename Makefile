.PHONY: install ingest ingest-trades ingest-summary trades-progress trades-watch clean analyze figures test all

install:
	pip install -r requirements.txt

# Pass 1: settled market pages -> data/raw/
ingest:
	python -m src.ingest.kalshi

# Pass 2: a price per market at each horizon before close -> data/raw_trades/
# The settled snapshot cannot supply an implied probability (ADR 003).
ingest-trades:
	python -m src.ingest.trades

# The "did it actually work" dashboard over data/raw/.
ingest-summary:
	python -m src.ingest.summary

# The "how far along is it" dashboard over data/raw_trades/.
# Reads the files on disk, so it works on a running, finished, or killed pull.
trades-progress:
	python -m src.ingest.trades_progress

# Same, redrawing with a live rate and ETA. Run in a second terminal
# alongside `make ingest-trades`.
trades-watch:
	python -m src.ingest.trades_progress --watch

# raw -> interim
clean:
	python -m src.clean

# processed -> the headline calibration table (reports/calibration_table.csv).
# Deterministic: same parquet + same config.yaml => same digits, seed included.
analyze:
	python -m src.analysis.report

# processed -> reports/figures/*.png, from the same functions analyze prints,
# so a chart and the table under it cannot drift apart.
figures:
	python -m src.analysis.figures
	python -m src.strategy.figures

test:
	python -m pytest -q

all: install ingest ingest-trades clean analyze figures test
