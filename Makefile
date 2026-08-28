.PHONY: install ingest clean analyze test all

install:
	# TODO

ingest:
	python -m src.ingest.kalshi

clean:
	# TODO

analyze:
	# TODO

test:
	# TODO

all: install ingest clean analyze test
