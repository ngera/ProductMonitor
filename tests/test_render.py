"""Tests for the render-time attribution guarantee (§13) and group labels."""

import pytest

from pipeline.render import (
    ATTRIBUTION_PARTIAL,
    ITEM_DISPLAYING_TEMPLATES,
    AttributionViolation,
    _group_label,
    validate_templates,
)


def test_validate_templates_passes_for_real_templates():
    # The shipped templates must all include the attribution partial.
    validate_templates()


def test_item_templates_actually_reference_partial():
    from pipeline.render import TEMPLATE_DIR

    for name in ITEM_DISPLAYING_TEMPLATES:
        text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        assert ATTRIBUTION_PARTIAL in text


def test_validator_raises_when_partial_missing(tmp_path, monkeypatch):
    import pipeline.render as render

    bad = tmp_path / "comments.html.j2"
    bad.write_text("<table>{{ item.title }}</table>", encoding="utf-8")
    monkeypatch.setattr(render, "TEMPLATE_DIR", tmp_path)
    monkeypatch.setattr(render, "ITEM_DISPLAYING_TEMPLATES", ["comments.html.j2"])
    with pytest.raises(AttributionViolation):
        render.validate_templates()


def test_group_label_entity():
    assert _group_label("entity:audio:driver:AX211") == "AX211 (driver)"


def test_group_label_kb():
    assert _group_label("kb:update:KB5036980") == "Update KB5036980"


def test_group_label_title():
    assert _group_label("title:audio:00ff") == "Similar reports"
