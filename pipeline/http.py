"""Shared retry-with-backoff for source HTTP calls (ADR-0022).

Wraps httpx requests with exponential backoff on transient failures.
LLM providers already retry inside the OpenAI SDK (see LLMClient's
`max_retries=`), so this module targets the source fetch layer where
one transient 429/503 currently drops a whole stream.

Adopt by wrapping the request callable:

    from pipeline import http as ph
    resp = ph.request_with_retry(
        lambda: self._client.get("/search_by_date", params=params),
        source_id="hn",
    )
    resp.raise_for_status()

Retries: 3 attempts, exponential backoff starting at 1s (1s, 2s, 4s;
capped at 30s). Retryable statuses: 408, 425, 429, 500, 502, 503, 504.
Retryable exceptions: httpx.TransportError, httpx.TimeoutException.
Honors `Retry-After` on 429 / 5xx responses.

Gated by the `http_retry_enabled` feature flag (default off, per
ADR-0006). When disabled, the callable is invoked once with no
retry wrapper. Callers that MUST NOT retry (e.g. paid APIs where each
call costs money — see sources/scrapecreators/client.py) should simply
not call this module.
"""

from __future__ import annotations

import logging
import random
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import httpx

_log = logging.getLogger(__name__)

_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 1.0
_MAX_DELAY_SECONDS = 30.0


def _flag_enabled() -> bool:
    # Late import to avoid a config load at module import time (some CLI
    # entry points import pipeline.* before app config is materialized).
    try:
        from pipeline.features import enabled
        return enabled("http_retry_enabled")
    except Exception:
        return False


def _parse_retry_after(header_value: str | None) -> float | None:
    if not header_value:
        return None
    header_value = header_value.strip()
    try:
        return max(0.0, float(header_value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header_value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _backoff_seconds(attempt: int) -> float:
    # attempt is 1-indexed. Full jitter to avoid thundering herd across
    # concurrent streams retrying the same host.
    base = min(_MAX_DELAY_SECONDS, _BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    return random.uniform(0.0, base)


def request_with_retry(
    call: Callable[[], httpx.Response],
    *,
    source_id: str = "",
    retryable_status: Iterable[int] = _RETRYABLE_STATUS,
    max_attempts: int = _MAX_ATTEMPTS,
) -> httpx.Response:
    """Invoke `call`; retry on transient failures with backoff.

    Returns the httpx.Response on success (which may still be a non-2xx
    non-retryable status — the caller decides whether to raise_for_status).
    On the final failed attempt, raises the underlying exception or returns
    the last response, matching what the un-retried call would have done.

    Non-retryable errors (4xx that isn't a rate-limit status, programming
    errors) bubble immediately — retrying a 401 wastes time and hides the
    real problem.
    """
    if not _flag_enabled():
        return call()

    retryable = frozenset(retryable_status)
    last_exc: BaseException | None = None
    last_resp: httpx.Response | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = call()
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt == max_attempts:
                raise
            delay = _backoff_seconds(attempt)
            _log.warning(
                "http_retry source=%s attempt=%d/%d error=%s delay=%.2fs",
                source_id, attempt, max_attempts, type(e).__name__, delay,
            )
            time.sleep(delay)
            continue

        if resp.status_code not in retryable:
            return resp

        last_resp = resp
        if attempt == max_attempts:
            return resp

        # Honor Retry-After when the server told us how long to wait.
        server_hint = _parse_retry_after(resp.headers.get("Retry-After"))
        delay = server_hint if server_hint is not None else _backoff_seconds(attempt)
        _log.warning(
            "http_retry source=%s attempt=%d/%d status=%d delay=%.2fs%s",
            source_id, attempt, max_attempts, resp.status_code, delay,
            " (Retry-After)" if server_hint is not None else "",
        )
        # Drain the body so httpx can reuse the connection.
        try:
            resp.read()
        except Exception:
            pass
        time.sleep(delay)

    # Unreachable: loop either returns or raises. Kept for type-checkers.
    if last_resp is not None:
        return last_resp
    assert last_exc is not None
    raise last_exc
