"""Tests for pipeline/llm_contract.py (POST_V1_PLAN §4.15, ADR-0007)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from pipeline.llm_contract import LLMCallSpec, LLMContractError, LLMResponseContract


class TinyResponse(BaseModel):
    relevant: bool
    confidence: float


class FakeChatCompletion:
    def __init__(self, content: str):
        self.choices = [type("Choice", (), {
            "message": type("Msg", (), {"content": content})()
        })()]
        self.usage = None


class FakeChatClient:
    """Minimal fake of the OpenAI SDK's `chat.completions` interface."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.completions = self
        self.chat = self  # for `.chat.completions.create`
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs) -> FakeChatCompletion:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("no more fake responses queued")
        return FakeChatCompletion(self._responses.pop(0))


@pytest.fixture
def fake_llm_client(monkeypatch):
    """Patch LLMClient.__init__ so it doesn't actually connect."""
    from pipeline import llm

    def stub_init(self, role: str):
        self.role = role
        self.cfg = {"model": "test", "endpoint": "http://x", "temperature": 0}
        self.model = "test"
        self.endpoint = "http://x"
        # ._client is the fake we'll set per-test
        self._client = None

    monkeypatch.setattr(llm.LLMClient, "__init__", stub_init)
    yield


def _valid_json() -> str:
    return json.dumps({"relevant": True, "confidence": 0.9})


def test_valid_response_parses(fake_llm_client):
    contract = LLMResponseContract("test")
    contract._client._client = FakeChatClient([_valid_json()])

    result = contract.call(LLMCallSpec(
        system="sys",
        user="hello",
        response_model=TinyResponse,
    ))
    assert isinstance(result, TinyResponse)
    assert result.relevant is True
    assert result.confidence == 0.9


def test_invalid_response_retries_with_feedback(fake_llm_client):
    """First response is malformed; retry succeeds. Contract should include
    the previous bad output + error message in the retry prompt."""
    contract = LLMResponseContract("test")
    contract._client._client = FakeChatClient([
        "not-valid-json",
        _valid_json(),
    ])

    result = contract.call(LLMCallSpec(
        system="sys",
        user="hello",
        response_model=TinyResponse,
        max_retries=2,
    ))
    assert isinstance(result, TinyResponse)

    # Second call's messages should include the retry hint mentioning
    # "failed validation"
    calls = contract._client._client.calls
    assert len(calls) == 2
    retry_messages = calls[1]["messages"]
    retry_last = retry_messages[-1]["content"]
    assert "failed" in retry_last.lower() or "validation" in retry_last.lower()


def test_exhausted_retries_raise_contract_error(fake_llm_client):
    """All attempts fail → LLMContractError with diagnostic fields."""
    contract = LLMResponseContract("test")
    contract._client._client = FakeChatClient(["bad", "still-bad", "also-bad"])

    with pytest.raises(LLMContractError) as exc_info:
        contract.call(LLMCallSpec(
            system="sys",
            user="hello",
            response_model=TinyResponse,
            max_retries=2,   # attempts = 3 total
        ))

    err = exc_info.value
    assert err.last_error
    assert err.last_raw == "also-bad"


def test_zero_retries_gives_one_shot(fake_llm_client):
    """max_retries=0 → one attempt, no retry on failure."""
    contract = LLMResponseContract("test")
    contract._client._client = FakeChatClient(["invalid-json"])

    with pytest.raises(LLMContractError):
        contract.call(LLMCallSpec(
            system="sys",
            user="hello",
            response_model=TinyResponse,
            max_retries=0,
        ))
    # Only one call
    assert len(contract._client._client.calls) == 1


def test_custom_retry_hint_replaces_default(fake_llm_client):
    """Callers can override the retry preamble with domain-specific guidance."""
    contract = LLMResponseContract("test")
    contract._client._client = FakeChatClient(["bad", _valid_json()])

    custom_hint = "GENUINE_CUSTOM_HINT_XYZ"
    contract.call(LLMCallSpec(
        system="sys",
        user="hello",
        response_model=TinyResponse,
        retry_hint=custom_hint,
    ))
    calls = contract._client._client.calls
    assert custom_hint in calls[1]["messages"][-1]["content"]


# ---------------------------------------------------------------------------
# Prompt caching (§4.15)
# ---------------------------------------------------------------------------


def test_cacheable_content_helper_returns_string_for_non_anthropic():
    """On non-Anthropic providers, no cache markers — plain string content."""
    from pipeline.llm import cacheable_content

    assert cacheable_content("stable prompt", "http://localhost:11434/v1") == "stable prompt"
    assert cacheable_content("stable prompt", "https://api.openai.com/v1") == "stable prompt"


def test_cacheable_content_helper_wraps_for_anthropic():
    """Anthropic endpoint → array-of-blocks with ephemeral cache_control."""
    from pipeline.llm import cacheable_content

    result = cacheable_content("stable prompt", "https://api.anthropic.com/v1")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "stable prompt"
    assert result[0]["cache_control"] == {"type": "ephemeral"}


def test_cacheable_system_flag_wraps_system_message_on_anthropic(fake_llm_client, monkeypatch):
    """When cacheable_system=True + Anthropic endpoint, the system message
    content is an array-of-blocks with cache_control marker."""
    contract = LLMResponseContract("test")
    contract._client.endpoint = "https://api.anthropic.com/v1"
    contract.endpoint = contract._client.endpoint
    contract._client._client = FakeChatClient([_valid_json()])

    contract.call(LLMCallSpec(
        system="STABLE_TAXONOMY",
        user="new item",
        response_model=TinyResponse,
        cacheable_system=True,
    ))
    calls = contract._client._client.calls
    system_content = calls[0]["messages"][0]["content"]
    assert isinstance(system_content, list)
    assert system_content[0]["cache_control"] == {"type": "ephemeral"}
    assert system_content[0]["text"] == "STABLE_TAXONOMY"


def test_cacheable_system_flag_noop_on_non_anthropic(fake_llm_client):
    """cacheable_system=True on non-Anthropic endpoint leaves system as plain string."""
    contract = LLMResponseContract("test")
    contract._client.endpoint = "http://localhost:11434/v1"
    contract.endpoint = contract._client.endpoint
    contract._client._client = FakeChatClient([_valid_json()])

    contract.call(LLMCallSpec(
        system="STABLE_TAXONOMY",
        user="new item",
        response_model=TinyResponse,
        cacheable_system=True,
    ))
    calls = contract._client._client.calls
    assert calls[0]["messages"][0]["content"] == "STABLE_TAXONOMY"


def test_cacheable_system_default_false_keeps_plain_string(fake_llm_client):
    """Default: no caching — plain string, even on Anthropic. Opt-in only."""
    contract = LLMResponseContract("test")
    contract._client.endpoint = "https://api.anthropic.com/v1"
    contract.endpoint = contract._client.endpoint
    contract._client._client = FakeChatClient([_valid_json()])

    contract.call(LLMCallSpec(
        system="STABLE_TAXONOMY",
        user="new item",
        response_model=TinyResponse,
    ))
    calls = contract._client._client.calls
    assert calls[0]["messages"][0]["content"] == "STABLE_TAXONOMY"
