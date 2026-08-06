"""Admin > Tokens tracker — provider/role derivation, cross-product totals,
and route smoke."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from pipeline import token_usage as tu


# ---------------------------------------------------------------------------
# Provider / role derivation (pure)
# ---------------------------------------------------------------------------


def test_provider_from_endpoint_matches_known_prefixes():
    assert tu.provider_from_endpoint("https://api.anthropic.com/v1") == "Anthropic"
    assert tu.provider_from_endpoint("https://api.openai.com/v1") == "OpenAI"
    assert tu.provider_from_endpoint("http://localhost:5273/v1") == "Foundry Local"
    assert tu.provider_from_endpoint("http://localhost:11434/v1") == "Ollama"
    assert tu.provider_from_endpoint("http://localhost:9999/v1") == "Local (other)"


def test_provider_from_endpoint_edge_cases():
    assert tu.provider_from_endpoint("") == "Unknown"
    assert tu.provider_from_endpoint(None) == "Unknown"     # type: ignore[arg-type]
    assert tu.provider_from_endpoint("https://mystery.example.com") == "Other"


def test_role_from_stage_maps_expected_stages():
    assert tu.role_from_stage("classify") == "classify"
    assert tu.role_from_stage("relevance") == "relevance"
    assert tu.role_from_stage("digest") == "assistant"
    assert tu.role_from_stage("headlines") == "assistant"
    assert tu.role_from_stage("profile_draft") == "assistant"
    assert tu.role_from_stage("stream_suggestions") == "assistant"
    assert tu.role_from_stage("fetch") == "other"
    assert tu.role_from_stage("") == "other"


# ---------------------------------------------------------------------------
# cross_product_totals — with a fake _fetch_raw_rows so no warehouse needed
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    tu.clear_cross_cache()
    yield
    tu.clear_cross_cache()


def _fake_rows():
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    day = timedelta(days=1)
    return [
        {"ts": now,             "run_id": "r1", "stage": "classify",
         "source_id": "reddit", "product_id": "windows-os",
         "endpoint": "https://api.anthropic.com/v1",
         "model": "claude-sonnet-4-6",
         "prompt_tokens": 1000, "completion_tokens": 500,
         "cached_input_tokens": 0, "total_tokens": 1500},
        {"ts": now,             "run_id": "r1", "stage": "relevance",
         "source_id": "reddit", "product_id": "windows-os",
         "endpoint": "https://api.anthropic.com/v1",
         "model": "claude-haiku-4-5-20251001",
         "prompt_tokens": 200,  "completion_tokens": 50,
         "cached_input_tokens": 0, "total_tokens": 250},
        {"ts": now - day,       "run_id": "r0", "stage": "digest",
         "source_id": "",       "product_id": "google-search",
         "endpoint": "https://api.openai.com/v1",
         "model": "gpt-4o-mini",
         "prompt_tokens": 400,  "completion_tokens": 100,
         "cached_input_tokens": 0, "total_tokens": 500},
    ]


def test_cross_product_totals_group_by_product(monkeypatch):
    monkeypatch.setattr(tu, "_fetch_raw_rows", lambda s, u, product_filter=None: _fake_rows())
    since = datetime(2026, 7, 28, tzinfo=timezone.utc)
    until = datetime(2026, 7, 31, tzinfo=timezone.utc)
    res = tu.cross_product_totals(since, until, group_by=("product_id",))
    by_pid = {r["product_id"]: r["tokens"] for r in res["series"]}
    assert by_pid == {"windows-os": 1750, "google-search": 500}
    assert res["totals"]["tokens"] == 2250
    assert res["totals"]["products"] == 2


def test_cross_product_totals_group_by_provider_derives_correctly(monkeypatch):
    monkeypatch.setattr(tu, "_fetch_raw_rows", lambda s, u, product_filter=None: _fake_rows())
    since = datetime(2026, 7, 28, tzinfo=timezone.utc)
    until = datetime(2026, 7, 31, tzinfo=timezone.utc)
    res = tu.cross_product_totals(since, until, group_by=("provider",))
    by_prov = {r["provider"]: r["tokens"] for r in res["series"]}
    assert by_prov == {"Anthropic": 1750, "OpenAI": 500}


def test_cross_product_totals_filter_by_role(monkeypatch):
    monkeypatch.setattr(tu, "_fetch_raw_rows", lambda s, u, product_filter=None: _fake_rows())
    since = datetime(2026, 7, 28, tzinfo=timezone.utc)
    until = datetime(2026, 7, 31, tzinfo=timezone.utc)
    res = tu.cross_product_totals(
        since, until, group_by=("stage",), filters={"role": "assistant"},
    )
    # Only the digest row is assistant-role
    by_stage = {r["stage"]: r["tokens"] for r in res["series"]}
    assert by_stage == {"digest": 500}


def test_cross_product_totals_time_bucket_day(monkeypatch):
    monkeypatch.setattr(tu, "_fetch_raw_rows", lambda s, u, product_filter=None: _fake_rows())
    since = datetime(2026, 7, 28, tzinfo=timezone.utc)
    until = datetime(2026, 7, 31, tzinfo=timezone.utc)
    res = tu.cross_product_totals(since, until, group_by=("day", "product_id"))
    keys = {(r["day"], r["product_id"]) for r in res["series"]}
    assert ("2026-07-30", "windows-os") in keys
    assert ("2026-07-29", "google-search") in keys


def test_fetch_raw_rows_closes_connections_even_when_table_missing(tmp_path, monkeypatch):
    """Regression: _fetch_raw_rows used to leak read-only connections for
    warehouses without an llm_usage table, blocking pipeline subprocesses
    from writing to those warehouses (DuckDB IO Error). Verify every
    connection is closed by mocking duckdb.connect and tracking close calls.
    """
    import duckdb

    closes: list[bool] = []

    class FakeCon:
        def execute(self, sql, params=None):
            class R:
                def fetchone(self_inner):
                    # Simulate "no llm_usage table" — returns count 0.
                    return (0,)
                def fetchall(self_inner):
                    return []
            return R()
        def close(self):
            closes.append(True)

    # Return a fake warehouse path pair so _warehouse_paths returns 1 entry.
    fake_wh = tmp_path / "warehouse.duckdb"
    fake_wh.write_bytes(b"")   # exists, empty is fine — connect is mocked
    monkeypatch.setattr(tu, "_warehouse_paths", lambda: [("acme", fake_wh)])
    monkeypatch.setattr(duckdb, "connect", lambda *a, **kw: FakeCon())

    since = datetime(2026, 7, 28, tzinfo=timezone.utc)
    until = datetime(2026, 7, 31, tzinfo=timezone.utc)
    tu._fetch_raw_rows(since, until)
    assert closes == [True], "connection must be closed even when llm_usage table is absent"


def test_cross_cache_serves_repeat_calls(monkeypatch):
    calls = []
    def _fake(s, u, product_filter=None):
        calls.append((s, u))
        return _fake_rows()
    monkeypatch.setattr(tu, "_fetch_raw_rows", _fake)
    since = datetime(2026, 7, 28, tzinfo=timezone.utc)
    until = datetime(2026, 7, 31, tzinfo=timezone.utc)
    tu.cross_product_totals(since, until, group_by=("product_id",))
    tu.cross_product_totals(since, until, group_by=("product_id",))
    # First call fetches current + prior windows (2 fetches).
    # Second identical call is fully served from cache.
    assert len(calls) == 2
