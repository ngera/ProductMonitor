"""Tests for webui/source_health.py (POST_V1_PLAN §4.2)."""

from __future__ import annotations

import pytest

from sources import registry
from webui.source_health import compute_health, compute_readiness


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch):
    """Ensure discovery uses a fresh registry per test."""
    monkeypatch.delenv("TRUST_PLUGINS_DIR", raising=False)
    registry.reset_registry()
    yield
    registry.reset_registry()


def test_readiness_ready_for_no_auth_plugin(monkeypatch):
    """HN needs no auth — readiness must be 'ready' regardless of env."""
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    result = compute_readiness([
        {"id": "hn-1", "type": "hn", "streams": [{"name": "hn"}]},
    ])
    assert len(result) == 1
    assert result[0].status == "ready"
    assert result[0].missing_env_vars == []


def test_readiness_missing_for_reddit_with_no_env(monkeypatch):
    """Reddit needs CLIENT_ID + CLIENT_SECRET + USER_AGENT. Missing = 'missing'."""
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("REDDIT_USER_AGENT", raising=False)

    # Patch out .env reader so the env is truly empty
    import webui.source_health as sh
    monkeypatch.setattr(sh, "_read_env_snapshot", lambda: {})

    result = compute_readiness([
        {"id": "reddit-1", "type": "reddit", "streams": [{"subreddit": "Windows11"}]},
    ])
    assert len(result) == 1
    # REDDIT_USER_AGENT has a default, so it's not required; CLIENT_ID and CLIENT_SECRET are secrets
    assert result[0].status in {"missing", "partial"}
    assert "REDDIT_CLIENT_ID" in result[0].missing_env_vars
    assert "REDDIT_CLIENT_SECRET" in result[0].missing_env_vars
    assert result[0].fix_url == "/connections/reddit"


def test_readiness_partial_when_some_env_set(monkeypatch):
    """Setting some but not all creds → 'partial'."""
    import webui.source_health as sh
    monkeypatch.setattr(sh, "_read_env_snapshot", lambda: {
        "REDDIT_CLIENT_ID": "abc",
        # REDDIT_CLIENT_SECRET missing
    })

    result = compute_readiness([
        {"id": "reddit-1", "type": "reddit", "streams": [{"subreddit": "Windows11"}]},
    ])
    assert result[0].status == "partial"
    assert "REDDIT_CLIENT_SECRET" in result[0].missing_env_vars
    assert "REDDIT_CLIENT_ID" not in result[0].missing_env_vars


def test_readiness_unknown_plugin():
    """A source instance referencing a non-registered plugin should report
    clearly, not crash."""
    result = compute_readiness([
        {"id": "fake-1", "type": "does_not_exist", "streams": [{}]},
    ])
    assert len(result) == 1
    assert result[0].status == "unknown_plugin"


def test_readiness_paused_stream_count():
    """Paused streams counted separately so the UI can show 'N of M paused'."""
    result = compute_readiness([
        {
            "id": "hn-1", "type": "hn",
            "streams": [
                {"name": "a"},
                {"name": "b", "paused": True},
                {"name": "c", "paused": True},
            ],
        },
    ])
    assert result[0].n_streams == 3
    assert result[0].n_streams_paused == 2


def test_health_ok_when_no_errors():
    """Clean run → status='ok'."""
    result = compute_health(
        run_json={"errors": [], "completeness": {}},
        product_sources=[
            {"id": "hn-1", "type": "hn", "streams": [{"name": "hn-x"}]},
        ],
    )
    assert result[0].status == "ok"
    assert result[0].streams_failed == 0
    assert result[0].error_summary == ""


def test_health_error_when_init_failed():
    """`source reddit-1 init failed: 'REDDIT_CLIENT_SECRET'` → error state
    attributed to reddit-1."""
    result = compute_health(
        run_json={
            "errors": ["source reddit-1 init failed: 'REDDIT_CLIENT_SECRET'"],
            "completeness": {},
        },
        product_sources=[
            {"id": "reddit-1", "type": "reddit", "streams": [{"subreddit": "Windows11"}]},
            {"id": "hn-1", "type": "hn", "streams": [{"name": "hn"}]},
        ],
    )
    reddit = next(h for h in result if h.instance_id == "reddit-1")
    hn = next(h for h in result if h.instance_id == "hn-1")
    assert reddit.status == "error"
    assert "REDDIT_CLIENT_SECRET" in reddit.error_summary
    assert reddit.streams_failed == 1
    assert hn.status == "ok"


def test_health_partial_on_ceiling_hits():
    """Ceiling hits (paging limits reached) → status='partial'."""
    result = compute_health(
        run_json={
            "errors": [],
            "completeness": {
                "ceiling_hits": [
                    ["hn-windows-media-platform:sound", 60.0],
                ],
            },
        },
        product_sources=[
            {
                "id": "hn-1", "type": "hn",
                "streams": [{"name": "hn-windows-media-platform"}],
            },
        ],
    )
    assert result[0].status == "partial"
    assert result[0].ceiling_hits == 1
