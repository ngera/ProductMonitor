"""LLM setup wizard tests (standalone /wizard/llm route).

Covers:
- GET /wizard/llm renders without config
- POST /wizard/llm writes config/assistant_llm.yaml + flips feature flag
- POST /wizard/llm with return_url redirects back to the caller
- Missing endpoint / model → error redirect
- The wizard v2 landing shows the LLM setup CTA when unready
- The profile step banner links to /wizard/llm with a return URL
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
    """Point CONFIG_DIR at tmp_path and re-target both assistant_llm._CONFIG_PATH
    and features._FEATURES_YAML so writes don't touch the real repo."""
    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
    monkeypatch.setattr("pipeline.config.CONFIG_DIR", cfg_dir)
    # These constants are captured at import time; re-point them.
    monkeypatch.setattr("pipeline.assistant_llm._CONFIG_PATH",
                        cfg_dir / "assistant_llm.yaml")
    monkeypatch.setattr("pipeline.features._FEATURES_YAML",
                        cfg_dir / "features.yaml")
    # Seed features.yaml with the flag off so we can assert the flip.
    (cfg_dir / "features.yaml").write_text(
        "features:\n  assistant_llm_enabled: false\n", encoding="utf-8",
    )
    from pipeline import features as _f; _f.clear_cache()
    return cfg_dir


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate the wizard's .env file AND os.environ. env_writer.set_var
    syncs writes into both, so isolating only the file leaks between
    tests (test A sets ANTHROPIC_API_KEY=sk-fake → test B still sees it
    in os.environ)."""
    import os
    env_file = tmp_path / ".env"
    monkeypatch.setattr("webui.wizard._env_path", lambda: env_file)
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
# /wizard/llm — GET
# ---------------------------------------------------------------------------


def test_llm_wizard_renders_without_config(client, isolated_configs, isolated_env):
    resp = client.get("/wizard/llm")
    assert resp.status_code == 200
    # Provider chooser shown, "Not configured" state.
    assert "Anthropic (Claude)" in resp.text
    assert "OpenAI" in resp.text
    assert "Not configured" in resp.text
    # Recommended models are embedded per provider for the JS dropdown.
    assert "data-recommended-models" in resp.text
    assert "claude-haiku-4-5-20251001" in resp.text
    assert "gpt-4o-mini" in resp.text
    # Endpoint URL input is hidden (not a visible text input in the primary form).
    assert 'type="hidden" name="endpoint"' in resp.text


def test_llm_wizard_shows_configured_state(client, isolated_configs, isolated_env):
    from pipeline import assistant_llm as _al
    _al.save_config(_al.AssistantLLMConfig(
        endpoint="https://api.anthropic.com/v1",
        model="claude-haiku-4-5-20251001",
    ))
    resp = client.get("/wizard/llm")
    assert resp.status_code == 200
    assert "Already configured" in resp.text or "flag is\n      off" in resp.text
    assert "claude-haiku-4-5-20251001" in resp.text


def test_llm_wizard_shows_key_saved_indicator_when_env_has_key(
    client, isolated_configs, isolated_env,
):
    """A saved key can't be rendered back to the browser for security,
    so an empty API-key field must be paired with an explicit indicator
    that a key IS on disk — otherwise the user assumes it was lost."""
    isolated_env.write_text("ASSISTANT_LLM_API_KEY=sk-existing\n", encoding="utf-8")
    resp = client.get("/wizard/llm")
    assert resp.status_code == 200
    assert "A key is already saved" in resp.text
    assert "Leave the field below blank" in resp.text
    # The placeholder text also reflects the saved state.
    assert "leave blank to keep the saved key" in resp.text
    # Key section is for hosted providers; local Ollama uses a separate panel.
    assert 'id="llm-key-section"' in resp.text
    assert 'id="llm-nokey-section"' in resp.text
    assert "No API key needed" in resp.text


def test_llm_wizard_hides_key_section_markup_for_ollama_default(
    client, isolated_configs, isolated_env,
):
    """When the saved assistant endpoint is Ollama, the key text box starts
    hidden and the no-key panel is shown instead."""
    from pipeline import assistant_llm as _al
    _al.save_config(_al.AssistantLLMConfig(
        endpoint="http://localhost:11434/v1",
        model="mistral:latest",
    ))
    isolated_env.write_text("ASSISTANT_LLM_API_KEY=sk-leftover-hosted\n", encoding="utf-8")
    resp = client.get("/wizard/llm")
    assert resp.status_code == 200
    assert 'id="llm-key-section" style="display:none"' in resp.text
    assert "No API key needed" in resp.text
    # Hosted-key leftover banner must not be the visible section-3 copy.
    assert 'id="llm-nokey-section"' in resp.text
    assert 'id="api-key-input"' in resp.text
    assert "disabled" in resp.text


def test_llm_wizard_shows_no_key_indicator_when_env_empty(
    client, isolated_configs, isolated_env,
):
    """Symmetric case — empty .env must NOT show the 'key saved' banner and
    must nudge the user to paste one."""
    # isolated_env fixture already gives us an empty .env path.
    resp = client.get("/wizard/llm")
    assert resp.status_code == 200
    assert "No key saved yet" in resp.text
    assert "A key is already saved" not in resp.text


# ---------------------------------------------------------------------------
# /wizard/llm — POST
# ---------------------------------------------------------------------------


def test_llm_wizard_save_writes_config_and_flips_flag(client, isolated_configs,
                                                       isolated_env, monkeypatch):
    # Skip the network probe.
    monkeypatch.setattr("webui.wizard._probe_assistant_llm",
                        lambda *args, **kw: (True, "ok"))
    resp = client.post("/wizard/llm", data={
        "provider": "anthropic",
        "endpoint": "https://api.anthropic.com/v1",
        "model": "claude-haiku-4-5-20251001",
        "api_key": "sk-test-abc",
        "temperature": "0.2", "seed": "42",
        "timeout_seconds": "60", "max_retries": "3",
        "budget_usd_per_product_per_month": "10.0",
    }, follow_redirects=False)
    assert resp.status_code == 303
    # config/assistant_llm.yaml exists AND records the assistant-specific env var.
    cfg = yaml.safe_load(
        (isolated_configs / "assistant_llm.yaml").read_text(encoding="utf-8"),
    )
    assert cfg["endpoint"] == "https://api.anthropic.com/v1"
    assert cfg["model"] == "claude-haiku-4-5-20251001"
    assert cfg["api_key_env"] == "ASSISTANT_LLM_API_KEY"
    # Feature flag flipped on.
    features = yaml.safe_load(
        (isolated_configs / "features.yaml").read_text(encoding="utf-8"),
    )
    assert features["features"]["assistant_llm_enabled"] is True
    # API key written to the ASSISTANT_ env var, NOT the shared connections one.
    env_text = isolated_env.read_text(encoding="utf-8")
    assert "ASSISTANT_LLM_API_KEY" in env_text
    assert "sk-test-abc" in env_text
    # Sanity: it did NOT write to the shared /connections/anthropic env var.
    assert "\nANTHROPIC_API_KEY=" not in env_text
    assert not env_text.startswith("ANTHROPIC_API_KEY=")


def test_assistant_and_connections_keys_can_coexist(client, isolated_configs,
                                                     isolated_env, monkeypatch):
    """User has BOTH a connections key (via /connections/anthropic) and an
    assistant key (via the LLM wizard). Both must persist independently."""
    monkeypatch.setattr("webui.wizard._probe_assistant_llm",
                        lambda *args, **kw: (True, "ok"))
    # Simulate the user having already set the connections key via /connections.
    isolated_env.write_text(
        "ANTHROPIC_API_KEY=connections-key-for-pipeline\n", encoding="utf-8",
    )
    # Now they use the assistant wizard with a different key.
    client.post("/wizard/llm", data={
        "provider": "anthropic",
        "endpoint": "https://api.anthropic.com/v1",
        "model": "claude-haiku-4-5-20251001",
        "api_key": "assistant-key-for-wizard",
        "temperature": "0.2", "timeout_seconds": "60", "max_retries": "3",
        "budget_usd_per_product_per_month": "10.0",
    })
    env_text = isolated_env.read_text(encoding="utf-8")
    # Both keys present, distinct env vars.
    assert "ANTHROPIC_API_KEY=" in env_text
    assert "connections-key-for-pipeline" in env_text
    assert "ASSISTANT_LLM_API_KEY=" in env_text
    assert "assistant-key-for-wizard" in env_text


def test_llm_wizard_save_defaults_from_provider_preset(client, isolated_configs,
                                                        isolated_env, monkeypatch):
    """User can leave endpoint + model blank; provider preset fills them in."""
    monkeypatch.setattr("webui.wizard._probe_assistant_llm",
                        lambda *args, **kw: (True, "ok"))
    resp = client.post("/wizard/llm", data={
        "provider": "openai", "endpoint": "", "model": "",
        "api_key": "sk-x", "temperature": "0.2",
        "timeout_seconds": "60", "max_retries": "3",
        "budget_usd_per_product_per_month": "10.0",
    }, follow_redirects=False)
    assert resp.status_code == 303
    cfg = yaml.safe_load((isolated_configs / "assistant_llm.yaml").read_text())
    assert cfg["endpoint"] == "https://api.openai.com/v1"
    assert cfg["model"] == "gpt-4o-mini"


def test_llm_wizard_return_url_redirects_back_on_success(
    client, isolated_configs, isolated_env, monkeypatch,
):
    monkeypatch.setattr("webui.wizard._probe_assistant_llm",
                        lambda *args, **kw: (True, "ok"))
    resp = client.post("/wizard/llm", data={
        "provider": "ollama", "endpoint": "", "model": "",
        "api_key": "", "temperature": "0.2",
        "timeout_seconds": "60", "max_retries": "3",
        "budget_usd_per_product_per_month": "10.0",
        "return_url": "/wizard/some-slug",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/wizard/some-slug"


def test_llm_wizard_missing_endpoint_redirects_with_error(
    client, isolated_configs, isolated_env, monkeypatch,
):
    # No provider set AND no explicit endpoint → defaults can't fill.
    resp = client.post("/wizard/llm", data={
        "provider": "", "endpoint": "", "model": "some-model",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=endpoint" in resp.headers["location"]


# ---------------------------------------------------------------------------
# CTA on wizard landing + profile step
# ---------------------------------------------------------------------------


def test_landing_shows_llm_cta_when_unready(client, products_dir, isolated_configs,
                                             monkeypatch):
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: flag == "wizard_v2_enabled")
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert "Set up your LLM first" in resp.text
    assert "/wizard/llm?return=%2Fwizard" in resp.text


def test_landing_hides_llm_cta_when_ready(client, products_dir, isolated_configs,
                                           monkeypatch):
    from pipeline import assistant_llm as _al
    _al.save_config(_al.AssistantLLMConfig(
        endpoint="https://api.anthropic.com/v1", model="claude-haiku-4-5-20251001",
    ))
    def _flags(flag, product_id=None):
        return flag in ("wizard_v2_enabled", "assistant_llm_enabled")
    monkeypatch.setattr("pipeline.features.enabled", _flags)
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert "Set up your LLM first" not in resp.text


def _install_fake_openai(monkeypatch, *, raise_exc=None, capture=None):
    """Stub the OpenAI client with a chat.completions.create() that either
    returns cleanly or raises the given exception."""
    if capture is None:
        capture = {}
    class _Completions:
        def create(self_inner, **kwargs):
            capture["completion_kwargs"] = kwargs
            if raise_exc:
                raise raise_exc
            return object()
    class _Chat:
        completions = _Completions()
    class _FakeOpenAI:
        def __init__(self, base_url, api_key, timeout):
            capture["base_url"] = base_url
            capture["api_key"] = api_key
        chat = _Chat()
    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    return capture


def test_normalize_endpoint_adds_trailing_slash():
    """Trailing slash is REQUIRED for Anthropic's OpenAI-compat layer —
    without it the SDK builds URLs that drop the /v1 segment (httpx's
    URL.join treats the last unslashed path segment as a filename)."""
    from webui.wizard import _normalize_endpoint
    assert _normalize_endpoint("https://api.anthropic.com/v1") == "https://api.anthropic.com/v1/"
    assert _normalize_endpoint("https://api.anthropic.com/v1/") == "https://api.anthropic.com/v1/"
    assert _normalize_endpoint("") == ""


def test_probe_uses_chat_completions_not_models_list(monkeypatch):
    """Anthropic + Gemini OpenAI-compat layers don't forward /v1/models
    (they return 401 for unsupported endpoints). The probe must use
    chat.completions.create so a valid key doesn't get flagged as invalid."""
    from webui import wizard as wz
    captured = _install_fake_openai(monkeypatch)
    ok, _ = wz._probe_assistant_llm(
        "https://api.anthropic.com/v1", "claude-haiku-4-5-20251001",
        "ASSISTANT_LLM_API_KEY", typed_key="valid-key",
    )
    assert ok is True
    # If this key ever disappears from completion_kwargs the probe has
    # regressed to the models.list() shape that hangs on Anthropic.
    assert "max_tokens" in captured["completion_kwargs"]


