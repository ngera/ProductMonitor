"""Regression: Ollama/Mistral wraps classify JSON; flatten before validate."""

from __future__ import annotations

import json

from pipeline.llm import _flatten_structured, _prepare_structured_json
from pipeline.models import CoreClassification, RelevanceResult


def test_unwrap_classification_envelope():
    raw = {
        "Classification": {
            "is_topic_relevant": True,
            "areas": ["shell"],
            "content_types": ["bug_report"],
            "sentiment": -0.5,
            "summary": "Taskbar bug",
        }
    }
    flat = _flatten_structured(raw, CoreClassification)
    assert flat["is_topic_relevant"] is True
    assert flat["areas"] == ["shell"]
    obj = CoreClassification.model_validate_json(json.dumps(flat))
    assert obj.is_topic_relevant is True


def test_unwrap_product_extras_type_name():
    raw = {
        "Classification_ProductExtras": {
            "is_topic_relevant": False,
            "summary": "Azure blog, not Windows",
            "areas": [],
            "content_types": ["news_discussion"],
        }
    }
    flat = _flatten_structured(raw, CoreClassification)
    assert flat["is_topic_relevant"] is False


def test_merge_labels_envelope():
    raw = {
        "labels": {
            "is_topic_relevant": True,
            "areas": ["input"],
            "content_types": ["feedback"],
        },
        "summary": "Cursor stuck in search",
        "sentiment": -0.2,
    }
    flat = _flatten_structured(raw, CoreClassification)
    assert flat["is_topic_relevant"] is True
    assert flat["summary"] == "Cursor stuck in search"
    assert flat["areas"] == ["input"]
    CoreClassification.model_validate(flat)


def test_alias_topic_relevant():
    raw = {
        "topic_relevant": True,
        "areas": [],
        "content_types": ["question"],
        "summary": "ok",
    }
    flat = _flatten_structured(raw, CoreClassification)
    assert "topic_relevant" not in flat
    assert flat["is_topic_relevant"] is True


def test_prepare_structured_json_end_to_end():
    text = (
        'Here you go:\n```json\n'
        '{"Classification": {"topic_relevant": true, "summary": "x", '
        '"areas": [], "content_types": ["feedback"]}}\n```'
    )
    prepared = _prepare_structured_json(text, CoreClassification)
    obj = CoreClassification.model_validate_json(prepared)
    assert obj.is_topic_relevant is True
    assert obj.summary == "x"


def test_relevance_result_not_broken_by_aliases():
    """RelevanceResult uses `relevant`, not is_topic_relevant — leave alone
    when already valid."""
    raw = {"relevant": True, "confidence": 0.9}
    flat = _flatten_structured(raw, RelevanceResult)
    assert flat["relevant"] is True
    RelevanceResult.model_validate(flat)


def test_fill_product_extras_list_dump():
    """Ollama sometimes returns only ProductExtras: [...] — soft-fail."""
    from pipeline.llm import _prepare_structured_json

    prepared = _prepare_structured_json(
        '{"ProductExtras": ["Gaming", "Office Suite"]}',
        CoreClassification,
    )
    obj = CoreClassification.model_validate_json(prepared)
    assert obj.is_topic_relevant is False
    assert "incomplete" in obj.summary


def test_fill_schema_type_name_list():
    from pipeline.llm import _prepare_structured_json

    prepared = _prepare_structured_json(
        '{"Classification_ProductExtras": ["Windows OS"]}',
        CoreClassification,
    )
    obj = CoreClassification.model_validate_json(prepared)
    assert obj.is_topic_relevant is False


def test_unwrap_classification_with_null_error_sibling():
    raw = {
        "Classification": {
            "topic_relevant": True,
            "summary": "ok",
            "areas": [],
            "content_types": ["feedback"],
        },
        "Error": None,
    }
    flat = _flatten_structured(raw, CoreClassification)
    assert flat["is_topic_relevant"] is True
    CoreClassification.model_validate(flat)
