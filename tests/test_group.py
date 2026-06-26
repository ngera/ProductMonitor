"""Tests for §4.8 deterministic grouping selectors (pure functions)."""

from pipeline.group import (
    canonical_score,
    choose_primary_area,
    compute_group_key,
    primary_entity,
)
from pipeline.models import Entity


def _ent(**kw) -> Entity:
    data = {"type": "driver", "vendor": "Intel", "product": "AX211",
            "role": "feature_implicated", "confidence": 0.8, "verbatim": "Intel AX211"}
    data.update(kw)
    return Entity(**data)


def test_primary_entity_picks_highest_confidence():
    a = _ent(vendor="Intel", confidence=0.6)
    b = _ent(vendor="AMD", confidence=0.9)
    assert primary_entity([a, b]).vendor == "AMD"


def test_primary_entity_none_without_implicated():
    e = _ent(role="hardware_in_use")
    assert primary_entity([e]) is None


def test_group_key_prefers_entity_with_product():
    key = compute_group_key("audio", [_ent()], ["KB5036980"], "title here")
    assert key == "entity:audio:driver:Intel:AX211"


def test_group_key_skips_null_product_entity_for_kb():
    # null product -> not eligible as primary key -> fall through to KB (§4.8.2)
    key = compute_group_key("audio", [_ent(product=None)], ["KB5036980"], "title")
    assert key == "kb:audio:KB5036980"


def test_group_key_title_fallback():
    key = compute_group_key("audio", [], [], "random audio cuts out")
    assert key.startswith("title:audio:")


def test_group_key_lowest_kb_chosen():
    key = compute_group_key("update", [], ["KB5036980", "KB5000001"], "t")
    assert key == "kb:update:KB5000001"


def test_choose_primary_area_uses_entity_hint():
    # driver entity_type_hint maps to 'drivers' area in taxonomy.yaml
    area = choose_primary_area(["audio", "drivers"], [_ent(type="driver")], "t", "b")
    assert area == "drivers"


def test_choose_primary_area_keyword_fallback():
    # no implicated entity -> keyword match; "webcam" -> camera area
    area = choose_primary_area(["camera", "audio"], [], "my webcam is broken", "webcam issue")
    assert area == "camera"


def test_choose_primary_area_respects_candidates():
    # hinted area must be among the item's declared areas
    area = choose_primary_area(["audio"], [_ent(type="driver")], "audio noise", "speaker")
    assert area == "audio"


def test_canonical_score_biases_repro_quality():
    low = {"score": 1.0, "repro_steps_quality": "none", "body": "x", "engagement_score": 0.0}
    high = {"score": 1.0, "repro_steps_quality": "detailed", "body": "x", "engagement_score": 0.0}
    assert canonical_score(high) > canonical_score(low)
