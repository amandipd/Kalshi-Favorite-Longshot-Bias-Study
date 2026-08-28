"""Tests for src/ingest/base.py.

Every test drives the client through an `httpx.MockTransport` rather than the
network, so the retry and pagination behaviour is exercised deterministically:
no live API, no flakiness, and no waiting out a real backoff schedule (the
sleeps are stubbed and recorded instead).
"""

from __future__ import annotations

import httpx
import pytest

from src.config import RetryConfig
from src.ingest.base import APIClient, RetryableHTTPError, _RateLimiter

FAST_RETRY = RetryConfig(
    max_attempts=4,
    initial_backoff_seconds=1.0,
    max_backoff_seconds=60.0,
    jitter_seconds=0.0,  # deterministic waits; jitter is asserted separately
    timeout_seconds=5.0,
)


@pytest.fixture
def recorded_sleeps() -> list[float]:
    """Collects the retry waits that `make_client` would otherwise perform."""
    return []


def make_client(
    handler,
    retry: RetryConfig = FAST_RETRY,
    rate: float = 1000.0,
    recorded_sleeps: list[float] | None = None,
) -> APIClient:
    """An APIClient wired to a mock transport, with retry waits recorded rather
    than slept. Note we swap the client's own sleep seam instead of patching
    `time.sleep` globally -- the rate limiter also sleeps, and a global patch
    would silently mix its pacing waits into the retry schedule under test.
    """
    client = APIClient(
        "https://example.test",
        rate_limit_per_second=rate,
        retry=retry,
        transport=httpx.MockTransport(handler),
    )
    if recorded_sleeps is not None:
        client._sleep = recorded_sleeps.append
    return client


# -- retry behaviour -------------------------------------------------------


def test_retries_429_then_succeeds(recorded_sleeps):
    """A rate-limited request is retried and its eventual success returned."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    with make_client(handler, recorded_sleeps=recorded_sleeps) as client:
        assert client.get("/thing") == {"ok": True}

    assert calls["n"] == 3
    # Exponential with jitter=0: initial * 2^0, then initial * 2^1.
    assert recorded_sleeps == [1.0, 2.0]


def test_retry_after_header_overrides_computed_backoff(recorded_sleeps):
    """The server's Retry-After wins over our exponential schedule."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={})

    with make_client(handler, recorded_sleeps=recorded_sleeps) as client:
        client.get("/thing")

    assert recorded_sleeps == [7.0]  # not the 1.0s we would have computed


def test_retry_after_is_capped_at_max_backoff(recorded_sleeps):
    """An absurd Retry-After cannot park the run for hours."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "99999"})

    with make_client(handler, recorded_sleeps=recorded_sleeps) as client:
        with pytest.raises(RetryableHTTPError):
            client.get("/thing")

    assert all(s == FAST_RETRY.max_backoff_seconds for s in recorded_sleeps)


def test_unparseable_retry_after_falls_back_to_exponential(recorded_sleeps):
    """An HTTP-date Retry-After is treated as absent, not guessed at."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        return httpx.Response(200, json={})

    with make_client(handler, recorded_sleeps=recorded_sleeps) as client:
        client.get("/thing")

    assert recorded_sleeps == [1.0]


def test_gives_up_after_max_attempts_and_reraises(recorded_sleeps):
    """A persistently failing endpoint fails loudly rather than looping forever."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with make_client(handler, recorded_sleeps=recorded_sleeps) as client:
        with pytest.raises(RetryableHTTPError) as exc_info:
            client.get("/thing")

    assert calls["n"] == FAST_RETRY.max_attempts  # attempts, not retries
    assert exc_info.value.status_code == 500


def test_client_error_is_not_retried(recorded_sleeps):
    """A 404 is our bug; retrying it only burns quota."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with make_client(handler, recorded_sleeps=recorded_sleeps) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get("/nope")

    assert calls["n"] == 1
    assert recorded_sleeps == []


