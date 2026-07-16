"""Tests for the shared ScrapeCreators HTTP client (POST_V1_PLAN §4.9)."""

from __future__ import annotations

import pytest

from sources.scrapecreators import client as sc_client
from sources.scrapecreators.client import (
    CreditTracker,
    ScrapeCreatorsAuthError,
    ScrapeCreatorsClient,
    ScrapeCreatorsError,
    ScrapeCreatorsHalted,
)


@pytest.fixture(autouse=True)
def reset_shared():
    """Every test starts with a clean shared client."""
    sc_client.reset_shared_client()
    yield
    sc_client.reset_shared_client()


# ---------------------------------------------------------------------------
# CreditTracker
# ---------------------------------------------------------------------------


def test_credit_tracker_spends_and_caps():
    t = CreditTracker(cap=3)
    assert t.spent == 0
    assert not t.would_exceed()
    t.spend()
    t.spend()
    assert t.spent == 2
    assert not t.would_exceed()
    t.spend()
    assert t.would_exceed()


def test_credit_tracker_halt_flag():
    t = CreditTracker(cap=100)
    t.halt("test reason")
    assert t.halted is True
    assert t.halt_reason == "test reason"


# ---------------------------------------------------------------------------
# ScrapeCreatorsClient.from_env
# ---------------------------------------------------------------------------


def test_from_env_requires_key_when_not_mock(monkeypatch):
    monkeypatch.delenv("SCRAPECREATORS_API_KEY", raising=False)
    monkeypatch.delenv("SCRAPECREATORS_MOCK", raising=False)
    with pytest.raises(ScrapeCreatorsAuthError, match="SCRAPECREATORS_API_KEY"):
        ScrapeCreatorsClient.from_env()


def test_from_env_accepts_mock_flag_without_key(monkeypatch):
    monkeypatch.delenv("SCRAPECREATORS_API_KEY", raising=False)
    monkeypatch.setenv("SCRAPECREATORS_MOCK", "1")
    c = ScrapeCreatorsClient.from_env()
    assert c.mock is True


def test_from_env_reads_cap(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", "test_key")
    c = ScrapeCreatorsClient.from_env(cap=25)
    assert c.tracker.cap == 25


# ---------------------------------------------------------------------------
# get() — mock mode
# ---------------------------------------------------------------------------


def test_get_uses_mock_when_configured(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_MOCK", "1")
    c = ScrapeCreatorsClient.from_env(cap=100)
    payload = c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})
    assert "posts" in payload
    assert len(payload["posts"]) >= 1
    assert c.tracker.spent == 1


def test_get_returns_empty_shape_for_missing_fixture(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_MOCK", "1")
    c = ScrapeCreatorsClient.from_env(cap=100)
    payload = c.get("/v1/reddit/subreddit", {"subreddit": "never_matches_a_fixture_xyz"})
    assert payload == {"posts": []}


# ---------------------------------------------------------------------------
# get() — credit cap + halt
# ---------------------------------------------------------------------------


def test_get_halts_when_cap_exhausted(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_MOCK", "1")
    c = ScrapeCreatorsClient.from_env(cap=2)
    c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})
    c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})
    with pytest.raises(ScrapeCreatorsHalted, match="cap"):
        c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})
    assert c.tracker.halted is True


def test_further_calls_after_halt_raise(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_MOCK", "1")
    c = ScrapeCreatorsClient.from_env(cap=1)
    c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})
    # Cap = 1, second call halts:
    with pytest.raises(ScrapeCreatorsHalted):
        c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})
    # Now the tracker is halted; any subsequent call raises immediately
    with pytest.raises(ScrapeCreatorsHalted, match="halted earlier"):
        c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})


# ---------------------------------------------------------------------------
# get() — HTTP status handling (live path with a stubbed httpx.Client)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class _FakeHttpxClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self._response

    def close(self):
        pass


def test_402_is_hard_halt(monkeypatch):
    """402 halts the tracker AND raises ScrapeCreatorsHalted."""
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", "test_key")
    monkeypatch.delenv("SCRAPECREATORS_MOCK", raising=False)
    c = ScrapeCreatorsClient.from_env(cap=100)
    c._client = _FakeHttpxClient(_FakeResponse(402, text="payment required"))

    with pytest.raises(ScrapeCreatorsHalted, match="402"):
        c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})
    assert c.tracker.halted is True


def test_401_raises_auth_error(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", "test_key")
    monkeypatch.delenv("SCRAPECREATORS_MOCK", raising=False)
    c = ScrapeCreatorsClient.from_env(cap=100)
    c._client = _FakeHttpxClient(_FakeResponse(401, text="unauthorized"))

    with pytest.raises(ScrapeCreatorsAuthError):
        c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})
    assert c.tracker.halted is False


def test_5xx_raises_generic_error(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", "test_key")
    monkeypatch.delenv("SCRAPECREATORS_MOCK", raising=False)
    c = ScrapeCreatorsClient.from_env(cap=100)
    c._client = _FakeHttpxClient(_FakeResponse(503, text="upstream down"))

    with pytest.raises(ScrapeCreatorsError, match="503"):
        c.get("/v1/reddit/subreddit", {"subreddit": "Windows11"})


def test_success_returns_json_body(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", "test_key")
    monkeypatch.delenv("SCRAPECREATORS_MOCK", raising=False)
    c = ScrapeCreatorsClient.from_env(cap=100)
    c._client = _FakeHttpxClient(_FakeResponse(200, json_data={"posts": [{"id": "abc"}]}))

    result = c.get("/v1/reddit/subreddit", {"subreddit": "test"})
    assert result == {"posts": [{"id": "abc"}]}
    assert c.tracker.spent == 1


def test_success_sends_api_key_header(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_API_KEY", "my_secret_key")
    monkeypatch.delenv("SCRAPECREATORS_MOCK", raising=False)
    c = ScrapeCreatorsClient.from_env(cap=100)
    fake = _FakeHttpxClient(_FakeResponse(200, json_data={}))
    c._client = fake

    c.get("/v1/reddit/subreddit", {"subreddit": "test"})
    assert fake.calls[0]["headers"]["x-api-key"] == "my_secret_key"


# ---------------------------------------------------------------------------
# Shared client factory
# ---------------------------------------------------------------------------


def test_shared_client_is_singleton_per_run(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_MOCK", "1")
    c1 = sc_client.get_shared_client(cap=50)
    c2 = sc_client.get_shared_client(cap=100)
    assert c1 is c2
    assert c1.tracker.cap == 50  # cap on second call is ignored


def test_reset_shared_client_creates_fresh(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_MOCK", "1")
    c1 = sc_client.get_shared_client(cap=50)
    sc_client.reset_shared_client(cap=25)
    c2 = sc_client.get_shared_client()
    assert c1 is not c2
    assert c2.tracker.cap == 25
