"""Relevance context pre-gate and fail-closed parse errors."""

from __future__ import annotations

from types import SimpleNamespace

from pipeline.llm import LLMError
from pipeline.models import RelevanceResult
from pipeline.product_facts_prompt import (
    build_relevance_context,
    relevance_context_needles,
)
from pipeline.relevance import text_mentions_product


def _product(**kwargs):
    base = dict(
        id="eleven-labs",
        display="Eleven Labs",
        aliases=["ElevenLabs", "Eleven Labs"],
        url="https://elevenlabs.io/",
        scope_in=[],
        taxonomy={"areas": []},
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _windows_sound_product():
    return _product(
        id="windows-os",
        display="Windows OS",
        aliases=["Windows 11", "Windows"],
        url="https://www.microsoft.com/windows",
        taxonomy={
            "areas": [{
                "id": "sound-audio",
                "display": "Sound & audio",
                "enabled": True,
                "features": [{
                    "id": "sound-audio",
                    "display": "Sound & audio",
                    "description": (
                        "Sound settings, speakers, headphones, volume and "
                        "Bluetooth audio issues in the OS."
                    ),
                }],
            }],
        },
    )


def test_text_mentions_product_display_and_host():
    p = _product()
    assert text_mentions_product("Eleven Labs voice clone", "", p)
    assert text_mentions_product("try elevenlabs.io today", "", p)
    assert text_mentions_product("ElevenLabs TTS", "body", p)
    assert not text_mentions_product("OpenAI ships GPT-5", "general AI news", p)
    assert not text_mentions_product("GTA 6 physical media", "", p)


def test_text_mentions_theme_description_without_brand():
    """Posts about a theme surface pass the gate even without the brand name."""
    p = _windows_sound_product()
    assert text_mentions_product(
        "Sound settings missing after update",
        "headphones work but speakers don't",
        p,
    )
    assert text_mentions_product("Bluetooth audio crackles", "", p)
    # Unrelated noise still drops.
    assert not text_mentions_product("OpenAI ships GPT-5", "general AI news", p)


def test_build_relevance_context_includes_themes():
    p = _windows_sound_product()
    block = build_relevance_context(p)
    assert "PRODUCT:" in block
    assert "THEMES:" in block
    assert "Sound & audio" in block
    assert "headphones" in block
    assert "<user_input>" in block


def test_relevance_context_needles_include_theme_tokens():
    p = _windows_sound_product()
    needles = relevance_context_needles(p)
    assert "windows os" in needles or "windows" in needles
    assert any("sound" in n for n in needles)
    assert "headphones" in needles
    assert "bluetooth" in needles


def test_theme_shaped_area_maps_to_1_to_1_feature():
    """Themes UI saves as area+feature with matching ids (server contract)."""
    theme = {
        "id": "sound-audio",
        "display": "Sound & audio",
        "enabled": True,
        "keywords": "sound, audio",
        "entity_type_hint": "",
        "features": [{
            "id": "sound-audio",
            "display": "Sound & audio",
            "description": "Speakers and headphones.",
        }],
    }
    assert theme["id"] == theme["features"][0]["id"]
    assert theme["display"] == theme["features"][0]["display"]


def test_run_relevance_drops_no_mention_without_llm(monkeypatch):
    from pipeline import relevance as rel

    calls = []

    class FakeClient:
        def structured(self, *a, **k):
            calls.append(1)
            return RelevanceResult(relevant=True, confidence=0.99)

    statuses: list[tuple[str, str]] = []
    rels: list[tuple[str, float, bool]] = []

    monkeypatch.setattr(rel, "app_config", lambda: {"filter": {"relevance_drop_confidence": 0.7}})
    monkeypatch.setattr(rel, "current_product", lambda: _product())
    monkeypatch.setattr(
        rel.storage,
        "items_for_week",
        lambda *a, **k: [
            {"id": "rss:1", "source": "rss", "title": "GTA 6 disc", "body": "games news"},
            {"id": "rss:2", "source": "rss", "title": "Eleven Labs update", "body": "voice"},
        ],
    )
    monkeypatch.setattr(rel.storage, "set_relevance_batch", lambda rows: rels.extend(rows))
    monkeypatch.setattr(rel.storage, "set_filter_status_batch", lambda rows: statuses.extend(rows))
    monkeypatch.setattr(rel, "_render_prompt", lambda *a, **k: ("sys", "user"))

    out = rel.run_relevance("2026-W38", client=FakeClient())
    assert out["counters"]["no_mention"] == 1
    assert out["counters"]["dropped"] >= 1
    assert ("rss:1", "dropped:no_product_mention") in statuses
    assert ("rss:1", 1.0, False) in rels
    # Only the brand-mention item hits the LLM.
    assert len(calls) == 1
    assert ("rss:2", 0.99, True) in rels


def test_run_relevance_keeps_theme_mention_for_llm(monkeypatch):
    from pipeline import relevance as rel

    calls = []

    class FakeClient:
        def structured(self, *a, **k):
            calls.append(1)
            return RelevanceResult(relevant=True, confidence=0.9)

    statuses: list[tuple[str, str]] = []
    rels: list[tuple[str, float, bool]] = []

    monkeypatch.setattr(rel, "app_config", lambda: {"filter": {"relevance_drop_confidence": 0.7}})
    monkeypatch.setattr(rel, "current_product", lambda: _windows_sound_product())
    monkeypatch.setattr(
        rel.storage,
        "items_for_week",
        lambda *a, **k: [
            {
                "id": "rss:sound",
                "source": "rss",
                "title": "Headphones silent after update",
                "body": "no brand name here",
            },
        ],
    )
    monkeypatch.setattr(rel.storage, "set_relevance_batch", lambda rows: rels.extend(rows))
    monkeypatch.setattr(rel.storage, "set_filter_status_batch", lambda rows: statuses.extend(rows))
    monkeypatch.setattr(rel, "_render_prompt", lambda *a, **k: ("sys", "user"))

    out = rel.run_relevance("2026-W38", client=FakeClient())
    assert out["counters"]["no_mention"] == 0
    assert len(calls) == 1
    assert ("rss:sound", 0.9, True) in rels


def test_run_relevance_fail_closed_on_llm_error(monkeypatch):
    from pipeline import relevance as rel

    class Boom:
        def structured(self, *a, **k):
            raise LLMError("empty after repair")

    statuses: list[tuple[str, str]] = []
    rels: list[tuple[str, float, bool]] = []

    monkeypatch.setattr(rel, "app_config", lambda: {"filter": {"relevance_drop_confidence": 0.7}})
    monkeypatch.setattr(rel, "current_product", lambda: _product())
    monkeypatch.setattr(
        rel.storage,
        "items_for_week",
        lambda *a, **k: [
            {"id": "rss:el", "source": "rss", "title": "ElevenLabs", "body": "tts"},
        ],
    )
    monkeypatch.setattr(rel.storage, "set_relevance_batch", lambda rows: rels.extend(rows))
    monkeypatch.setattr(rel.storage, "set_filter_status_batch", lambda rows: statuses.extend(rows))
    monkeypatch.setattr(rel, "_render_prompt", lambda *a, **k: ("sys", "user"))

    out = rel.run_relevance("2026-W38", client=Boom())
    assert out["counters"]["errors"] == 1
    assert out["counters"]["dropped"] == 1
    assert ("rss:el", "dropped:not_topic_relevant") in statuses
    assert ("rss:el", 1.0, False) in rels
