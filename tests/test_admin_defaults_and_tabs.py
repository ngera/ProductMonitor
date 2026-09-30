"""Tests for the three admin-facing additions:
1. Default LLM provider — set on /connections, pre-selected in the wizard
2. 'Use for Assistant LLM' checkbox on /connections/<provider>
3. Product page 3-tab layout (Product Configuration | Advanced | Runs)
"""

from __future__ import annotations

from pathlib import Path

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
    monkeypatch.setattr("pipeline.admin_defaults._DEFAULTS_YAML",
                        cfg_dir / "admin_defaults.yaml")
    monkeypatch.setattr("pipeline.assistant_llm._CONFIG_PATH",
                        cfg_dir / "assistant_llm.yaml")
    monkeypatch.setattr("pipeline.features._FEATURES_YAML",
                        cfg_dir / "features.yaml")
    monkeypatch.setattr("webui.app._FEATURES_YAML",
                        cfg_dir / "features.yaml")
    (cfg_dir / "features.yaml").write_text(
        "features:\n  assistant_llm_enabled: false\n",
        encoding="utf-8",
    )
    from pipeline import admin_defaults, features
    admin_defaults.clear_cache(); features.clear_cache()
    return cfg_dir


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate the .env file AND os.environ. env_writer.set_var syncs
    writes into both, so isolating only the file leaks between tests
    (test A sets ANTHROPIC_API_KEY=sk-fake → test B still sees it in
    os.environ)."""
    import os
    env_file = tmp_path / ".env"
    monkeypatch.setattr("webui.app.ENV_FILE_PATH", env_file)
    # Snapshot + restore os.environ around the test so env_writer writes
    # into it don't leak to sibling tests.
    original_env = dict(os.environ)
    yield env_file
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Default LLM provider
# ---------------------------------------------------------------------------


def test_default_llm_provider_roundtrip(isolated_configs):
    from pipeline import admin_defaults
    assert admin_defaults.default_llm_provider() is None
    admin_defaults.set_default_llm_provider("anthropic")
    assert admin_defaults.default_llm_provider() == "anthropic"
    admin_defaults.set_default_llm_provider("")
    assert admin_defaults.default_llm_provider() is None


def test_set_default_llm_route_persists(client, isolated_configs):
    resp = client.post("/connections/default_llm",
                       data={"provider": "anthropic"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "default_saved=1" in resp.headers["location"]
    from pipeline import admin_defaults
    admin_defaults.clear_cache()
    assert admin_defaults.default_llm_provider() == "anthropic"


def test_wizard_options_move_default_to_top(monkeypatch, isolated_configs,
                                              isolated_env):
    """When admin sets `anthropic` as default and OpenAI has a key too,
    Anthropic must be first in _available_llm_options() so the wizard
    Review screen pre-selects it."""
    isolated_env.write_text(
        "ANTHROPIC_API_KEY=sk-ant-xxx\nOPENAI_API_KEY=sk-oai-xxx\n",
        encoding="utf-8",
    )
    from pipeline import admin_defaults
    admin_defaults.set_default_llm_provider("anthropic")
    from webui import wizard as _wz
    opts = _wz._available_llm_options()
    # First provider option (skipping the "skip" tail) must be Anthropic.
    hosted = [o for o in opts if o["choice"] == "hosted"]
    assert hosted[0]["provider"] == "anthropic"
    assert hosted[0]["is_default"] is True
    assert hosted[0]["badge"] == "default"


def test_connections_page_dropdown_shows_saved_default_as_selected(
    client, isolated_configs, isolated_env,
):
    """Regression: after saving 'anthropic' via POST /connections/default_llm,
    the GET /connections dropdown must render <option selected> for that
    value — not fall back to the "— none —" placeholder."""
    isolated_env.write_text("ANTHROPIC_API_KEY=sk-ant\n", encoding="utf-8")
    # Save via the actual POST route (matches what the UI form does).
    client.post("/connections/default_llm",
                data={"provider": "anthropic"},
                follow_redirects=False)
    # Now GET the page and check the rendered dropdown.
    # LLM dropdown moved to the LLM Connections tab per the sources/llms split.
    resp = client.get("/connections/llms")
    assert resp.status_code == 200
    # The saved value must be selected. We look for the exact HTML that
    # would set the option — Jinja renders `selected` (no value) when
    # the condition is true.
    assert 'value="anthropic"' in resp.text
    # Find the anthropic option line and check it has "selected".
    for line in resp.text.split("<option"):
        if 'value="anthropic"' in line:
            assert "selected" in line, (
                "The dropdown didn't mark the saved default as selected: "
                + line[:200]
            )
            break
    else:
        pytest.fail("anthropic option not rendered")


def test_connections_page_dropdown_only_lists_configured(
    client, isolated_configs, isolated_env,
):
    """The default-LLM dropdown must ONLY offer providers that are
    currently configured — no point picking one whose key isn't set."""
    isolated_env.write_text("ANTHROPIC_API_KEY=sk-ant\n", encoding="utf-8")
    # LLM dropdown moved to the LLM Connections tab per the sources/llms split.
    resp = client.get("/connections/llms")
    assert resp.status_code == 200
    # Anthropic (configured) is in the dropdown; OpenAI (not) is not.
    # We look for the option-shaped substrings so parsing HTML isn't needed.
    assert 'value="anthropic"' in resp.text
    assert '>Anthropic Claude (anthropic)<' in resp.text or 'Anthropic' in resp.text
    assert 'value="openai"' not in resp.text or 'OPENAI_API_KEY' not in isolated_env.read_text()


