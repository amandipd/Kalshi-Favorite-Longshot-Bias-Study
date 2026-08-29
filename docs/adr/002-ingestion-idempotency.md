# ADR 002 - Resumable, idempotent ingestion

**Status:** Accepted (Phase 1, 2026-08-09)

## Context

A full pull walks 4 categories x 20 series x N pages against a rate-limited
public API at ~15 requests/second. It takes long enough that the interesting
question is not "does it work" but "what happens when it stops halfway."

Runs stop for ordinary reasons: a laptop sleeps, the network drops, a 429
outlasts the retry budget, the process is killed. Three properties have to
hold when that happens:

1. **Resumable.** A re-run continues rather than starting over.
2. **Idempotent.** Running twice produces the same raw layer as running once.
   No duplicate pages, no doubled markets downstream.
3. **Crash-safe.** A run killed mid-write must not leave a half-file that a
   later run mistakes for complete work.

`src/ingest/base.py` already retries individual requests, but retry only
covers failures *inside* a run. It cannot help a process that no longer
exists.

## Decision

**Resumption is keyed on the raw-layer file path, and lives in the venue
client, not in `APIClient`.**

Page N of series S in category C always lands at exactly
`data/raw/kalshi/<C>/<S>/page_NNNN.json`. The path is a pure function of the
work unit, so `path.exists()` is the entire completion check -- no manifest, no
checkpoint file, no state that can disagree with the data.

This lives in `KalshiClient` because only that layer knows what a unit of work
*is*. `APIClient.paginate()` sees an opaque cursor stream; it has no idea that
one page maps to one file, or that a series can be abandoned once it passes
the start of the window.

Three consequences follow from the design and are worth stating explicitly,
because each is a place the obvious implementation is wrong:

**Cached pages are still read, not skipped.** Page N carries the cursor for
page N+1. Skipping a cached page loses the thread and makes resumption
impossible from anywhere but page 1. So the loop reads cached pages from disk
and fetches only uncached ones -- and this is why the page walk is hand-rolled
in `_walk_series` rather than delegated to `APIClient.paginate()`, which
cannot express "read this one locally."

**Writes are atomic.** `_write_json` writes to `page_NNNN.json.tmp` and
`os.replace`s it into place. Rename is atomic on the same filesystem, so a
page file either does not exist or is complete. Without this, a kill during
`write_text` leaves truncated JSON that `path.exists()` reports as done and
the next run happily parses -- silent data loss, the worst failure mode
available.

**Series selection is cached too, and deliberately frozen.** The top-20-by-volume
choice per category is written to `<category>/_series.json` on first run and
reused thereafter. Re-ranking on every run would silently change the study
sample as volumes shift -- a resumed run would then be studying a slightly
different universe than the run it resumed. Deleting the file is the explicit
way to re-select.

Observability comes from `IngestStats`: `pages_fetched` and `pages_skipped`
make idempotency directly checkable. A fresh run reports `pages_skipped=0`; an
immediate re-run must report `pages_fetched=0` and the same total.

## Consequences

**Gained**

- A killed run costs only its in-flight page.
- Idempotency is verifiable in one line -- re-run and assert `pages_fetched=0`
  -- rather than asserted in prose.
- No state file to corrupt or to fall out of sync with the pages on disk.
- Development is cheap: iterating on the ingestion code does not re-hit the API
  for data already on disk, which also keeps us well clear of rate limits.

**Given up**

- **Cached pages are trusted, not verified.** A page written by an older code
  version, or hand-edited, is accepted as-is. Acceptable because raw is
  immutable by policy (ADR 001) and pages are byte-faithful responses -- there
  is no schema of ours for them to drift from. Re-fetching means deleting
  files, which is the intended, explicit gesture.
- **Re-fetch is all-or-nothing per series.** There is no "refresh pages older
  than X." Not needed: settled markets do not change.
- **Cursor pagination cannot be parallelised.** Page N+1 requires page N's
  cursor, so a series walk is inherently serial. Categories could be
  parallelised, but at 15 req/s the rate limit binds first, so this buys
  nothing.

## Alternatives considered

- **A manifest / checkpoint file listing completed pages.** Rejected: a second
  source of truth that can disagree with the filesystem, and it needs its own
  atomic-write and corruption story. The paths already are the manifest.
- **Resumption inside `APIClient`.** Rejected: the base client would need to
  know about raw-layer paths and per-venue stopping rules, which is exactly the
  venue-specific knowledge it exists to stay free of.
- **A content hash per page to detect drift.** Rejected as premature. Settled
  markets are immutable upstream, so there is nothing legitimate to detect.
- **One file per market instead of per page.** Rejected: it would mean parsing
  during ingestion to split the response, violating ADR 001's verbatim rule,
  and it discards the cursor the next page needs.
