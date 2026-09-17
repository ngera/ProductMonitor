"""Stack Exchange discovery + site normalization (ADR-0030)."""

from __future__ import annotations

from sources.stackex import StackExchangeSource, normalize_site


def test_normalize_site_bare_slug():
    assert normalize_site("stackoverflow") == "stackoverflow"
    assert normalize_site("SuperUser") == "superuser"


def test_normalize_site_url():
    assert normalize_site("https://stackoverflow.com/questions/1") == "stackoverflow"
    assert normalize_site("https://dba.stackexchange.com/") == "dba"
    assert normalize_site("www.serverfault.com") == "serverfault"


def test_discover_streams_fallback_when_llm_empty(monkeypatch):
    """When the assistant returns nothing, Site must still get defaults
    so the wizard configure textarea is never blank."""
    monkeypatch.setattr(
        "pipeline.stream_suggestions.suggest_stream_identifiers",
        lambda **kw: [],
    )
    src = StackExchangeSource()
    try:
        cands = src.discover_streams(
            {"display": "Snowflake", "description": "cloud data platform"},
            max_candidates=3,
        )
    finally:
        src._client.close()
    assert len(cands) >= 1
    sites = [c.stream_config["site"] for c in cands]
    assert "stackoverflow" in sites


def test_discover_streams_uses_llm_sites(monkeypatch):
    monkeypatch.setattr(
        "pipeline.stream_suggestions.suggest_stream_identifiers",
        lambda **kw: [
            {"value": "dba", "rationale": "DBA site"},
            {"value": "https://stackoverflow.com", "rationale": "main"},
            {"value": "dba", "rationale": "dup"},
        ],
    )
    src = StackExchangeSource()
    try:
        cands = src.discover_streams({"display": "Snowflake"}, max_candidates=8)
    finally:
        src._client.close()
    sites = [c.stream_config["site"] for c in cands]
    assert sites == ["dba", "stackoverflow"]
