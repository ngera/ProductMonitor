"""Tests for the LLM API-key resolver — picks the right env var per endpoint."""

from __future__ import annotations

from pipeline.llm import _resolve_api_key


def _env(**kw):
    return dict(kw)


def test_explicit_api_key_env_wins():
    cfg = {"endpoint": "https://api.anthropic.com/v1", "api_key_env": "CUSTOM_KEY"}
    env = _env(CUSTOM_KEY="abc", ANTHROPIC_API_KEY="should-not-be-used")
    assert _resolve_api_key(cfg, env) == "abc"


def test_anthropic_endpoint_picks_anthropic_key():
    cfg = {"endpoint": "https://api.anthropic.com/v1"}
    env = _env(ANTHROPIC_API_KEY="sk-ant-xxx")
    assert _resolve_api_key(cfg, env) == "sk-ant-xxx"


def test_openai_endpoint_picks_openai_key():
    cfg = {"endpoint": "https://api.openai.com/v1"}
    env = _env(OPENAI_API_KEY="sk-yyy")
    assert _resolve_api_key(cfg, env) == "sk-yyy"


def test_gemini_endpoint_picks_google_key():
    cfg = {"endpoint": "https://generativelanguage.googleapis.com/v1beta/openai"}
    env = _env(GOOGLE_API_KEY="gemini-zzz")
    assert _resolve_api_key(cfg, env) == "gemini-zzz"


def test_local_ollama_returns_placeholder():
    cfg = {"endpoint": "http://localhost:11434/v1"}
    env = _env()
    assert _resolve_api_key(cfg, env) == "not-needed-for-local"


def test_local_foundry_returns_placeholder():
    cfg = {"endpoint": "http://localhost:5273/v1"}
    env = _env()
    assert _resolve_api_key(cfg, env) == "not-needed-for-local"


def test_127_address_treated_as_local():
    cfg = {"endpoint": "http://127.0.0.1:11434/v1"}
    env = _env()
    assert _resolve_api_key(cfg, env) == "not-needed-for-local"


def test_missing_anthropic_key_returns_placeholder():
    cfg = {"endpoint": "https://api.anthropic.com/v1"}
    env = _env()  # no ANTHROPIC_API_KEY
    assert _resolve_api_key(cfg, env) == "not-needed-for-local"


def test_unknown_remote_endpoint_uses_llm_api_key_fallback():
    cfg = {"endpoint": "https://api.somewhere-new.example.com/v1"}
    env = _env(LLM_API_KEY="fallback-key")
    assert _resolve_api_key(cfg, env) == "fallback-key"


def test_openrouter_endpoint_picks_openrouter_key():
    cfg = {"endpoint": "https://openrouter.ai/api/v1"}
    env = _env(OPENROUTER_API_KEY="or-xxx")
    assert _resolve_api_key(cfg, env) == "or-xxx"
