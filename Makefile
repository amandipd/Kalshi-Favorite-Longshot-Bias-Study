.PHONY: install ingest ingest-trades ingest-summary trades-progress trades-watch clean analyze test all

install:
	# TODO

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

analyze:
	# TODO

test:
	python -m pytest -q

all: install ingest ingest-trades clean analyze test