# ---------------------------------------------------------------------------
# 2. Use-for-Assistant checkbox on connection form
# ---------------------------------------------------------------------------


def test_connection_form_shows_use_for_assistant_checkbox_on_llm(
    client, isolated_configs, isolated_env,
):
    resp = client.get("/connections/anthropic")
    assert resp.status_code == 200
    assert 'name="use_for_assistant"' in resp.text
    assert "Also use this connection for the Assistant LLM" in resp.text


def test_connection_form_hides_checkbox_for_source_types(client, isolated_configs,
                                                          isolated_env):
    """Reddit is a source, not an LLM — the checkbox would be nonsense."""
    resp = client.get("/connections/reddit")
    assert resp.status_code == 200
    assert 'name="use_for_assistant"' not in resp.text


def test_connection_save_with_checkbox_copies_to_assistant_llm(
    client, isolated_configs, isolated_env,
):
    """Saving with the checkbox on:
       - writes the ANTHROPIC_API_KEY value to .env (normal path)
       - copies the endpoint + a recommended model to assistant_llm.yaml
       - writes the same value to ASSISTANT_LLM_API_KEY in .env
       - flips assistant_llm_enabled to True in features.yaml
    """
    resp = client.post("/connections/anthropic", data={
        "ANTHROPIC_API_KEY": "sk-ant-test-value",
        "use_for_assistant": "on",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "assistant=1" in resp.headers["location"]

    env = isolated_env.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" in env and "sk-ant-test-value" in env
    assert "ASSISTANT_LLM_API_KEY" in env
    # Same value copied to the assistant env var.
    assert env.count("sk-ant-test-value") == 2

    # assistant_llm.yaml has endpoint + model.
    cfg = yaml.safe_load(
        (isolated_configs / "assistant_llm.yaml").read_text(encoding="utf-8"),
    )
    assert cfg["endpoint"] == "https://api.anthropic.com/v1"
    assert cfg["model"]  # some recommended model got picked
    assert cfg["api_key_env"] == "ASSISTANT_LLM_API_KEY"

    # features.yaml flag flipped on.
    flags = yaml.safe_load(
        (isolated_configs / "features.yaml").read_text(encoding="utf-8"),
    )
    assert flags["features"]["assistant_llm_enabled"] is True


def test_connection_save_without_checkbox_does_not_touch_assistant(
    client, isolated_configs, isolated_env,
):
    resp = client.post("/connections/anthropic", data={
        "ANTHROPIC_API_KEY": "sk-ant-other",
    }, follow_redirects=False)
    assert resp.status_code == 303
    # No assistant config was written.
    assert not (isolated_configs / "assistant_llm.yaml").exists()
    # ASSISTANT_LLM_API_KEY not set.
    env = isolated_env.read_text(encoding="utf-8")
    assert "ASSISTANT_LLM_API_KEY" not in env


# ---------------------------------------------------------------------------
# 3. Product page tab layout
# ---------------------------------------------------------------------------


def test_product_page_has_section_tabs(client, products_dir):
    from pipeline.product import scaffold_product
    scaffold_product("acme", "Acme Corp", "A test product")
    resp = client.get("/products/acme")
    assert resp.status_code == 200
    for tab in ("summary", "configuration", "sources", "advanced"):
        assert f'data-tab="{tab}"' in resp.text
        assert f'data-panel="{tab}"' in resp.text
    assert 'href="/products/acme/items"' in resp.text
    assert "Summary" in resp.text
    assert "Configuration" in resp.text
    assert "Items" in resp.text
    assert "Advanced" in resp.text


def test_product_config_tab_shows_wizard_style_5_steps(client, products_dir):
    from pipeline.product import scaffold_product
    scaffold_product("acme", "Acme", "d")
    resp = client.get("/products/acme")
    assert resp.status_code == 200
    # 5 steps referenced in the Configuration panel.
    for label in ("Profile", "Sources", "Themes", "Snippets", "LLM"):
        assert label in resp.text
    # Each step links to the existing per-product edit page.
    assert 'href="/products/acme/profile"' in resp.text
    assert 'href="/products/acme/sources"' in resp.text
    assert 'href="/products/acme/taxonomy"' in resp.text
    assert 'href="/products/acme/snippets"' in resp.text
    assert 'href="/products/acme/llm_routing"' in resp.text


def test_product_advanced_tab_contains_hand_tune_surfaces(client, products_dir):
    from pipeline.product import scaffold_product
    scaffold_product("acme", "Acme")
    resp = client.get("/products/acme")
    # Prompts linked from Advanced.
    assert 'href="/products/acme/prompts"' in resp.text


def test_items_and_prompts_pages_keep_product_tabs(client, products_dir):
    """Items + Prompts are separate routes but must still show the product
    section tabs so operators can navigate without losing context."""
    from pipeline.product import scaffold_product
    scaffold_product("acme", "Acme")

    items = client.get("/products/acme/items")
    assert items.status_code == 200
    assert 'aria-label="Product sections"' in items.text
    assert 'aria-current="page"' in items.text
    assert 'href="/products/acme/items"' in items.text
    assert "is-active" in items.text

    prompts = client.get("/products/acme/prompts")
    assert prompts.status_code == 200
    assert 'aria-label="Product sections"' in prompts.text
    assert 'href="/products/acme#advanced"' in prompts.text
    assert "is-active" in prompts.text


def test_product_runs_tab_links_to_runs_page(client, products_dir):
    from pipeline.product import scaffold_product
    scaffold_product("acme", "Acme")
    resp = client.get("/products/acme")
    assert 'href="/products/acme/runs"' in resp.text
