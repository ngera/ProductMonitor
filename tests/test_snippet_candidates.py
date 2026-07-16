"""Tests for pipeline/snippet_candidates.py (POST_V1_PLAN §4.4-B)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pipeline.snippet_candidates import (
    CandidatePool,
    CandidateSuggestion,
    CandidateSuggestions,
    rank_and_zip,
    rank_candidates,
    sample_candidate_pool,
)


# ---------------------------------------------------------------------------
# Stratified sampling
# ---------------------------------------------------------------------------


class _FakeCon:
    """Minimal DuckDB con stand-in that returns queued rows."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return self._rows


class _FakeWarehouseCtx:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return _FakeCon(self._rows)

    def __exit__(self, *args):
        return False


def _row(item_id, area, ts=0):
    return (
        item_id,                                # id
        "src_display",                          # source_display_name
        f"Title of {item_id}",                  # title
        f"Body of {item_id}",                   # body
        f"https://example.com/{item_id}",       # url
        "author1",                              # author
        ts,                                     # created_at
        area,                                   # primary_area
        f"summary {item_id}",                   # summary
        -0.2,                                   # sentiment
    )


def _patch_warehouse(monkeypatch, rows):
    monkeypatch.setattr(
        "pipeline.storage.warehouse",
        lambda: _FakeWarehouseCtx(rows),
    )


def test_stratified_sample_caps_at_three_per_area(monkeypatch):
    """More than 3 items in an area → we only keep the top 3 by recency."""
    # 5 in area "audio" (should keep 3), 2 in area "network" (keep both)
    rows = (
        [_row(f"a{i}", "audio", ts=100 - i) for i in range(5)]
        + [_row(f"n{i}", "network", ts=50 - i) for i in range(2)]
    )
    _patch_warehouse(monkeypatch, rows)

    pool = sample_candidate_pool("prod", max_per_area=3)
    assert pool.total_relevant == 7
    assert pool.per_area_counts == {"audio": 3, "network": 2}
    assert pool.total_returned == 5


def test_stratified_sample_handles_missing_primary_area(monkeypatch):
    """Rows without primary_area fall into '(unknown)' bucket."""
    rows = [
        _row("a1", None),
        _row("a2", ""),
        _row("a3", "network"),
    ]
    _patch_warehouse(monkeypatch, rows)
    pool = sample_candidate_pool("prod", max_per_area=3)
    # None and "" both fall into "(unknown)"
    assert "(unknown)" in pool.per_area_counts
    assert pool.per_area_counts["(unknown)"] == 2
    assert pool.per_area_counts["network"] == 1


def test_stratified_sample_returns_empty_when_no_rows(monkeypatch):
    _patch_warehouse(monkeypatch, [])
    pool = sample_candidate_pool("prod")
    assert pool.items == []
    assert pool.per_area_counts == {}
    assert pool.total_relevant == 0


def test_stratified_sample_respects_hard_ceiling(monkeypatch):
    """Even with room in areas, we stop at hard_ceiling."""
    rows = [_row(f"item_{i}", f"area_{i}") for i in range(200)]
    _patch_warehouse(monkeypatch, rows)
    pool = sample_candidate_pool("prod", max_per_area=3, hard_ceiling=50)
    assert pool.total_returned == 50


def test_stratified_sample_swallows_warehouse_errors(monkeypatch):
    """If the warehouse throws (no DB yet), we return an empty pool
    without raising — the UI should show 'no data' cleanly."""
    def _boom():
        raise RuntimeError("no warehouse")
    monkeypatch.setattr("pipeline.storage.warehouse", _boom)
    pool = sample_candidate_pool("prod")
    assert pool.items == []


# ---------------------------------------------------------------------------
# rank_candidates + rank_and_zip
# ---------------------------------------------------------------------------


def test_rank_candidates_empty_pool_returns_empty():
    pool = CandidatePool()
    assert rank_candidates(pool) == []


