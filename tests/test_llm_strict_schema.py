"""Tests for _strict_schema in pipeline/llm.py.

OpenAI strict mode (and Anthropic's OpenAI-compat layer) reject JSON
schemas that:
  - lack `additionalProperties: false` on object types
  - have a `required` array shorter than `properties` (no truly optional fields)
  - carry OpenAI-unfriendly keywords (default, min/max*, format, pattern)

Pydantic's `model_json_schema()` doesn't set additionalProperties and marks
fields with defaults as optional. `_strict_schema` rewrites the emitted
schema so the same Pydantic models work for guided decoding.
"""

from __future__ import annotations

import pytest

from pipeline.llm import _strict_schema


def test_sets_additional_properties_false_on_object_type():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    out = _strict_schema(schema)
    assert out["additionalProperties"] is False


def test_sets_additional_properties_false_on_bare_object_no_properties():
    """Pydantic emits `dict`-typed fields as `{"type": "object"}` with no
    `properties` block. OpenAI + Anthropic strict mode reject these unless
    additionalProperties is explicitly set. Regression test — this was the
    hole that let ProfileDraft.suggested_sources[].stream_config through."""
    schema = {
        "type": "object",
        "properties": {
            "cfg": {"type": "object", "description": "arbitrary dict"},
        },
    }
    out = _strict_schema(schema)
    assert out["properties"]["cfg"]["additionalProperties"] is False


def test_makes_every_property_required():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }
    out = _strict_schema(schema)
    assert sorted(out["required"]) == ["a", "b"]


def test_recursively_walks_nested_objects():
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": {"type": "string"}},
            },
        },
    }
    out = _strict_schema(schema)
    assert out["additionalProperties"] is False
    assert out["properties"]["outer"]["additionalProperties"] is False
    assert out["properties"]["outer"]["required"] == ["inner"]


def test_walks_into_array_items():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"plugin_id": {"type": "string"}},
                },
            },
        },
    }
    out = _strict_schema(schema)
    assert out["properties"]["items"]["items"]["additionalProperties"] is False


def test_drops_unsupported_keywords():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 40,
                     "pattern": "^[a-z]+$", "default": "foo"},
            "count": {"type": "integer", "minimum": 0, "maximum": 100},
        },
    }
    out = _strict_schema(schema)
    name = out["properties"]["name"]
    count = out["properties"]["count"]
    for k in ("minLength", "maxLength", "pattern", "default"):
        assert k not in name
    for k in ("minimum", "maximum"):
        assert k not in count


def test_walks_into_definitions_for_ref_backed_models():
    """Pydantic emits nested BaseModels as `$defs` refs — the strictifier
    must walk into the definitions block, not just top-level properties."""
    schema = {
        "type": "object",
        "properties": {"sources": {"$ref": "#/$defs/SuggestedSource"}},
        "$defs": {
            "SuggestedSource": {
                "type": "object",
                "properties": {"plugin_id": {"type": "string"}},
            }
        },
    }
    out = _strict_schema(schema)
    assert out["$defs"]["SuggestedSource"]["additionalProperties"] is False
    assert out["$defs"]["SuggestedSource"]["required"] == ["plugin_id"]


def test_end_to_end_pydantic_profiledraft_schema_is_strict_valid():
    """The actual ProfileDraft schema — the one that failed against Anthropic —
    must satisfy strict-mode requirements after strictification.

    EVERY object type is checked, not just ones with `properties`. Bare
    objects (from `dict` fields) were the exact hole that returned 400
    from Anthropic's compat layer in production."""
    from pipeline.profile_draft import ProfileDraft
    raw = ProfileDraft.model_json_schema()
    strict = _strict_schema(raw)

    def _every_object_is_strict(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, node
                if "properties" in node:
                    assert set(node["required"]) == set(node["properties"].keys())
            for v in node.values():
                _every_object_is_strict(v)
        elif isinstance(node, list):
            for item in node:
                _every_object_is_strict(item)

    _every_object_is_strict(strict)
