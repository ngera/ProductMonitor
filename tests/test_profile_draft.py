"""Phase 2 tests — profile drafting service.

Covers:
- draft_profile happy path with a mocked assistant contract
- URL-fetch failure sets page_fetch_failed but drafting still runs
- Unknown plugin_ids in suggested_sources are dropped
- requires_key computed from manifest connection_fields + env state
- Missing assistant LLM returns a DraftResult(profile=None, error_message=...)
- Draft function never raises even when the LLM throws
- List caps enforced (defensive; LLM can be sloppy)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline import profile_draft as pd
from pipeline.profile_draft import (
    DraftResult,
    ProfileDraft,
    SuggestedSource,
    draft_profile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeContract:
    """Stand-in for LLMResponseContract. Returns whatever the test queued."""

    def __init__(self, response: ProfileDraft | Exception):
        self.response = response
        self.calls: list = []

    def call(self, spec):
        self.calls.append(spec)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _install_contract(monkeypatch, contract):
    """Force _assistant_contract() to return `contract`."""
    monkeypatch.setattr(pd, "_assistant_contract", lambda: contract)


def _fake_manifest(plugin_id: str, required_fields: list[str] = None):
    """Build a SimpleNamespace shaped like SourceManifest for tests."""
    fields = [
        SimpleNamespace(name=n, required=True, type="secret")
        for n in (required_fields or [])
    ]
    return SimpleNamespace(
        plugin_id=plugin_id, display_name=plugin_id, connection_fields=fields,
    )


def _install_registry(monkeypatch, plugins: dict):
    """`plugins` maps plugin_id -> manifest. get() returns SimpleNamespace with .manifest."""
    class _Reg:
        def get(self, pid):
            m = plugins.get(pid)
            if m is None:
                return None
            return SimpleNamespace(manifest=m)
    monkeypatch.setattr("sources.registry.get_registry", lambda: _Reg())


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_draft_profile_returns_profile_when_contract_succeeds(monkeypatch):
    fake = ProfileDraft(
        description="Acme is a cloud storage product.",
        aliases=["Acme Cloud"],
        not_to_be_confused_with=["Acme Corp"],
        competitors=["Rival"],
        scope_in=["sync bugs"],
        scope_out=["billing"],
        suggested_sources=[
            SuggestedSource(plugin_id="hn", stream_config={"search_queries": ["Acme"]},
                            rationale="Broad tech signal."),
        ],
    )
    _install_contract(monkeypatch, _FakeContract(fake))
    _install_registry(monkeypatch, {"hn": _fake_manifest("hn")})

    result = draft_profile("Acme", "A cloud storage product.", ["bugs"])
    assert result.profile is not None
    assert result.profile.description == "Acme is a cloud storage product."
    assert result.profile.suggested_sources[0].plugin_id == "hn"
    assert result.profile.suggested_sources[0].requires_key is False
    assert result.page_fetch_failed is False


def test_draft_profile_filters_invalid_goals(monkeypatch):
    fake = ProfileDraft(description="x")
    contract = _FakeContract(fake)
    _install_contract(monkeypatch, contract)
    _install_registry(monkeypatch, {})

    draft_profile("Acme", "desc", ["bugs", "not_a_goal", "sentiment"])
    # The user prompt sent to the LLM should contain only valid goals.
    sent = contract.calls[0].user
    assert "bugs" in sent and "sentiment" in sent
    assert "not_a_goal" not in sent


# ---------------------------------------------------------------------------
# URL fetching
# ---------------------------------------------------------------------------


def test_draft_profile_marks_page_fetch_failed_when_url_fetch_errors(monkeypatch):
    fake = ProfileDraft(description="Acme desc")
    _install_contract(monkeypatch, _FakeContract(fake))
    _install_registry(monkeypatch, {})
    # Force _fetch_url_text to report failure.
    monkeypatch.setattr(pd, "_fetch_url_text", lambda url: ("", True))

    result = draft_profile("Acme", "https://example.com/broken", [])
    assert result.profile is not None  # drafting still proceeds
    assert result.page_fetch_failed is True


def test_draft_profile_no_url_does_not_mark_fetch_failed(monkeypatch):
    fake = ProfileDraft(description="Acme desc")
    _install_contract(monkeypatch, _FakeContract(fake))
    _install_registry(monkeypatch, {})
    result = draft_profile("Acme", "just a text description", [])
    assert result.page_fetch_failed is False


# ---------------------------------------------------------------------------
# Source validation
# ---------------------------------------------------------------------------


def test_unknown_plugin_id_gets_dropped(monkeypatch):
    fake = ProfileDraft(
        description="x",
        suggested_sources=[
            SuggestedSource(plugin_id="hn", stream_config={}),
            SuggestedSource(plugin_id="totally-made-up", stream_config={}),
        ],
    )
    _install_contract(monkeypatch, _FakeContract(fake))
    _install_registry(monkeypatch, {"hn": _fake_manifest("hn")})

    result = draft_profile("Acme", "desc", [])
    plugin_ids = [s.plugin_id for s in result.profile.suggested_sources]
    assert plugin_ids == ["hn"]


def test_requires_key_true_when_env_var_missing(monkeypatch):
    fake = ProfileDraft(
        description="x",
        suggested_sources=[
            SuggestedSource(plugin_id="reddit", stream_config={"subreddit": "r/foo"}),
        ],
    )
    _install_contract(monkeypatch, _FakeContract(fake))
    _install_registry(monkeypatch, {
        "reddit": _fake_manifest("reddit", required_fields=["REDDIT_CLIENT_ID"]),
    })
    # Ensure the env doesn't have this key.
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.setattr(pd, "_read_env_snapshot", lambda: {})

    result = draft_profile("Acme", "desc", [])
    assert result.profile.suggested_sources[0].requires_key is True


def test_requires_key_false_when_env_var_present(monkeypatch):
    fake = ProfileDraft(
        description="x",
        suggested_sources=[
            SuggestedSource(plugin_id="reddit", stream_config={"subreddit": "r/foo"}),
        ],
    )
    _install_contract(monkeypatch, _FakeContract(fake))
    _install_registry(monkeypatch, {
        "reddit": _fake_manifest("reddit", required_fields=["REDDIT_CLIENT_ID"]),
    })
    monkeypatch.setattr(pd, "_read_env_snapshot", lambda: {"REDDIT_CLIENT_ID": "xyz"})

    result = draft_profile("Acme", "desc", [])
    assert result.profile.suggested_sources[0].requires_key is False


def test_optional_secret_does_not_set_requires_key(monkeypatch):
    """STACKEX_KEY-style fields are type=secret but required=False.
    Missing them must not mark the source as requiring a key — otherwise
    the wizard pick→configure step silently drops the user's selection."""
    fake = ProfileDraft(
        description="x",
        suggested_sources=[
            SuggestedSource(plugin_id="stackex", stream_config={"site": "stackoverflow"}),
        ],
    )
    _install_contract(monkeypatch, _FakeContract(fake))
    manifest = SimpleNamespace(
        plugin_id="stackex",
        display_name="Stack Exchange",
        connection_fields=[
            SimpleNamespace(name="STACKEX_KEY", required=False, type="secret"),
        ],
    )
    _install_registry(monkeypatch, {"stackex": manifest})
    monkeypatch.setattr(pd, "_read_env_snapshot", lambda: {})

    result = draft_profile("Acme", "desc", [])
    assert result.profile is not None
    assert result.profile.suggested_sources[0].requires_key is False


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_missing_assistant_llm_returns_error_result(monkeypatch):
    _install_contract(monkeypatch, None)  # simulates unconfigured LLM
    monkeypatch.setattr(pd, "_assistant_contract", lambda: None)

    result = draft_profile("Acme", "desc", [])
    assert result.profile is None
    assert "assistant LLM not configured" in result.error_message