def test_probe_prefers_typed_key_over_env(monkeypatch):
    """User pastes a fresh key + clicks Test before saving. The probe MUST
    use that key, not fall back to os.environ."""
    from webui import wizard as wz
    captured = _install_fake_openai(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-env-key")

    ok, msg = wz._probe_assistant_llm(
        "https://api.anthropic.com/v1", "claude-haiku-4-5-20251001",
        "ANTHROPIC_API_KEY", typed_key="fresh-form-key",
    )
    assert ok is True
    assert captured["api_key"] == "fresh-form-key"
    # Uses chat.completions with max_tokens=1 (not models.list, which
    # Anthropic's OpenAI-compat layer doesn't forward).
    assert captured["completion_kwargs"]["max_tokens"] == 1
    assert captured["completion_kwargs"]["model"] == "claude-haiku-4-5-20251001"
    # Endpoint gets normalized to trailing slash (Anthropic requirement).
    assert captured["base_url"] == "https://api.anthropic.com/v1/"


def test_probe_prefers_env_file_over_stale_os_environ(monkeypatch, tmp_path):
    """`dotenv.set_key` writes to .env but does NOT reload os.environ.
    A key that was set by a fresh Save must win over the stale one in
    os.environ from server startup."""
    from webui import wizard as wz
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=freshly-saved-key\n", encoding="utf-8")
    monkeypatch.setattr(wz, "_env_path", lambda: env_file)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-key-from-server-startup")
    captured = {}
    _install_fake_openai(monkeypatch, capture=captured)

    ok, msg = wz._probe_assistant_llm(
        "https://api.anthropic.com/v1", "m", "ANTHROPIC_API_KEY",
    )
    assert ok is True
    assert captured["api_key"] == "freshly-saved-key"
    assert "from .env" in msg


def test_save_autodetects_provider_from_endpoint_when_none_selected(
    client, isolated_configs, isolated_env, monkeypatch,
):
    """User types an Anthropic endpoint but doesn't click a provider card.
    Save should still write to the ASSISTANT_-prefixed env var, not silently
    skip because provider_id is empty."""
    monkeypatch.setattr("webui.wizard._probe_assistant_llm",
                        lambda *a, **kw: (True, "ok"))
    resp = client.post("/wizard/llm", data={
        "provider": "",  # user didn't click a card
        "endpoint": "https://api.anthropic.com/v1",
        "model": "claude-haiku-4-5-20251001",
        "api_key": "sk-test-xyz",
        "temperature": "0.2", "seed": "42",
        "timeout_seconds": "60", "max_retries": "3",
        "budget_usd_per_product_per_month": "10.0",
    }, follow_redirects=False)
    assert resp.status_code == 303
    env_text = isolated_env.read_text(encoding="utf-8")
    assert "ASSISTANT_LLM_API_KEY" in env_text
    assert "sk-test-xyz" in env_text


def test_save_errors_when_key_pasted_for_unknown_endpoint(
    client, isolated_configs, isolated_env, monkeypatch,
):
    """User pastes a key but supplies a completely unknown endpoint AND no
    provider — we can't decide whether a key is needed, so we must fail
    loudly rather than dropping the key silently."""
    resp = client.post("/wizard/llm", data={
        "provider": "",
        "endpoint": "https://unknown-llm.example.com/v1",
        "model": "some-model",
        "api_key": "sk-test-xyz",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "cannot+determine+provider" in resp.headers["location"]


def test_assistant_key_env_is_provider_agnostic(
    client, isolated_configs, isolated_env, monkeypatch,
):
    """Switching providers must reuse the same ASSISTANT_LLM_API_KEY var —
    not create per-provider names — so one key slot serves any provider."""
    monkeypatch.setattr("webui.wizard._probe_assistant_llm",
                        lambda *a, **kw: (True, "ok"))
    # First save an Anthropic setup.
    client.post("/wizard/llm", data={
        "provider": "anthropic",
        "endpoint": "https://api.anthropic.com/v1",
        "model": "claude-haiku-4-5-20251001",
        "api_key": "sk-ant-first",
        "temperature": "0.2", "timeout_seconds": "60", "max_retries": "3",
        "budget_usd_per_product_per_month": "10.0",
    })
    env_after_anthropic = isolated_env.read_text(encoding="utf-8")
    assert "ASSISTANT_LLM_API_KEY" in env_after_anthropic
    # Explicitly no per-provider prefix leaked in.
    assert "ASSISTANT_ANTHROPIC_API_KEY" not in env_after_anthropic

    # Now switch to OpenAI — the same env var name is overwritten.
    client.post("/wizard/llm", data={
        "provider": "openai",
        "endpoint": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key": "sk-openai-second",
        "temperature": "0.2", "timeout_seconds": "60", "max_retries": "3",
        "budget_usd_per_product_per_month": "10.0",
    })
    env_after_openai = isolated_env.read_text(encoding="utf-8")
    assert "sk-openai-second" in env_after_openai
    assert "sk-ant-first" not in env_after_openai  # overwritten
    # Config on disk records the same generic env var name.
    cfg = yaml.safe_load(
        (isolated_configs / "assistant_llm.yaml").read_text(encoding="utf-8"),
    )
    assert cfg["api_key_env"] == "ASSISTANT_LLM_API_KEY"
    assert cfg["endpoint"] == "https://api.openai.com/v1"


def test_key_diagnostic_flags_truncated_paste():
    """The redacted key diagnostic must call out short pastes so a user
    who accidentally copied only half the key sees it in one glance."""
    from webui.wizard import _key_diagnostic
    assert "truncated" in _key_diagnostic("sk-ant-ab")
    assert _key_diagnostic("") == "empty"
    long_key = "sk-ant-api03-" + "x" * 100
    diag = _key_diagnostic(long_key)
    # Middle redacted, first + last 4 visible.
    assert "sk-a" in diag and diag.endswith("xxxx)")
    assert "truncated" not in diag


def test_401_error_includes_key_length_diagnostic(monkeypatch):
    """A 401 with the typed key must include the redacted key preview so
    the user can spot a partial paste without exposing the secret."""
    from webui import wizard as wz
    _install_fake_openai(
        monkeypatch,
        raise_exc=Exception("Error code: 401 - Invalid bearer token"),
    )
    # A key ≤ 10 chars trips the "probably truncated" hint from _key_diagnostic.
    ok, msg = wz._probe_assistant_llm(
        "https://api.anthropic.com/v1", "m", "ASSISTANT_LLM_API_KEY",
        typed_key="sk-ant-x",
    )
    assert ok is False
    assert "401" in msg
    # The diagnostic block includes the raw length so the user can spot
    # a bad paste without exposing the secret.
    assert "8 chars" in msg
    assert "truncated" in msg  # ≤10 chars trips the "too short" hint

    # Repeat with a full-length key so the redacted preview shows up.
    ok, msg = wz._probe_assistant_llm(
        "https://api.anthropic.com/v1", "m", "ASSISTANT_LLM_API_KEY",
        typed_key="sk-ant-abcd" + "x" * 90 + "1234",
    )
    assert ok is False
    assert "sk-a" in msg  # first 4 chars visible in the redacted preview
    assert "1234" in msg  # last 4 chars visible


def test_probe_401_message_is_helpful(monkeypatch):
    from webui import wizard as wz
    _install_fake_openai(
        monkeypatch,
        raise_exc=Exception("Error code: 401 - {'error': {'message': 'Invalid bearer token'}}"),
    )
    ok, msg = wz._probe_assistant_llm(
        "https://api.anthropic.com/v1", "m", "ANTHROPIC_API_KEY", typed_key="bad-key",
    )
    assert ok is False
    assert "key rejected" in msg
    # The 'source' hint helps the user diagnose stale-key issues.
    assert "key source: form" in msg


def test_profile_step_auto_retries_drafting_when_llm_becomes_available(
    client, products_dir, isolated_configs, monkeypatch,
):
    """Historic 'assistant LLM not configured' errors persist on the draft
    file. When the user comes back after configuring the LLM, the profile
    step must auto-retry so the banner reflects reality."""
    from pipeline import assistant_llm as _al
    from pipeline import profile_draft as _pd
    _al.save_config(_al.AssistantLLMConfig(
        endpoint="https://api.anthropic.com/v1",
        model="claude-haiku-4-5-20251001",
        api_key_env="ASSISTANT_LLM_API_KEY",
    ))
    def _flags(flag, product_id=None):
        return flag in ("wizard_v2_enabled", "assistant_llm_enabled")
    monkeypatch.setattr("pipeline.features.enabled", _flags)

    # Stub draft_profile to succeed on the retry.
    def _draft(name, url_or_desc, goals=None, *, product_id_for_budget=None):
        return _pd.DraftResult(profile=_pd.ProfileDraft(
            description="fresh draft", aliases=["fresh"],
        ))
    monkeypatch.setattr("webui.wizard._profile_draft.draft_profile", _draft)

    from pipeline import wizard_v2 as wv2
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="profile",
        drafting_error="assistant LLM not configured",
    ))

    resp = client.get("/wizard/acme")
    assert resp.status_code == 200
    # The stale banner is gone; the drafted content is now on the draft.
    assert "assistant LLM not configured" not in resp.text
    reloaded = wv2.load_draft(products_dir, "acme")
    assert reloaded.drafting_error == ""
    assert reloaded.aliases == ["fresh"]


def test_profile_banner_links_to_llm_wizard_with_return_url(
    client, products_dir, isolated_configs, monkeypatch,
):
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: flag == "wizard_v2_enabled")
    from pipeline import wizard_v2 as wv2
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="profile",
        drafting_error="assistant LLM not configured",
    ))
    resp = client.get("/wizard/acme")
    assert resp.status_code == 200
    assert "/wizard/llm?return=/wizard/acme" in resp.text
