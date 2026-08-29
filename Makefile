.PHONY: install ingest ingest-summary clean analyze test all

install:
	# TODO

ingest:
	python -m src.ingest.kalshi

# The "did it actually work" dashboard over data/raw/.
ingest-summary:
	python -m src.ingest.summary

clean:
	# TODO

analyze:
	# TODO

test:
	python -m pytest -q

all: install ingest clean analyze test
