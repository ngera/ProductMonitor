"""Admin > Prompt templates tests.

Master templates that generate product-specific prompts (scaffold + wizard
assistant LLM calls). Overrides land in config/prompt_templates.yaml;
unchanged templates keep serving the code default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_templates(tmp_path, monkeypatch):
    """Point config/prompt_templates.yaml at a tmp file so tests can save
    overrides without polluting the real repo."""
    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
    monkeypatch.setattr("pipeline.config.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("pipeline.prompt_templates._TEMPLATES_YAML",
                        cfg_dir / "prompt_templates.yaml")
    from pipeline import prompt_templates
    prompt_templates.clear_cache()
    return cfg_dir / "prompt_templates.yaml"


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
def client():
    from webui.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# prompt_templates helper
# ---------------------------------------------------------------------------


def test_get_returns_code_default_when_no_override(isolated_templates):
    """Fresh install has no config file — get() must fall back to the
    code-level default, not error."""
    from pipeline import prompt_templates
    assert isolated_templates.exists() is False
    val = prompt_templates.get("assistant_profile_draft")
    assert "customer feedback" in val.lower()


def test_get_returns_override_when_config_has_key(isolated_templates):
    """A YAML override wins over the code default."""
    isolated_templates.write_text(
        "templates:\n  assistant_profile_draft: \"my custom drafting prompt\"\n",
        encoding="utf-8",
    )
    from pipeline import prompt_templates
    prompt_templates.clear_cache()
    assert prompt_templates.get("assistant_profile_draft") == "my custom drafting prompt"


def test_save_overrides_strips_values_matching_default(isolated_templates):
    """If the user's edit equals the code default, we DON'T write it —
    otherwise a future default update would be masked by a stale override."""
    from pipeline import prompt_templates
    default = prompt_templates.TEMPLATES["assistant_profile_draft"].default
    prompt_templates.save_overrides({
        "assistant_profile_draft": default,   # unchanged
        "assistant_taxonomy_proposal": "custom taxonomy prompt",
    })
    data = yaml.safe_load(isolated_templates.read_text(encoding="utf-8"))
    assert "assistant_profile_draft" not in data["templates"]
    assert data["templates"]["assistant_taxonomy_proposal"] == "custom taxonomy prompt"


def test_revert_removes_one_override_but_keeps_others(isolated_templates):
    from pipeline import prompt_templates
    prompt_templates.save_overrides({
        "assistant_profile_draft": "X",
        "assistant_taxonomy_proposal": "Y",
    })
    prompt_templates.revert("assistant_profile_draft")
    data = yaml.safe_load(isolated_templates.read_text(encoding="utf-8"))
    assert "assistant_profile_draft" not in data["templates"]
    assert data["templates"]["assistant_taxonomy_proposal"] == "Y"


def test_unknown_key_get_raises(isolated_templates):
    from pipeline import prompt_templates
    with pytest.raises(KeyError):
        prompt_templates.get("no-such-template")


# ---------------------------------------------------------------------------
# Consumer wiring — the pipeline actually uses the templates helper
# ---------------------------------------------------------------------------


def test_profile_draft_reads_from_templates_helper(isolated_templates, monkeypatch):
    """A saved override for `assistant_profile_draft` must flow through
    to profile_draft.draft_profile — no reimport needed."""
    isolated_templates.write_text(
        "templates:\n  assistant_profile_draft: |\n    OVERRIDDEN prompt for tests\n",
        encoding="utf-8",
    )
    from pipeline import prompt_templates, profile_draft
    prompt_templates.clear_cache()

    captured = {}
    class _FakeContract:
        def call(self, spec):
            captured["system"] = spec.system
            return profile_draft.ProfileDraft(description="x")
    monkeypatch.setattr(profile_draft, "_assistant_contract",
                        lambda: _FakeContract())

    profile_draft.draft_profile("Acme", "desc", [])
    assert "OVERRIDDEN prompt for tests" in captured["system"]


def test_scaffold_writes_prompts_from_templates(isolated_templates,
                                                  products_dir, monkeypatch):
    """When admin overrides `scaffold_relevance_system`, new products'
    prompts.yaml get the overridden value (with {product_display} substituted)."""
    isolated_templates.write_text(
        "templates:\n"
        "  scaffold_relevance_system: |\n"
        "    CUSTOM sys for {product_display}\n",
        encoding="utf-8",
    )
    from pipeline import prompt_templates, product
    prompt_templates.clear_cache()
    product.scaffold_product("acme", "Acme Corp")
    prompts = yaml.safe_load(
        (products_dir / "acme" / "prompts.yaml").read_text(encoding="utf-8"),
    )
    assert "CUSTOM sys for Acme Corp" in prompts["relevance"]["system"]


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------


def test_admin_prompts_page_lists_all_templates(client, isolated_templates):
    resp = client.get("/admin/prompts")
    assert resp.status_code == 200
    # Every template key must have a textarea (all live in the DOM; JS
    # hides all but the selected one).
    from pipeline import prompt_templates
    for key in prompt_templates.TEMPLATES.keys():
        assert f'name="{key}"' in resp.text, f"textarea missing for {key}"
    # Stage groups render as optgroup labels in the dropdown.
    assert '<optgroup label="Product scaffold defaults">' in resp.text
    assert '<optgroup label="Assistant LLM (wizard v2)">' in resp.text
    assert '<optgroup label="Wizard v1 (legacy)">' in resp.text
    # No per-product prompt content leaks in — the earlier design mistakenly
    # showed those; the ask was templates only.
    assert "Per-product prompts" not in resp.text


def test_admin_prompts_page_has_dropdown_picker(client, isolated_templates):
    """New UX: single dropdown at top + one visible editor. Prevents the
    long-scroll problem from the earlier vertical-stack layout."""
    resp = client.get("/admin/prompts")
    assert resp.status_code == 200
    # The dropdown itself.
    assert 'id="tpl-select"' in resp.text
    # JS switcher hook.
    assert "window.__tplSwitch" in resp.text
    # Every template appears once as a panel (data-key) and once as an option.
    from pipeline import prompt_templates
    for key in prompt_templates.TEMPLATES.keys():
        assert f'data-key="{key}"' in resp.text, f"panel missing for {key}"
        assert f'value="{key}"' in resp.text, f"option missing for {key}"


def test_admin_prompts_save_writes_only_changed_values(
    client, isolated_templates,
):
    from pipeline import prompt_templates
    default = prompt_templates.TEMPLATES["assistant_profile_draft"].default
    tax_default = prompt_templates.TEMPLATES["assistant_taxonomy_proposal"].default
    # Post a form where profile_draft is unchanged from default and
    # taxonomy_proposal has been edited.
    form_data = {k: default if k == "assistant_profile_draft"
                    else tax_default if k == "assistant_taxonomy_proposal"
                    else prompt_templates.TEMPLATES[k].default
                 for k in prompt_templates.TEMPLATES.keys()}
    form_data["assistant_taxonomy_proposal"] = "user's custom taxonomy prompt"
    resp = client.post("/admin/prompts", data=form_data, follow_redirects=False)
    assert resp.status_code == 303
    assert "saved=1" in resp.headers["location"]

    data = yaml.safe_load(isolated_templates.read_text(encoding="utf-8"))
    # Only the changed key is in the override file.
    assert set(data["templates"].keys()) == {"assistant_taxonomy_proposal"}
    assert data["templates"]["assistant_taxonomy_proposal"] == "user's custom taxonomy prompt"


def test_admin_prompts_revert_removes_override(client, isolated_templates):
    from pipeline import prompt_templates
    prompt_templates.save_overrides({"assistant_profile_draft": "custom"})
    resp = client.post("/admin/prompts",
                       data={"action": "revert", "key": "assistant_profile_draft"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "reverted=assistant_profile_draft" in resp.headers["location"]
    # Config file: either the key is gone or the whole templates map is empty.
    data = yaml.safe_load(isolated_templates.read_text(encoding="utf-8"))
    assert "assistant_profile_draft" not in (data.get("templates") or {})


def test_admin_subnav_includes_prompts_tab(client, isolated_templates):
    resp = client.get("/admin/prompts")
    assert 'href="/admin/prompts"' in resp.text
    assert "admin-subnav-tab active" in resp.text
