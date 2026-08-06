"""Admin features page — assistant LLM card tests.

The admin /connections page carries a summary card for the currently
configured assistant LLM plus a link to the setup wizard. This gives
admins a single place to see connection state alongside the feature flags
that gate its use.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_configs(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
    monkeypatch.setattr("pipeline.config.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("pipeline.assistant_llm._CONFIG_PATH",
                        cfg_dir / "assistant_llm.yaml")
    monkeypatch.setattr("pipeline.features._FEATURES_YAML",
                        cfg_dir / "features.yaml")
    (cfg_dir / "features.yaml").write_text(
        "features:\n  assistant_llm_enabled: false\n", encoding="utf-8",
    )
    from pipeline import features as _f; _f.clear_cache()
    return cfg_dir


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


def test_admin_features_shows_not_configured_card_when_no_config(client, isolated_configs):
    # Assistant LLM card moved to the LLM Connections tab per the sources/llms split.
    resp = client.get("/connections/llms")
    assert resp.status_code == 200
    assert "Assistant LLM" in resp.text
    assert "Not configured" in resp.text
    # The card links to the setup wizard.
    assert 'href="/wizard/llm"' in resp.text
    # And to the legacy dedicated form as an advanced escape hatch.
    assert 'href="/connections/assistant_llm"' in resp.text


def test_admin_features_shows_configured_summary(client, isolated_configs):
    from pipeline import assistant_llm as _al
    _al.save_config(_al.AssistantLLMConfig(
        endpoint="https://api.anthropic.com/v1",
        model="claude-haiku-4-5-20251001",
        budget_usd_per_product_per_month=5.5,
        api_key_env="ASSISTANT_LLM_API_KEY",
    ))
    # Assistant LLM card moved to the LLM Connections tab per the sources/llms split.
    resp = client.get("/connections/llms")
    assert resp.status_code == 200
    assert "https://api.anthropic.com/v1" in resp.text
    assert "claude-haiku-4-5-20251001" in resp.text
    assert "5.50" in resp.text
    assert "ASSISTANT_LLM_API_KEY" in resp.text
    # Button text switches from "Set up" to "Change" when configured.
    assert "Change" in resp.text
    assert "Set up" not in resp.text.split("Assistant LLM", 1)[1].split("</section>", 1)[0]


def test_admin_features_shows_flag_off_warning_when_configured_but_disabled(
    client, isolated_configs,
):
    """Config saved but flag off = wizard still shows 'not configured' banner.
    The admin card must flag this specific state so it's a one-glance fix."""
    from pipeline import assistant_llm as _al
    _al.save_config(_al.AssistantLLMConfig(
        endpoint="https://api.anthropic.com/v1",
        model="claude-haiku-4-5-20251001",
    ))
    # Flag stays False from the fixture.
    # Assistant LLM card moved to the LLM Connections tab per the sources/llms split.
    resp = client.get("/connections/llms")
    assert resp.status_code == 200
    assert "assistant_llm_enabled" in resp.text
    # The warning banner text.
    assert "is\n        <strong>off</strong>" in resp.text \
        or "flag below is" in resp.text
