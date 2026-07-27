"""The 'Set up your LLM first' banner on /wizard.

Three failure modes we diagnose:
  - config missing (assistant_llm.yaml absent / no endpoint / no model)
  - flag off (assistant_llm_enabled=false)
  - both

Regression: the banner used to show even for existing drafts on the
describe step because wizard_step didn't pass `assistant_llm_ready` down.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def products_dir(tmp_path, monkeypatch):
    d = tmp_path / "products"; d.mkdir()
    monkeypatch.setattr("pipeline.product.PRODUCTS_DIR", d)
    monkeypatch.setattr("pipeline.features.PRODUCTS_DIR", d)
    monkeypatch.setattr("webui.app.PRODUCTS_DIR", d)
    from pipeline import product as _p, features as _f
    _p.clear_cache(); _f.clear_cache()
    return d


@pytest.fixture
def isolated_configs(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
    monkeypatch.setattr("pipeline.config.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("pipeline.assistant_llm._CONFIG_PATH",
                        cfg_dir / "assistant_llm.yaml")
    monkeypatch.setattr("pipeline.features._FEATURES_YAML",
                        cfg_dir / "features.yaml")
    (cfg_dir / "features.yaml").write_text(
        "features:\n"
        "  wizard_v2_enabled: true\n"
        "  assistant_llm_enabled: false\n",
        encoding="utf-8",
    )
    from pipeline import features
    features.clear_cache()
    return cfg_dir


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


def _configure_assistant(cfg_dir):
    from pipeline import assistant_llm
    assistant_llm.save_config(assistant_llm.AssistantLLMConfig(
        endpoint="https://api.anthropic.com/v1",
        model="claude-haiku-4-5-20251001",
    ))


def _turn_flag_on(cfg_dir):
    (cfg_dir / "features.yaml").write_text(
        "features:\n"
        "  wizard_v2_enabled: true\n"
        "  assistant_llm_enabled: true\n",
        encoding="utf-8",
    )
    from pipeline import features; features.clear_cache()


def test_banner_hidden_when_configured_and_flag_on(client, products_dir, isolated_configs):
    _configure_assistant(isolated_configs)
    _turn_flag_on(isolated_configs)
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert "Set up your LLM first" not in resp.text


def test_banner_shows_flag_off_diagnostic_when_configured_but_flag_off(
    client, products_dir, isolated_configs,
):
    """Regression: user has set up assistant LLM, but the flag is off. The
    banner must NAME that specific state so the user knows to enable the
    flag, not re-configure the endpoint."""
    _configure_assistant(isolated_configs)
    # Flag stays off from fixture.
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert "Set up your LLM first" in resp.text
    # The specific diagnostic mentions the flag.
    assert "assistant_llm_enabled" in resp.text
    # And the action button points at /admin/features, not /wizard/llm.
    assert 'href="/admin/features"' in resp.text


def test_banner_shows_config_missing_diagnostic(
    client, products_dir, isolated_configs,
):
    """Config file absent, flag on → diagnostic names the missing config."""
    _turn_flag_on(isolated_configs)
    # Do NOT configure assistant.
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert "Set up your LLM first" in resp.text
    assert "config/assistant_llm.yaml" in resp.text
    # Action still points at the setup wizard.
    assert "/wizard/llm?return=" in resp.text


def test_banner_check_busts_features_cache(
    client, products_dir, isolated_configs, monkeypatch,
):
    """features.enabled is @lru_cache'd — a hand-edit of features.yaml
    outside our request cycle would return a stale False without a bust.
    The wizard route must clear the cache so a fresh check runs."""
    _configure_assistant(isolated_configs)
    # Flag off in fixture. Prime the cache with the current (False) value.
    from pipeline import features
    features._global_features()  # warms cache
    # Flip the flag file directly (simulating an admin/features save or hand edit).
    _turn_flag_on(isolated_configs)
    # We deliberately do NOT call features.clear_cache() here — the route
    # must do that for us.
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert "Set up your LLM first" not in resp.text


def test_existing_draft_describe_step_gets_ready_check(
    client, products_dir, isolated_configs, monkeypatch,
):
    """Regression: on the DESCRIBE step of an existing draft (routed via
    wizard_step, not wizard_landing), the ready check was omitted so the
    banner always showed. This test asserts it's now passed through."""
    _configure_assistant(isolated_configs)
    _turn_flag_on(isolated_configs)
    from pipeline import wizard_v2
    wizard_v2.save_draft(products_dir, wizard_v2.WizardV2Draft(
        slug="acme", display="Acme", step="describe",
    ))
    resp = client.get("/wizard/acme")
    assert resp.status_code == 200
    assert "Set up your LLM first" not in resp.text
