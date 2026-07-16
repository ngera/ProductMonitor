"""Tests for pipeline/rationale.py (POST_V1_PLAN §4.7, ADR-0003)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pipeline.rationale import (
    RationaleContext,
    RationaleResponse,
    _cache_key,
    generate,
    load_cached,
    prompt_hash,
    store_cached,
)


# ---------------------------------------------------------------------------
# Cache-key correctness (D3 — must change on prompt OR model change)
# ---------------------------------------------------------------------------


def test_cache_key_stable_for_same_inputs():
    a = _cache_key(item_id="x", prompt_hash="p", model="m", model_version="v")
    b = _cache_key(item_id="x", prompt_hash="p", model="m", model_version="v")
    assert a == b


def test_cache_key_changes_when_prompt_hash_changes():
    a = _cache_key(item_id="x", prompt_hash="p1", model="m", model_version="v")
    b = _cache_key(item_id="x", prompt_hash="p2", model="m", model_version="v")
    assert a != b


def test_cache_key_changes_when_model_changes():
    a = _cache_key(item_id="x", prompt_hash="p", model="m1", model_version="v")
    b = _cache_key(item_id="x", prompt_hash="p", model="m2", model_version="v")
    assert a != b


def test_cache_key_changes_when_item_changes():
    a = _cache_key(item_id="x1", prompt_hash="p", model="m", model_version="v")
    b = _cache_key(item_id="x2", prompt_hash="p", model="m", model_version="v")
    assert a != b


def test_cache_key_changes_when_model_version_changes():
    a = _cache_key(item_id="x", prompt_hash="p", model="m", model_version="v1")
    b = _cache_key(item_id="x", prompt_hash="p", model="m", model_version="v2")
    assert a != b


# ---------------------------------------------------------------------------
# prompt_hash — order-independent, non-empty
# ---------------------------------------------------------------------------


def test_prompt_hash_stable_across_key_order():
    a = prompt_hash({"system": "sys", "template": "tmpl"})
    b = prompt_hash({"template": "tmpl", "system": "sys"})
    assert a == b


def test_prompt_hash_changes_on_edit():
    a = prompt_hash({"system": "sys", "template": "tmpl"})
    b = prompt_hash({"system": "sys", "template": "tmpl edited"})
    assert a != b


def test_prompt_hash_handles_empty():
    """Empty prompt block still produces a stable hash."""
    a = prompt_hash({})
    assert a and len(a) == 16


# ---------------------------------------------------------------------------
# Cache load/store roundtrip
# ---------------------------------------------------------------------------


def test_load_returns_none_when_missing(tmp_path, monkeypatch):
    from pipeline import rationale as _r
    monkeypatch.setattr("pipeline.config.app_config",
                        lambda: {"paths": {"data_root": str(tmp_path)}})
    monkeypatch.setattr("pipeline.config.resolve_path", lambda p: __import__("pathlib").Path(p))
    result = load_cached(product_id="p", week_id="2026-W01", item_id="i",
                         prompt_hash="h", model="m")
    assert result is None


def test_store_then_load_returns_same_response(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.app_config",
                        lambda: {"paths": {"data_root": str(tmp_path)}})
    monkeypatch.setattr("pipeline.config.resolve_path", lambda p: __import__("pathlib").Path(p))

    resp = RationaleResponse(
        rationale="This is the key rationale.",
        highlights=["A", "B", "C"],
    )
    store_cached(
        product_id="p", week_id="w", item_id="i",
        prompt_hash="ph", model="m", model_version="v", response=resp,
    )
    loaded = load_cached(
        product_id="p", week_id="w", item_id="i",
        prompt_hash="ph", model="m", model_version="v",
    )
    assert loaded is not None
    assert loaded.rationale == "This is the key rationale."
    assert loaded.highlights == ["A", "B", "C"]


def test_load_returns_none_for_different_key(tmp_path, monkeypatch):
    """Store under one prompt hash; load with a different one → miss."""
    monkeypatch.setattr("pipeline.config.app_config",
                        lambda: {"paths": {"data_root": str(tmp_path)}})
    monkeypatch.setattr("pipeline.config.resolve_path", lambda p: __import__("pathlib").Path(p))

    resp = RationaleResponse(rationale="r", highlights=["h"])
    store_cached(product_id="p", week_id="w", item_id="i",
                 prompt_hash="old_prompt", model="m", model_version="v",
                 response=resp)
    # Change the prompt hash to simulate a prompt edit:
    assert load_cached(product_id="p", week_id="w", item_id="i",
                       prompt_hash="new_prompt", model="m", model_version="v") is None


# ---------------------------------------------------------------------------
# generate() — cache-first, LLM call on miss
# ---------------------------------------------------------------------------


def test_generate_returns_cached_without_llm_call(tmp_path, monkeypatch):
    """Cache hit: no LLM call is made."""
    monkeypatch.setattr("pipeline.config.app_config",
                        lambda: {"paths": {"data_root": str(tmp_path)}})
    monkeypatch.setattr("pipeline.config.resolve_path", lambda p: __import__("pathlib").Path(p))

    # Pre-seed cache
    ctx = RationaleContext(
        item_id="i", title="T", body="B",
        source_display_name="reddit", primary_area="audio", summary="s",
    )
    resp = RationaleResponse(rationale="cached rationale", highlights=["h1"])
    store_cached(product_id="p", week_id="w", item_id="i",
                 prompt_hash="ph", model="m", model_version="",
                 response=resp)

    def _boom(*a, **kw):
        raise AssertionError("LLM should not be called on cache hit")

    with patch("pipeline.llm_contract.LLMResponseContract", side_effect=_boom):
        result = generate(ctx, product_id="p", week_id="w",
                          prompt_hash="ph", model="m")
    assert result is not None
    assert result.rationale == "cached rationale"


def test_generate_returns_none_when_llm_fails(tmp_path, monkeypatch):
    """LLM raises → generate returns None (callers show fallback)."""
    monkeypatch.setattr("pipeline.config.app_config",
                        lambda: {"paths": {"data_root": str(tmp_path)}})
    monkeypatch.setattr("pipeline.config.resolve_path", lambda p: __import__("pathlib").Path(p))

    ctx = RationaleContext(
        item_id="i", title="T", body="B",
        source_display_name="", primary_area="", summary="",
    )

    class _FailingContract:
        def __init__(self, role): raise RuntimeError("no LLM configured")

    with patch("pipeline.llm_contract.LLMResponseContract", _FailingContract):
        result = generate(ctx, product_id="p", week_id="w",
                          prompt_hash="ph_nomatch", model="m")
    assert result is None


def test_generate_calls_llm_on_cache_miss_and_stores(tmp_path, monkeypatch):
    """LLM returns; the response gets cached for future calls."""
    monkeypatch.setattr("pipeline.config.app_config",
                        lambda: {"paths": {"data_root": str(tmp_path)}})
    monkeypatch.setattr("pipeline.config.resolve_path", lambda p: __import__("pathlib").Path(p))

    ctx = RationaleContext(
        item_id="i", title="T", body="B",
        source_display_name="reddit", primary_area="audio", summary="",
    )
    expected = RationaleResponse(rationale="fresh", highlights=["a", "b"])

    class _StubContract:
        def __init__(self, role):
            pass
        def call(self, spec):
            return expected

    with patch("pipeline.llm_contract.LLMResponseContract", _StubContract):
        result = generate(ctx, product_id="p", week_id="w",
                          prompt_hash="ph_new", model="m_new")

    assert result is not None
    assert result.rationale == "fresh"

    # Now confirm it was written to cache
    cached = load_cached(product_id="p", week_id="w", item_id="i",
                        prompt_hash="ph_new", model="m_new")
    assert cached is not None
    assert cached.rationale == "fresh"


# ---------------------------------------------------------------------------
# RationaleResponse validation
# ---------------------------------------------------------------------------


def test_rationale_response_allows_zero_highlights():
    r = RationaleResponse(rationale="r", highlights=[])
    assert r.highlights == []


def test_rationale_response_caps_highlights_at_five():
    with pytest.raises(Exception):
        RationaleResponse(rationale="r", highlights=["a"] * 6)
