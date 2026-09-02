# Data sources

Every endpoint this project reads, the gotchas each one carries, and what a
market record actually looks like. Verified against docs.kalshi.com and
exercised in `notebooks/00_api_scratch.ipynb`.

## Kalshi

**Base URL:** `https://external-api.kalshi.com/trade-api/v2` (config key
`kalshi_base_url`)

**Auth:** none. Every endpoint below is public read. Kalshi's authenticated
API-key scheme (RSA-PSS request signing) is required only for trading and for
portfolio endpoints, neither of which this project touches. Nothing here
needs a secret, which is why there is no `.env` in the repo.

### The live / historical split -- the main gotcha

Kalshi serves settled markets from **two different API surfaces**:

- `/markets` -- the live surface. Recent activity, including recently settled
  markets.
- `/historical/markets` -- older data, migrated off the live surface.

The boundary between them is **a moving timestamp**, published by
`/historical/cutoff`. Data ages across it as Kalshi migrates.

**Why this matters.** Querying only `/markets` and assuming it holds
everything silently truncates the study to recent history -- and truncates it
by *settlement date*, which is not random with respect to anything being
measured. Half the sample would be missing with no error raised and no gap in
the data to notice.

**How this project handles it.** The ingestion client reads only the
historical surface, so the cutoff is the newest settlement it can see, and
`config.yaml`'s `date_range.end` is pinned to the cutoff observed during the
2026-08-05 exploration. Every run re-fetches the cutoff and writes it to
`data/raw/kalshi/_cutoff.json` before touching anything else -- that file is
the record of what "as much history as available" meant on the day of that
pull. The boundary does move: exploration on 2026-08-05 saw
`2026-06-06`; the production run on 2026-08-29 saw `2026-06-29`.

Reading only the historical surface means the ~3 most recent weeks of settled
markets are out of scope by construction. That is a deliberate trade: one
uniform surface with a recorded boundary beats stitching two surfaces with
different schemas together and having to argue the seam is clean.

### Endpoints used

| Endpoint | Purpose | Key params | Notes |
|---|---|---|---|
| `GET /historical/cutoff` | The live/historical boundary | -- | Fetched first on every run; saved to `_cutoff.json`. Returns several timestamps; `market_settled_ts` is the one that bounds this study. |
| `GET /series` | Enumerate a category's series | `category`, `include_volume=true` | `include_volume` is required for ranking -- without it `volume_fp` is absent. Not paginated; Economics returns all 772 series in one response. |
| `GET /historical/markets` | Settled markets in a series | `series_ticker`, `limit` (max 200), `cursor` | Cursor-paginated. Returns markets in **descending settlement order**, which is what lets a series walk stop once it passes the start of the window. |

### Pagination

Cursor-based: each response carries `cursor`, which is passed as a param to
get the next page. An empty or absent `cursor` means exhausted.

Two consequences the ingestion layer is built around: pages must be walked in
order (page N holds page N+1's cursor, so a cached page is still *read*), and
a series walk cannot be parallelised. See `docs/adr/002-ingestion-idempotency.md`.

### Rate limits

The unauthenticated "Basic" tier is a token bucket refilling at **200
tokens/sec**, with a default cost of **10 tokens per request** -- about 20
req/s. `config.yaml` sets `rate_limit_per_second: 15` for headroom. Breaches
return **429** with a `Retry-After` header, which `src/ingest/base.py` honours
in preference to its own computed backoff.

### Series-level fields (`/series`)

| Field | Type | Notes |
|---|---|---|
| `ticker` | str | Series identifier, e.g. `KXFEDDECISION`. |
| `title` | str | Human label, e.g. "Fed meeting". |
| `volume_fp` | **str** | Total traded volume as a *decimal string* ("99935.00"). **Cast before comparing** -- sorting these lexicographically ranks "9917.00" above "10000000.00". |
| `frequency` | str enum | `custom`, `one_off`, `annual`, `monthly`, `weekly`, `daily`, `hourly`, `fifteen_min`. The **only** correct way to exclude sub-daily series -- matching the ticker string false-positives on Sports "1H" (First Half) markets. See `docs/journal.md`, 2026-08-05. |

### Market-level fields (`/historical/markets`)

The fields Phase 2 parsing depends on:

| Field | Type | Meaning |
|---|---|---|
| `ticker` | str | Unique contract id, e.g. `KXAAAGASD-26JUN28-3.920`. The dedup key. |
| `event_ticker` | str | Groups contracts resolving the same underlying event. |
| `title` / `yes_sub_title` | str | Question and the specific YES leg ("Above 3.920"). |
| `market_type` | str | `binary` for everything in scope. |
| `result` | str | Settlement: `yes`, `no`, or empty/`""` for voided. **This is the outcome.** |
| `status` | str | `finalized` for settled markets. |
| `settlement_ts` | str | RFC-3339 with a literal `Z`; `fromisoformat` needs the `Z` replaced with `+00:00`. |
| `open_time` / `close_time` / `expiration_time` | str | Same format. `close_time` bounds trading; `expiration_time` can be much later. |
| `last_price_dollars` | **str** | Last traded price, already in dollars (0-1), e.g. `"0.0100"`. |
| `previous_price_dollars` | str | Prior settlement-day reference price. |
| `yes_bid_dollars` / `yes_ask_dollars` | **str** | Best bid/ask on the YES leg. |
| `volume_fp` | **str** | Contracts traded over the market's life. |
| `open_interest_fp` | str | Open interest. |

**Every price and quantity is a decimal string, not a number.** Cast on read.

**A trap for the Phase 2 price decision:** on a settled market the book is
gone -- the record above shows `yes_bid_dollars: "0.0000"` and
`yes_ask_dollars: "1.0000"`, a maximally wide quote that is not a price at
all. A bid-ask mid computed from a settled snapshot returns 0.50 for every
contract regardless of what the market believed. `/historical/markets` gives
one post-settlement snapshot, not a time series, so `last_price_dollars` is
the only usable implied price available from this endpoint. That constraint,
not preference, drives design decision doc 003.

## Polymarket (stretch, not yet ingested)

**Base URL:** `https://gamma-api.polymarket.com` (config key
`polymarket_base_url`). Public read, no auth. `src/ingest/polymarket.py` is a
stub. It will subclass the same `APIClient` and write to
`data/raw/polymarket/` under the same layering rules, so adding it does not
change any layer downstream. Prices there are quoted 0-1 already, and the
YES/NO framing normalisation in `src/clean.py` is where the two venues are
reconciled.
