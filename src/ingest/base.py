"""Shared retry/pagination logic for venue API clients.

`APIClient` is the venue-agnostic half of the ingestion layer: it knows how to
make a request that survives rate limits and transient server errors, how to
pace itself so it does not cause them in the first place, and how to walk a
cursor-paginated endpoint. It knows nothing about Kalshi or Polymarket -- what
to fetch, and what to do with the bytes, belongs to the subclasses.

Design notes:

* **Retry, not resume.** This class retries a single failing request. Skipping
  work already completed on an earlier run (idempotency) happens one layer up,
  in the venue client, because only that layer knows what a "unit of work" is
  and where its output file lives.
* **Raw pages, not parsed objects.** `paginate()` yields the decoded JSON page
  exactly as the server sent it. Parsing into `Contract` happens later, in
  src/clean.py, reading from the immutable raw files -- so a parsing bug is
  fixable without re-hitting the API. See docs/adr/001-storage-layers.md.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt
from tenacity.nap import sleep as tenacity_sleep

from src.config import RetryConfig

logger = logging.getLogger(__name__)

# 429 (rate limited) and 5xx (server-side) are transient by definition: the same
# request may well succeed shortly. 4xx other than 429 are our fault -- a bad
# param, a wrong path -- and retrying just burns quota against a request that
# will never work, so those raise immediately.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class RetryableHTTPError(Exception):
    """A response worth trying again, carrying any server-supplied wait hint.

    Attributes:
        status_code: The HTTP status that triggered the retry.
        retry_after: Seconds the server asked us to wait (from the Retry-After
            header), or None if it did not say. When present this overrides our
            computed backoff -- the server knows its own quota window better
            than our exponential schedule does.
    """

    def __init__(self, status_code: int, retry_after: float | None, url: str) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"retryable HTTP {status_code} from {url} (retry_after={retry_after})")


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Read Retry-After as a delay in seconds.

    The header is allowed to be either a number of seconds or an HTTP date. We
    only handle the numeric form -- Kalshi sends seconds -- and treat anything
    else as absent rather than guessing, since a wrong parse here means either
    hammering the server or sleeping for hours.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("event=retry_after_unparsed value=%r", raw)
        return None


class _RateLimiter:
    """Minimum-interval pacer: blocks so calls are spaced >= 1/rate apart.

    Deliberately not a token bucket. A bucket lets a burst through at full
    speed, which is exactly what trips a quota at the start of a long ingestion
    run; smoothing every call to a fixed interval trades a little throughput
    for never being the reason a 429 happened. Thread-safe so a future
    concurrent fetcher can share one client.
    """

    def __init__(self, rate_per_second: float) -> None:
        self._min_interval = 1.0 / rate_per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class APIClient:
    """httpx-backed client with retry/backoff, pacing, and cursor pagination.

    Usable as a context manager; call `close()` otherwise.

        with APIClient(base_url, rate_limit_per_second, retry_cfg) as client:
            cutoff = client.get("/historical/cutoff")
    """

    def __init__(
        self,
        base_url: str,
        rate_limit_per_second: float,
        retry: RetryConfig,
        *,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """
        Args:
            base_url: Venue API root; endpoints are resolved relative to it.
            rate_limit_per_second: Ceiling on outbound call rate.
            retry: Backoff policy, from config.yaml.
            headers: Sent on every request (auth, user-agent).
            transport: Override httpx's network transport. Exists so tests can
                inject an `httpx.MockTransport` and exercise the retry and
                pagination logic deterministically -- no network, no real
                sleeping on a server's schedule. Production leaves it None.
        """
        self.base_url = base_url
        self.retry_config = retry
        self._limiter = _RateLimiter(rate_limit_per_second)
        # How retry waits are actually performed. Swapped out in tests so a
        # backoff schedule can be asserted without spending it in real time.
        self._sleep = tenacity_sleep
        self._client = httpx.Client(
            base_url=base_url,
            timeout=retry.timeout_seconds,
            headers=headers or {},
            transport=transport,
        )

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "APIClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- retry policy ------------------------------------------------------

    def _wait(self, retry_state: RetryCallState) -> float:
        """Seconds to sleep before the next attempt.

        Prefers the server's Retry-After when it gave one; otherwise
        exponential backoff (initial * 2^n) capped at max_backoff_seconds.
        Either way a uniform [0, jitter_seconds) is added.

        Jitter is the point of this function. Without it, every request that
        got rate-limited in the same window computes an identical sleep, wakes
        at the same instant, and collides again -- a self-inflicted thundering
        herd that can keep a run pinned against the quota wall. Random noise
        spreads the wake-ups out so the queue drains.
        """
        cfg = self.retry_config
        exc = retry_state.outcome.exception() if retry_state.outcome else None

        if isinstance(exc, RetryableHTTPError) and exc.retry_after is not None:
            base = min(exc.retry_after, cfg.max_backoff_seconds)
        else:
            # attempt_number is 1 on the first failure, so 2^0 = the initial wait.
            exponential = cfg.initial_backoff_seconds * (2 ** (retry_state.attempt_number - 1))
            base = min(exponential, cfg.max_backoff_seconds)

        return base + random.uniform(0.0, cfg.jitter_seconds)

    def _log_retry(self, retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "event=retry attempt=%d/%d sleep=%.2fs reason=%s",
            retry_state.attempt_number,
            self.retry_config.max_attempts,
            retry_state.next_action.sleep if retry_state.next_action else 0.0,
            exc,
        )

    # -- requests ----------------------------------------------------------

    def _send(self, method: str, endpoint: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """One attempt. Raises RetryableHTTPError on 429/5xx so tenacity retries."""
        self._limiter.acquire()
        started = time.monotonic()
        response = self._client.request(method, endpoint, params=params)
        elapsed_ms = (time.monotonic() - started) * 1000

        logger.info(
            "event=api_call method=%s endpoint=%s status=%d elapsed_ms=%.0f",
            method,
            endpoint,
            response.status_code,
            elapsed_ms,
        )

        if response.status_code in RETRYABLE_STATUS_CODES:
            raise RetryableHTTPError(
                response.status_code, _parse_retry_after(response), str(response.url)
            )

        # Anything else non-2xx is a client error waiting will not fix.
        response.raise_for_status()
        return response.json()

    def request(
        self, method: str, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make a request, retrying transient failures per the configured policy.

        Retries on 429/5xx and on transport-level errors (connection resets,
        read timeouts) -- both are conditions where the identical request may
        succeed moments later. Gives up after `retry.max_attempts` total tries
        and re-raises the final exception, so a genuinely broken run fails
        loudly instead of hanging forever.
        """
        retrying = Retrying(
            stop=stop_after_attempt(self.retry_config.max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type((RetryableHTTPError, httpx.TransportError)),
            before_sleep=self._log_retry,
            sleep=self._sleep,
            reraise=True,
        )
        return retrying(self._send, method, endpoint, params)

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET `endpoint`, returning the decoded JSON body."""
        return self.request("GET", endpoint, params)

    # -- pagination --------------------------------------------------------

    def paginate(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        cursor_param: str = "cursor",
        cursor_key: str = "cursor",
        max_pages: int | None = None,
    ) -> Iterator[tuple[int, dict[str, Any]]]:
        """Walk a cursor-paginated endpoint, yielding (page_number, page_json).

        The cursor contract, confirmed empirically against Kalshi in
        notebooks/00_api_scratch.ipynb: each response carries an opaque token;
        passing it back as the next request's cursor returns the following,
        non-overlapping page. The walk ends when the server returns an empty or
        absent token.

        Pages are yielded rather than accumulated so the caller can write each
        one to disk as it arrives. That is what makes ingestion resumable: an
        interrupted run leaves every page it already fetched on disk.

        Args:
            endpoint: Path relative to base_url.
            params: Query params sent on every page (the cursor is added here).
            cursor_param: Request param carrying the cursor.
            cursor_key: Response key holding the next cursor.
            max_pages: Stop after this many pages. For smoke tests only -- a
                real ingestion run leaves it None and reads to the end.

        Yields:
            (page_number, page_json), page_number starting at 1.
        """
        base_params = dict(params or {})
        cursor: str | None = None
        page_number = 0

        while max_pages is None or page_number < max_pages:
            page_params = dict(base_params)
            if cursor:
                page_params[cursor_param] = cursor

            page = self.get(endpoint, page_params)
            page_number += 1
            yield page_number, page

            cursor = page.get(cursor_key) or None
            if cursor is None:
                logger.info(
                    "event=pagination_complete endpoint=%s pages=%d", endpoint, page_number
                )
                return

        logger.info(
            "event=pagination_truncated endpoint=%s pages=%d reason=max_pages",
            endpoint,
            page_number,
        )
