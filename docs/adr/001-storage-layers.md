# Design decision doc 001 - Three storage layers: raw, interim, processed

**Status:** Accepted (Phase 1, 2026-08-09)

## Context

The pipeline turns Kalshi API responses into a table of resolved contracts
that a calibration study can be run against. That transformation bundles
several distinct kinds of work:

- **Transport** -- pagination, retries, rate limiting. Fails for network
  reasons; expensive to repeat; hits a third party we do not control.
- **Parsing** -- reading `yes_ask` and `settlement_ts` out of a JSON blob into
  typed fields. Fails for *our* reasons, and does so silently.
- **Research judgment** -- which price counts as "the market's implied
  probability," which markets are in the study, how a NO-framed contract is
  flipped to P(event). Not a bug class at all; a set of decisions that will be
  revisited and must be defensible.

Collapsing these into one step -- fetch, parse, filter, write Parquet -- means
a mistake in the cheapest, most-likely-to-be-wrong stage (parsing) forces
repeating the most expensive one (transport). Worse, it makes the dataset
unprovable: once responses have been mutated on the way to disk, there is no
artifact left that shows what the API actually said.

Kalshi adds a specific hazard. Its historical endpoints serve a moving window
(see `docs/data-sources.md`), so a re-pull months from now is *not* guaranteed
to return the same markets. Raw responses are not reliably re-derivable.

## Decision

Three layers, each derived deterministically from the one above:

| Layer | Path | Format | Written by | Contract |
|---|---|---|---|---|
| raw | `data/raw/<venue>/<category>/<series>/page_NNNN.json` | JSON | `src/ingest/` | Byte-faithful API responses. **Immutable.** Never edited, never trimmed, never re-serialised with fields dropped. |
| interim | `data/interim/contracts.parquet` | Parquet | `src/clean.py` | One row per contract, parsed and typed via the pydantic `Contract` model. No research filtering. |
| processed | `data/processed/contracts.parquet` | Parquet | `src/clean.py` | Analysis-ready. Filtered, deduped, every row P(event) with `outcome` in {0,1}. Every drop logged with a reason. |

Rules that make the layering mean something:

1. **Raw is append-only.** The ingestion layer writes pages and nothing else.
   No parsing, no date filtering, no field selection -- even the page that
   overshoots the study window is written whole, because trimming it would
   break the verbatim guarantee.
2. **Each layer is a pure function of the one above it.** Deleting `interim/`
   and `processed/` and re-running must reproduce them exactly, with no
   network access.
3. **The split between interim and processed is the split between parsing
   and judgment.** Interim answers "what did the API say, typed." Processed
   answers "what is in the study." Anything defensible in an interview
   belongs in the second step, where it can be varied from `config.yaml`.
4. **Raw JSON, derived Parquet.** JSON is the byte-faithful record of a JSON
   API. The analysis is columnar and read-heavy, so the derived layers are
   Parquet: compact, typed, and free of the "is this price a string?" bugs
   CSV reintroduces at every read.

## Consequences

**Gained**

- A parsing bug costs a re-run of `src/clean.py` -- seconds, offline -- rather
  than a full re-fetch against a window that may have shifted underneath us.
- Provable lineage. Every number in the report traces to a specific JSON page
  on disk, and `data/raw/kalshi/_cutoff.json` records what "as much history as
  available" meant on the day of the pull.
- Research decisions become reversible. "What if longshots were <15c?" and
  "what if we used the bid-ask mid instead?" are re-runs of one layer, which
  is what makes sensitivity analysis cheap enough to actually do.
- The layers are separately testable, because their failure modes are
  separate: transport is tested with a mock transport, parsing with synthetic
  fixtures.

**Given up**

- Disk. Raw JSON is verbose and largely redundant with the processed table.
  Acceptable at this scale; `data/` is gitignored.
- A second pass over the data, so the pipeline is not the fastest possible.
  Irrelevant -- this is a batch study, not a live system.
- Discipline is required. The layering only holds if the ingestion layer stays
  free of parsing logic. `_parse_ts` exists in `src/ingest/kalshi.py` solely to
  decide when a series walk has reached the start of the window; it never
  changes what is written.

## Alternatives considered

- **Fetch straight to Parquet.** Rejected: couples parsing bugs to re-fetch
  cost and destroys the evidentiary record.
- **Raw + processed, no interim.** Rejected: it merges parsing and judgment
  into one function, so "we dropped 12% of rows" cannot be attributed between
  "malformed" and "deliberately excluded" -- exactly the distinction
  `docs/cleaning-log.md` has to report.
- **A database instead of files.** Rejected: adds a service to reproduce for
  no gain at this size, and files diff, copy and inspect with normal tools.
