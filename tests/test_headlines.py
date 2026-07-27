"""Unit tests for digest v2 headline generation + cache.

Covers the pure helpers (cache key derivation, content/prompt hashing) and
the graceful-fallback path (returns None when assistant LLM isn't
configured). The live LLM call itself is an integration concern covered
elsewhere.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.digest import headlines


class TestContentHash:
    def test_same_content_same_hash(self):
        h1 = headlines._content_hash("a", "b", "c", "d")
        h2 = headlines._content_hash("a", "b", "c", "d")
        assert h1 == h2

    def test_title_change_changes_hash(self):
        h1 = headlines._content_hash("a", "b", "c", "d")
        h2 = headlines._content_hash("X", "b", "c", "d")
        assert h1 != h2

    def test_body_truncated_to_2000_chars(self):
        # Bodies longer than 2000 chars but identical in the first 2000 hash the same.
        long_body = "x" * 3000
        different_tail = long_body + "extra"
        assert headlines._content_hash("t", long_body, "s", "") == \
               headlines._content_hash("t", different_tail, "s", "")

    def test_none_values_treated_as_empty(self):
        # Missing fields should be treated as "" — no crashes, deterministic.
        assert headlines._content_hash(None, None, None, None) == \
               headlines._content_hash("", "", "", "")


class TestCacheKey:
    def test_composition(self):
        key = headlines._cache_key("abc", "def", "some-model")
        assert "abc" in key
        assert "def" in key
        assert "some-model" in key

    def test_truncated_to_120_chars(self):
        # Prevent unbounded growth from long model names.
        key = headlines._cache_key("a" * 50, "b" * 50, "m" * 200)
        assert len(key) <= 120


class TestCacheReadWrite:
    """Round-trip through the actual `headlines` warehouse table."""

    def test_put_then_get_returns_value(self, tmp_path, monkeypatch):
        # Point storage at a throwaway warehouse for this test.
        import duckdb
        db = tmp_path / "wh.duckdb"
        con = duckdb.connect(str(db))
        con.execute(
            "CREATE TABLE headlines ("
            "cache_key VARCHAR PRIMARY KEY, item_id VARCHAR NOT NULL, "
            "headline VARCHAR NOT NULL, model VARCHAR, generated_at TIMESTAMP)"
        )
        con.close()

        from pipeline import storage
        monkeypatch.setattr(storage, "warehouse_path", lambda: db)

        assert headlines._get_cached("nope") is None

        headlines._put_cached("k1", "item1", "First headline", "some-model")
        assert headlines._get_cached("k1") == "First headline"

        # Upsert semantics — same key, new headline replaces.
        headlines._put_cached("k1", "item1", "Second headline", "some-model")
        assert headlines._get_cached("k1") == "Second headline"


class TestGenerateHeadline:
    def test_returns_none_when_assistant_not_configured(self, monkeypatch):
        from pipeline import assistant_llm
        monkeypatch.setattr(assistant_llm, "is_configured", lambda: False)
        h = headlines.generate_headline(
            item_id="x", title="t", body="b", source_display_name="reddit",
        )
        assert h is None

    def test_returns_cached_headline_without_calling_llm(self, tmp_path, monkeypatch):
        import duckdb
        db = tmp_path / "wh.duckdb"
        con = duckdb.connect(str(db))
        con.execute(
            "CREATE TABLE headlines ("
            "cache_key VARCHAR PRIMARY KEY, item_id VARCHAR NOT NULL, "
            "headline VARCHAR NOT NULL, model VARCHAR, generated_at TIMESTAMP)"
        )
        con.close()

        from pipeline import assistant_llm, storage
        monkeypatch.setattr(storage, "warehouse_path", lambda: db)

        # Fake "configured" but never actually call the LLM — cache hit should
        # short-circuit before the LLM is touched.
        from types import SimpleNamespace
        monkeypatch.setattr(assistant_llm, "is_configured", lambda: True)
        monkeypatch.setattr(
            assistant_llm, "current_config",
            lambda: SimpleNamespace(model="test-model"),
        )
        # Sentinel — if the contract is built we'd see an error, but the
        # cache hit should mean we never get there.
        monkeypatch.setattr(
            headlines, "_assistant_contract",
            lambda: (_ for _ in ()).throw(AssertionError("contract should not be built on cache hit")),
        )

        ph = headlines._prompt_hash()
        ch = headlines._content_hash("Title", "Body", "reddit", "")
        key = headlines._cache_key(ch, ph, "test-model")
        headlines._put_cached(key, "item1", "Cached headline text", "test-model")

        h = headlines.generate_headline(
            item_id="item1", title="Title", body="Body",
            source_display_name="reddit", summary="",
        )
        assert h == "Cached headline text"


class TestGenerateBatch:
    def test_skips_items_missing_id(self, monkeypatch):
        from pipeline import assistant_llm
        monkeypatch.setattr(assistant_llm, "is_configured", lambda: False)
        out = headlines.generate_batch([
            {"title": "no id here"},
            {"id": "", "title": "empty id"},
        ])
        assert out == {}

    def test_missing_id_stays_out_of_result(self, monkeypatch):
        # With LLM not configured every item returns None; result has no keys.
        from pipeline import assistant_llm
        monkeypatch.setattr(assistant_llm, "is_configured", lambda: False)
        items = [
            {"item_id": "a", "title": "t"},
            {"item_id": "b", "title": "t2"},
        ]
        out = headlines.generate_batch(items)
        assert out == {}
