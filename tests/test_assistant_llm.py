"""Tests for pipeline/assistant_llm.py (POST_V1_PLAN §4.8, ADR-0002)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_is_configured_false_when_missing(tmp_path, monkeypatch):
    """No config file → is_configured() returns False."""
    from pipeline import assistant_llm
    monkeypatch.setattr(assistant_llm, "_CONFIG_PATH", tmp_path / "assistant_llm.yaml")
    assert assistant_llm.is_configured() is False
    assert assistant_llm.current_config() is None


def test_save_and_reload_roundtrip(tmp_path, monkeypatch):
    """save_config() then _load_config() returns the same values."""
    from pipeline import assistant_llm
    monkeypatch.setattr(assistant_llm, "_CONFIG_PATH", tmp_path / "assistant_llm.yaml")

    cfg = assistant_llm.AssistantLLMConfig(
        endpoint="https://api.anthropic.com/v1",
        model="claude-sonnet-4-6",
        temperature=0.3,
        seed=99,
        timeout_seconds=45,
        max_retries=2,
        budget_usd_per_product_per_month=25.0,
    )
    assistant_llm.save_config(cfg)

    assert assistant_llm.is_configured() is True
    loaded = assistant_llm.current_config()
    assert loaded is not None
    assert loaded.endpoint == "https://api.anthropic.com/v1"
    assert loaded.model == "claude-sonnet-4-6"
    assert loaded.temperature == 0.3
    assert loaded.seed == 99
    assert loaded.timeout_seconds == 45
    assert loaded.max_retries == 2
    assert loaded.budget_usd_per_product_per_month == 25.0


def test_save_creates_backup_on_overwrite(tmp_path, monkeypatch):
    """Saving over an existing config leaves a .bak sibling."""
    from pipeline import assistant_llm
    cfg_path = tmp_path / "assistant_llm.yaml"
    monkeypatch.setattr(assistant_llm, "_CONFIG_PATH", cfg_path)

    first = assistant_llm.AssistantLLMConfig(endpoint="e1", model="m1")
    assistant_llm.save_config(first)
    assert cfg_path.exists()

    second = assistant_llm.AssistantLLMConfig(endpoint="e2", model="m2")
    assistant_llm.save_config(second)
    assert cfg_path.exists()
    assert cfg_path.with_suffix(cfg_path.suffix + ".bak").exists()

    reloaded = assistant_llm.current_config()
    assert reloaded.endpoint == "e2"


def test_load_returns_none_for_incomplete_yaml(tmp_path, monkeypatch):
    """A YAML file missing endpoint or model is treated as unconfigured."""
    from pipeline import assistant_llm
    cfg_path = tmp_path / "assistant_llm.yaml"
    monkeypatch.setattr(assistant_llm, "_CONFIG_PATH", cfg_path)

    cfg_path.write_text("temperature: 0.5\n", encoding="utf-8")
    assert assistant_llm.is_configured() is False


def test_budget_within_when_not_configured(tmp_path, monkeypatch):
    """No config → always within budget (returns True, 0, 0)."""
    from pipeline import assistant_llm
    monkeypatch.setattr(assistant_llm, "_CONFIG_PATH", tmp_path / "assistant_llm.yaml")

    within, spent, cap = assistant_llm.is_within_budget("p1")
    assert within is True
    assert spent == 0.0
    assert cap == 0.0


def test_budget_within_when_cap_is_zero(tmp_path, monkeypatch):
    """Cap of 0.0 disables the check → always within budget."""
    from pipeline import assistant_llm
    monkeypatch.setattr(assistant_llm, "_CONFIG_PATH", tmp_path / "assistant_llm.yaml")

    assistant_llm.save_config(assistant_llm.AssistantLLMConfig(
        endpoint="e",
        model="m",
        budget_usd_per_product_per_month=0.0,
    ))
    within, spent, cap = assistant_llm.is_within_budget("p1")
    assert within is True
    assert cap == 0.0


def test_client_raises_when_not_configured(tmp_path, monkeypatch):
    """client() with no config raises RuntimeError, not a cryptic AttributeError."""
    from pipeline import assistant_llm
    monkeypatch.setattr(assistant_llm, "_CONFIG_PATH", tmp_path / "assistant_llm.yaml")

    with pytest.raises(RuntimeError, match="not configured"):
        assistant_llm.client()