def test_missing_name_returns_error_without_llm_call(monkeypatch):
    called = {"n": 0}
    def spy():
        called["n"] += 1
        return None
    monkeypatch.setattr(pd, "_assistant_contract", spy)

    result = draft_profile("", "desc", [])
    assert result.profile is None
    assert "name" in result.error_message
    assert called["n"] == 0  # short-circuits before touching LLM


def test_llm_exception_returns_error_result_not_raises(monkeypatch):
    _install_contract(monkeypatch, _FakeContract(RuntimeError("boom")))
    _install_registry(monkeypatch, {})

    result = draft_profile("Acme", "desc", [])
    assert result.profile is None
    assert "drafting failed" in result.error_message


# ---------------------------------------------------------------------------
# Defensive caps
# ---------------------------------------------------------------------------


def test_over_long_alias_list_truncated(monkeypatch):
    fake = ProfileDraft(
        description="x",
        aliases=[f"alias-{i}" for i in range(50)],
        competitors=[f"comp-{i}" for i in range(50)],
    )
    _install_contract(monkeypatch, _FakeContract(fake))
    _install_registry(monkeypatch, {})

    result = draft_profile("Acme", "desc", [])
    assert len(result.profile.aliases) == pd.MAX_ALIASES
    assert len(result.profile.competitors) == pd.MAX_COMPETITORS


def test_suggested_source_accepts_bare_string(monkeypatch):
    """LLM regression path: when guided decoding fails and the LLM emits a
    list of plain plugin_id strings, the `SuggestedSource` validator must
    coerce them into objects rather than failing 5 validation errors and
    aborting the draft."""
    fake = ProfileDraft(
        description="x",
        # Simulate the exact plain-JSON pattern from the 400-error log path:
        # LLM returned `["github_issues", "reddit", "hn"]` after fallback.
        suggested_sources=["github_issues", "reddit", "hn"],  # type: ignore[list-item]
    )
    _install_contract(monkeypatch, _FakeContract(fake))
    _install_registry(monkeypatch, {"hn": _fake_manifest("hn")})

    result = draft_profile("Acme", "desc", [])
    # Unknown ids get dropped by registry validation; hn survives.
    plugin_ids = [s.plugin_id for s in result.profile.suggested_sources]
    assert plugin_ids == ["hn"]


def test_duplicate_aliases_deduped(monkeypatch):
    fake = ProfileDraft(
        description="x",
        aliases=["A1", "A1", "A2", "A1"],
    )
    _install_contract(monkeypatch, _FakeContract(fake))
    _install_registry(monkeypatch, {})

    result = draft_profile("Acme", "desc", [])
    assert result.profile.aliases == ["A1", "A2"]
