"""Tests for pipeline.http.request_with_retry (ADR-0022).

One transient 429/503 from a source used to drop the entire fetch call —
weekly digests silently missed items without any error surfaced. The helper
retries transient statuses/exceptions with exponential backoff, honors
Retry-After, and bubbles non-retryable errors immediately so bad auth /
bad URL / bad params still fail loud and fast.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from pipeline import http as retry_http


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}

    def read(self) -> None:  # matches httpx.Response.read signature
        return None


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_http, "_flag_enabled", lambda: True)
    # Kill sleep so tests don't spend real seconds backing off.
    monkeypatch.setattr(retry_http.time, "sleep", lambda _s: None)


def test_flag_off_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_http, "_flag_enabled", lambda: False)
    calls: list[int] = []

    def call() -> Any:
        calls.append(1)
        return _FakeResponse(503)

    resp = retry_http.request_with_retry(call, source_id="test")
    assert resp.status_code == 503
    assert len(calls) == 1  # no retries when flag is off


def test_retries_on_503_then_succeeds(flag_on: None) -> None:
    responses = iter([_FakeResponse(503), _FakeResponse(503), _FakeResponse(200)])
    calls: list[int] = []

    def call() -> Any:
        calls.append(1)
        return next(responses)

    resp = retry_http.request_with_retry(call, source_id="test")
    assert resp.status_code == 200
    assert len(calls) == 3


def test_gives_up_after_max_attempts(flag_on: None) -> None:
    calls: list[int] = []

    def call() -> Any:
        calls.append(1)
        return _FakeResponse(503)

    resp = retry_http.request_with_retry(call, source_id="test")
    assert resp.status_code == 503
    assert len(calls) == 3  # _MAX_ATTEMPTS


def test_non_retryable_status_returns_immediately(flag_on: None) -> None:
    calls: list[int] = []

    def call() -> Any:
        calls.append(1)
        return _FakeResponse(401)  # auth error — retrying wastes time

    resp = retry_http.request_with_retry(call, source_id="test")
    assert resp.status_code == 401
    assert len(calls) == 1


def test_retries_on_transport_error_then_succeeds(flag_on: None) -> None:
    responses: list[Any] = [
        httpx.ConnectError("boom"),
        _FakeResponse(200),
    ]
    calls: list[int] = []

    def call() -> Any:
        calls.append(1)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    resp = retry_http.request_with_retry(call, source_id="test")
    assert resp.status_code == 200
    assert len(calls) == 2


def test_transport_error_bubbles_after_max_attempts(flag_on: None) -> None:
    def call() -> Any:
        raise httpx.ConnectError("boom")

    with pytest.raises(httpx.ConnectError):
        retry_http.request_with_retry(call, source_id="test")


def test_honors_retry_after_seconds(
    flag_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(retry_http.time, "sleep", lambda s: sleeps.append(s))

    responses = iter([
        _FakeResponse(429, {"Retry-After": "7"}),
        _FakeResponse(200),
    ])

    def call() -> Any:
        return next(responses)

    resp = retry_http.request_with_retry(call, source_id="test")
    assert resp.status_code == 200
    # The 7-second server hint should be honored exactly (backoff would
    # have been a jittered fraction of 1s otherwise).
    assert sleeps == [7.0]


def test_retry_after_invalid_falls_back_to_jitter(
    flag_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(retry_http.time, "sleep", lambda s: sleeps.append(s))
    # Deterministic jitter for the assertion below.
    monkeypatch.setattr(retry_http.random, "uniform", lambda a, b: b)

    responses = iter([
        _FakeResponse(503, {"Retry-After": "nonsense"}),
        _FakeResponse(200),
    ])

    def call() -> Any:
        return next(responses)

    retry_http.request_with_retry(call, source_id="test")
    assert sleeps == [1.0]  # base delay for attempt 1
