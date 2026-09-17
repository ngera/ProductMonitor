"""Microsoft Tech Community feed-URL discovery (ADR-0030)."""

from __future__ import annotations

from sources.microsoft_community import (
    MicrosoftCommunitySource,
    _normalize_feed_url,
)


def test_normalize_feed_url_bare_category():
    url = _normalize_feed_url("Windows")
    assert "category.id=Windows" in url
    assert url.startswith("https://techcommunity.microsoft.com/")


def test_normalize_feed_url_full_url():
    raw = (
        "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/"
        "Category?category.id=Azure"
    )
    assert _normalize_feed_url(raw) == raw


def test_discover_streams_uses_llm(monkeypatch):
    monkeypatch.setattr(
        "pipeline.stream_suggestions.suggest_stream_identifiers",
        lambda **kw: [
            {"value": "Windows", "rationale": "Windows forum"},
            {"value": (
                "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/"
                "Category?category.id=Azure"
            ), "rationale": "Azure"},
            {"value": "https://evil.example/rss", "rationale": "drop"},
        ],
    )
    src = MicrosoftCommunitySource()
    cands = src.discover_streams({"display": "Azure SQL"}, max_candidates=8)
    urls = [c.stream_config["feed_url"] for c in cands]
    assert any("category.id=Windows" in u for u in urls)
    assert any("category.id=Azure" in u for u in urls)
    assert all("evil.example" not in u for u in urls)


def test_discover_streams_keyword_fallback(monkeypatch):
    monkeypatch.setattr(
        "pipeline.stream_suggestions.suggest_stream_identifiers",
        lambda **kw: [],
    )
    src = MicrosoftCommunitySource()
    cands = src.discover_streams(
        {"display": "Microsoft Teams", "description": "collaboration app"},
        max_candidates=5,
    )
    urls = [c.stream_config["feed_url"] for c in cands]
    assert any("category.id=Teams" in u for u in urls)