def test_rank_candidates_uses_assistant_llm(monkeypatch):
    """When the LLM returns valid picks, rank_candidates filters to
    only ids that were actually in the pool."""
    pool = CandidatePool(items=[
        {"id": "keep_1", "source_display_name": "reddit", "title": "T", "body": "B",
         "url": "u", "author": "a", "created_at": 0, "primary_area": "audio",
         "summary": "", "sentiment": 0},
        {"id": "keep_2", "source_display_name": "reddit", "title": "T", "body": "B",
         "url": "u", "author": "a", "created_at": 0, "primary_area": "video",
         "summary": "", "sentiment": 0},
    ])

    fake_result = CandidateSuggestions(suggestions=[
        CandidateSuggestion(item_id="keep_1", polarity="positive_example", why="good"),
        CandidateSuggestion(item_id="hallucinated", polarity="positive_example", why="bad"),
        CandidateSuggestion(item_id="keep_2", polarity="negative_example", why="also good"),
    ])

    fake_client = SimpleNamespace(model="test", endpoint="http://x")
    monkeypatch.setattr("pipeline.assistant_llm.client", lambda: fake_client)

    # Stub the contract to return the fake result without touching HTTP.
    class _StubContract:
        def call(self, spec):
            return fake_result

    def _fake_build(inst):
        return _StubContract()

    monkeypatch.setattr("pipeline.snippet_candidates._build_assistant_contract", _fake_build)

    result = rank_candidates(pool)
    ids = [s.item_id for s in result]
    assert "keep_1" in ids and "keep_2" in ids
    assert "hallucinated" not in ids


def test_rank_candidates_returns_empty_when_assistant_unconfigured(monkeypatch):
    """If assistant_llm.client() raises RuntimeError (not configured),
    rank_candidates returns [] rather than propagating."""
    pool = CandidatePool(items=[
        {"id": "x", "source_display_name": "", "title": "", "body": "",
         "url": "", "author": "", "created_at": 0, "primary_area": "",
         "summary": "", "sentiment": 0},
    ])

    def _raises():
        raise RuntimeError("not configured")
    monkeypatch.setattr("pipeline.assistant_llm.client", _raises)
    assert rank_candidates(pool) == []


def test_rank_and_zip_puts_ranked_first(monkeypatch):
    """Ranked items come first, then unranked."""
    pool = CandidatePool(items=[
        {"id": "one", "source_display_name": "", "title": "", "body": "",
         "url": "", "author": "", "created_at": 0, "primary_area": "a",
         "summary": "", "sentiment": 0},
        {"id": "two", "source_display_name": "", "title": "", "body": "",
         "url": "", "author": "", "created_at": 0, "primary_area": "b",
         "summary": "", "sentiment": 0},
        {"id": "three", "source_display_name": "", "title": "", "body": "",
         "url": "", "author": "", "created_at": 0, "primary_area": "c",
         "summary": "", "sentiment": 0},
    ])

    fake_result = CandidateSuggestions(suggestions=[
        CandidateSuggestion(item_id="three", polarity="positive_example", why="w"),
        CandidateSuggestion(item_id="one", polarity="positive_example", why="w"),
    ])

    monkeypatch.setattr("pipeline.assistant_llm.client",
                        lambda: SimpleNamespace(model="m", endpoint="e"))

    class _StubContract:
        def call(self, spec):
            return fake_result

    monkeypatch.setattr("pipeline.snippet_candidates._build_assistant_contract",
                        lambda inst: _StubContract())

    zipped = rank_and_zip(pool)
    # 2 ranked + 1 unranked
    assert len(zipped) == 3
    assert zipped[0]["item"]["id"] == "three"          # LLM's #1
    assert zipped[1]["item"]["id"] == "one"            # LLM's #2
    assert zipped[2]["item"]["id"] == "two"            # not picked
    assert zipped[0]["suggestion"] is not None
    assert zipped[2]["suggestion"] is None
