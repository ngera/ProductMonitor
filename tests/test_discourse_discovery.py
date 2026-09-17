"""Discourse host discovery (ADR-0030) + hostname normalization."""

from __future__ import annotations

from sources.discourse import DiscourseSource, normalize_host


def test_normalize_host_strips_scheme_and_path():
    assert normalize_host("https://forum.cursor.com/t/123") == "forum.cursor.com"
    assert normalize_host("Forum.Cursor.Com/") == "forum.cursor.com"


def test_discover_streams_uses_llm_hosts(monkeypatch):
    monkeypatch.setattr(
        "pipeline.stream_suggestions.suggest_stream_identifiers",
        lambda **kw: [
            {"value": "https://forum.cursor.com/t/1", "rationale": "official"},
            {"value": "community.example.com", "rationale": "adjacent"},
            {"value": "notahost", "rationale": "bare word — drop"},
            {"value": "forum.cursor.com", "rationale": "dup"},
        ],
    )
    src = DiscourseSource()
    try:
        cands = src.discover_streams(
            {"display": "Cursor", "url": "https://cursor.com"},
            max_candidates=8,
        )
    finally:
        src._client.close()
    hosts = [c.stream_config["host"] for c in cands]
    assert hosts == ["forum.cursor.com", "community.example.com"]
    assert cands[0].stream_config.get("query") == "Cursor"
    assert cands[0].stream_config.get("mode") == "search"


def test_discover_streams_url_fallback_when_llm_empty(monkeypatch):
    monkeypatch.setattr(
        "pipeline.stream_suggestions.suggest_stream_identifiers",
        lambda **kw: [],
    )
    src = DiscourseSource()
    try:
        cands = src.discover_streams(
            {"display": "Snowflake", "url": "https://www.snowflake.com/"},
            max_candidates=4,
        )
    finally:
        src._client.close()
    hosts = [c.stream_config["host"] for c in cands]
    assert hosts[0] == "forum.snowflake.com"
    assert "community.snowflake.com" in hosts
