"""Relevance/classify push item_id + source_id onto TokenContext for llm_usage."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.models import RelevanceResult
from pipeline.token_usage import TokenContext, get_context


def test_relevance_sets_item_and_source_context(monkeypatch):
    from pipeline import relevance as rel

    captured: list[TokenContext | None] = []

    class FakeClient:
        def structured(self, system, prompt, model):
            captured.append(get_context())
            return RelevanceResult(relevant=True, confidence=0.9)

    monkeypatch.setattr(rel, "app_config", lambda: {"filter": {"relevance_drop_confidence": 0.7}})
    monkeypatch.setattr(rel, "current_product", lambda: MagicMock(
        id="acme", display="Acme", aliases=[], url="",
    ))
    monkeypatch.setattr(rel.storage, "items_for_week", lambda *a, **k: [
        {"id": "rss:1", "source": "rss", "title": "Acme broken", "body": "b"},
    ])
    monkeypatch.setattr(rel.storage, "set_relevance_batch", lambda *a, **k: None)
    monkeypatch.setattr(rel.storage, "set_filter_status_batch", lambda *a, **k: None)
    monkeypatch.setattr(rel, "_render_prompt", lambda *a, **k: ("sys", "user"))

    rel.run_relevance("2026-W38", client=FakeClient())
    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0].item_id == "rss:1"
    assert captured[0].source_id == "rss"


def test_classify_sets_item_and_source_context(monkeypatch):
    from pipeline import classify as cl
    from pipeline.models import CoreClassification

    captured: list[TokenContext | None] = []

    class FakeClient:
        model = "test"
        def structured(self, system, prompt, schema):
            captured.append(get_context())
            # Minimal object normalize_classification can handle — stub the path.
            raise RuntimeError("stop-after-context")

    def _query(sql, params=None):
        if "item_classifications" in sql and "COUNT" in sql:
            return [{"n": 0}]
        return [
            {"id": "mc:9", "source": "microsoft_community", "title": "t", "body": "b"},
        ]

    monkeypatch.setattr(cl, "app_config", lambda: {"grouping": {}})
    monkeypatch.setattr(cl, "current_product", lambda: MagicMock(
        classification_schema=object,
    ))
    monkeypatch.setattr(cl.storage, "query", _query)
    monkeypatch.setattr(cl.storage, "set_relevance_batch", lambda *a, **k: None)
    monkeypatch.setattr(cl.storage, "set_filter_status_batch", lambda *a, **k: None)
    monkeypatch.setattr(cl, "extract", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cl, "_build_prompt", lambda *a, **k: ("sys", "user"))

    cl.run_classify("2026-W38", client=FakeClient())
    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0].item_id == "mc:9"
    assert captured[0].source_id == "microsoft_community"