def test_transport_errors_are_retried(recorded_sleeps):
    """Connection resets and read timeouts are transient too."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"ok": True})

    with make_client(handler, recorded_sleeps=recorded_sleeps) as client:
        assert client.get("/thing") == {"ok": True}

    assert calls["n"] == 2


def test_jitter_is_applied_and_bounded(recorded_sleeps):
    """Backoff carries random noise so concurrent retries do not re-collide."""
    jittered = FAST_RETRY.model_copy(update={"jitter_seconds": 2.0, "max_attempts": 8})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with make_client(handler, retry=jittered, recorded_sleeps=recorded_sleeps) as client:
        with pytest.raises(RetryableHTTPError):
            client.get("/thing")

    # Each wait sits in [base, base + jitter) for its own exponential base.
    for i, slept in enumerate(recorded_sleeps):
        base = min(jittered.initial_backoff_seconds * 2**i, jittered.max_backoff_seconds)
        assert base <= slept < base + jittered.jitter_seconds

    # The whole point of jitter: the waits are not all identical.
    assert len(set(recorded_sleeps)) > 1


# -- pagination ------------------------------------------------------------


def build_paged_handler(pages: list[dict], seen_cursors: list[str | None]):
    """Serve `pages` in order, keyed off the cursor the client sends back."""

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        seen_cursors.append(cursor)
        index = 0 if cursor is None else int(cursor)
        return httpx.Response(200, json=pages[index])

    return handler


def test_paginate_follows_cursors_until_exhausted():
    """Walks every page and stops when the server returns an empty cursor."""
    pages = [
        {"markets": [{"ticker": "A"}], "cursor": "1"},
        {"markets": [{"ticker": "B"}], "cursor": "2"},
        {"markets": [{"ticker": "C"}], "cursor": ""},  # empty = last page
    ]
    seen_cursors: list[str | None] = []

    with make_client(build_paged_handler(pages, seen_cursors)) as client:
        collected = list(client.paginate("/markets", {"limit": 1}))

    assert [n for n, _ in collected] == [1, 2, 3]
    assert [p["markets"][0]["ticker"] for _, p in collected] == ["A", "B", "C"]
    # First request carries no cursor; each later one echoes the previous token.
    assert seen_cursors == [None, "1", "2"]


def test_paginate_stops_when_cursor_key_absent():
    """A response with no cursor field at all also terminates the walk."""
    pages = [{"markets": [{"ticker": "A"}]}]
    with make_client(build_paged_handler(pages, [])) as client:
        assert len(list(client.paginate("/markets"))) == 1


def test_paginate_respects_max_pages():
    """max_pages truncates the walk even when more pages remain."""
    pages = [{"markets": [], "cursor": str(i + 1)} for i in range(10)]
    with make_client(build_paged_handler(pages, [])) as client:
        assert len(list(client.paginate("/markets", max_pages=3))) == 3


def test_paginate_does_not_mutate_caller_params():
    """The caller's params dict is reused across pages; the cursor must not leak
    into it, or a second call would start mid-stream."""
    pages = [{"cursor": "1"}, {"cursor": ""}]
    params = {"limit": 200}

    with make_client(build_paged_handler(pages, [])) as client:
        list(client.paginate("/markets", params))

    assert params == {"limit": 200}


def test_paginate_is_lazy():
    """Pages are yielded as they arrive, so a caller writing each to disk leaves
    partial progress behind if the run is interrupted."""
    pages = [{"cursor": "1"}, {"cursor": "2"}, {"cursor": ""}]
    requests_made: list[str | None] = []

    with make_client(build_paged_handler(pages, requests_made)) as client:
        pager = client.paginate("/markets")
        next(pager)
        assert len(requests_made) == 1  # not 3


# -- pacing ----------------------------------------------------------------


def test_rate_limiter_spaces_calls():
    """Calls are smoothed to >= 1/rate apart rather than bursting."""
    times: list[float] = []
    limiter = _RateLimiter(rate_per_second=50.0)

    import time

    for _ in range(4):
        limiter.acquire()
        times.append(time.monotonic())

    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(gap >= 0.015 for gap in gaps), gaps  # 1/50 = 0.02s, minus timer slop
