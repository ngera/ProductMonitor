"""Tests for the SourceManifest schema (POST_V1_PLAN §4.1, ADR-0001)."""

from __future__ import annotations

import pytest

from sources.base import (
    MANIFEST_SCHEMA_VERSION,
    FieldSpec,
    SourceManifest,
)


def test_manifest_minimum_valid():
    m = SourceManifest(plugin_id="test", display_name="Test")
    assert m.plugin_id == "test"
    assert m.display_name == "Test"
    assert m.category == "source"
    assert m.manifest_schema_version == MANIFEST_SCHEMA_VERSION


def test_manifest_rejects_empty_plugin_id():
    with pytest.raises(ValueError, match="plugin_id must be non-empty"):
        SourceManifest(plugin_id="", display_name="Test")


def test_manifest_rejects_bad_plugin_id_chars():
    # Slashes, dots, spaces, hyphens all invalid — YAML keys and Python
    # identifiers should be trivial to derive from plugin_id.
    for bad in ["te-st", "te.st", "te/st", "te st", "te$t"]:
        with pytest.raises(ValueError, match="must contain only"):
            SourceManifest(plugin_id=bad, display_name="Test")


def test_manifest_accepts_underscores_and_digits():
    m = SourceManifest(plugin_id="test_plugin_v2", display_name="Test")
    assert m.plugin_id == "test_plugin_v2"


def test_manifest_rejects_incompatible_schema_version():
    with pytest.raises(ValueError, match="incompatible with this runtime"):
        SourceManifest(
            plugin_id="test",
            display_name="Test",
            manifest_schema_version="99",
        )


def test_field_spec_validates_type():
    # Valid types work
    for t in ("text", "number", "bool", "csv", "textarea_list", "secret"):
        FieldSpec(name="x", label="X", type=t)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must be one of"):
        FieldSpec(name="x", label="X", type="invalid")  # type: ignore[arg-type]


def test_manifest_identifier_field_must_reference_a_stream_field():
    # identifier_field pointing at a nonexistent field raises
    with pytest.raises(ValueError, match="not in stream_fields"):
        SourceManifest(
            plugin_id="test",
            display_name="Test",
            stream_fields=[FieldSpec(name="foo", label="Foo")],
            identifier_field="does_not_exist",
        )


def test_manifest_identifier_field_matching_a_stream_field_ok():
    m = SourceManifest(
        plugin_id="test",
        display_name="Test",
        stream_fields=[FieldSpec(name="subreddit", label="Subreddit")],
        identifier_field="subreddit",
    )
    assert m.identifier_field == "subreddit"


def test_manifest_identifier_field_empty_is_ok():
    """An empty identifier_field is valid — some plugins may not have a
    natural single-key identifier."""
    m = SourceManifest(plugin_id="test", display_name="Test")
    assert m.identifier_field == ""


def test_manifest_default_lists_are_independent():
    """Regression: mutable defaults must not be shared between instances."""
    a = SourceManifest(plugin_id="a", display_name="A")
    b = SourceManifest(plugin_id="b", display_name="B")
    a.connection_fields.append(FieldSpec(name="x", label="X"))
    assert a.connection_fields != b.connection_fields
    assert len(b.connection_fields) == 0
