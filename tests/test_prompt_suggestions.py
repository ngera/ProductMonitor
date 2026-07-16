"""Tests for pipeline/prompt_suggestions.py (POST_V1_PLAN §4.5)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pipeline.prompt_suggestions import (
    COVERAGE_DROP_THRESHOLD,
    CoverageResult,
    PromptEdit,
    PromptSuggestions,
    apply_edit,
    coverage_check,
    generate_suggestions,
    suggestion_cache_key,
)


# ---------------------------------------------------------------------------
# apply_edit — pure transforms on prompt dicts
# ---------------------------------------------------------------------------


def test_apply_add_to_empty_field():
    result = apply_edit(
        {"classify": {}},
        PromptEdit(kind="add", target="classify.template", after="new text",
                   rationale="r"),
    )
    assert result["classify"]["template"] == "new text"


def test_apply_add_appends_with_blank_line():
    result = apply_edit(
        {"classify": {"template": "existing"}},
        PromptEdit(kind="add", target="classify.template", after="added",
                   rationale="r"),
    )
    assert result["classify"]["template"] == "existing\n\nadded"


def test_apply_remove_strips_target_text():
    result = apply_edit(
        {"classify": {"template": "keep this. drop this. keep more."}},
        PromptEdit(kind="remove", target="classify.template",
                   before="drop this.", rationale="r"),
    )
    assert "drop this" not in result["classify"]["template"]
    assert "keep this" in result["classify"]["template"]


def test_apply_remove_raises_when_target_absent():
    with pytest.raises(ValueError, match="not found"):
        apply_edit(
            {"classify": {"template": "abc"}},
            PromptEdit(kind="remove", target="classify.template",
                       before="xyz", rationale="r"),
        )


def test_apply_replace_swaps_text():
    result = apply_edit(
        {"relevance": {"system": "You are a strict classifier."}},
        PromptEdit(kind="replace", target="relevance.system",
                   before="strict", after="calibrated", rationale="r"),
    )
    assert result["relevance"]["system"] == "You are a calibrated classifier."


def test_apply_edit_does_not_mutate_input():
    original = {"classify": {"template": "abc"}}
    apply_edit(
        original,
        PromptEdit(kind="add", target="classify.template", after="xyz", rationale="r"),
    )
    assert original["classify"]["template"] == "abc"


def test_apply_edit_rejects_invalid_target():
    with pytest.raises(ValueError):
        apply_edit(
            {},
            PromptEdit(kind="add", target="unknown.template", after="x", rationale="r"),
        )
    with pytest.raises(ValueError):
        apply_edit(
            {},
            PromptEdit(kind="add", target="no_dot", after="x", rationale="r"),
        )


def test_apply_edit_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown edit kind"):
        apply_edit(
            {"classify": {"template": "x"}},
            PromptEdit(kind="mutate", target="classify.template",
                       before="x", after="y", rationale="r"),
        )


# ---------------------------------------------------------------------------
# generate_suggestions — assistant LLM gate
# ---------------------------------------------------------------------------


def test_generate_returns_none_when_no_snippets():
    result = generate_suggestions(current_prompts={}, recent_snippets=[])
    assert result is None


def test_generate_returns_none_when_assistant_unconfigured(monkeypatch):
    def _boom():
        raise RuntimeError("not configured")
    monkeypatch.setattr("pipeline.assistant_llm.client", _boom)

    snippet = SimpleNamespace(id="s1", body="b", title="t", polarity="positive_example", labels={})
    result = generate_suggestions(
        current_prompts={"relevance": {}}, recent_snippets=[snippet],
    )
    assert result is None


def test_generate_returns_parsed_suggestions(monkeypatch):
    """When the LLM responds cleanly, we return the parsed PromptSuggestions."""
    fake_llm = SimpleNamespace(model="m", endpoint="e")
    monkeypatch.setattr("pipeline.assistant_llm.client", lambda: fake_llm)

    expected = PromptSuggestions(
        edits=[
            PromptEdit(kind="add", target="classify.template",
                       after="Do X", rationale="why"),
        ],
        summary="One edit.",
    )

    class _StubContract:
        def call(self, spec):
            return expected

    def _fake_new(cls):
        return _StubContract()

    monkeypatch.setattr("pipeline.llm_contract.LLMResponseContract.__new__", _fake_new)

    snippet = SimpleNamespace(id="s1", body="b", title="t",
                              polarity="positive_example", labels={"areas": ["audio"]})
    result = generate_suggestions(
        current_prompts={"relevance": {"template": "t"}, "classify": {"template": "c"}},
        recent_snippets=[snippet],
    )
    assert result is expected


# ---------------------------------------------------------------------------
# Coverage check
# ---------------------------------------------------------------------------


def test_coverage_result_reports_drop_pp():
    r = CoverageResult(
        n_items=10, baseline_pass_rate=0.8, proposed_pass_rate=0.7,
        delta=0.1, blocked=False,
    )
    assert r.drop_pp == 10.0


class _FakeCon:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_kw):
        return self

    def fetchall(self):
        return self._rows


class _FakeWarehouseCtx:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return _FakeCon(self._rows)

    def __exit__(self, *args):
        return False


def test_coverage_check_no_items_returns_pass_rate_zero(monkeypatch):
    monkeypatch.setattr("pipeline.storage.warehouse", lambda: _FakeWarehouseCtx([]))
    result = coverage_check(
        product_id="p",
        baseline_prompts={"relevance": {"system": "s", "template": "t"}},
        proposed_prompts={"relevance": {"system": "s", "template": "t"}},
    )
    assert result.n_items == 0
    assert result.blocked is False


def test_coverage_check_computes_baseline_pass_rate(monkeypatch):
    rows = [("i1", "t", "b", True), ("i2", "t", "b", True),
            ("i3", "t", "b", False), ("i4", "t", "b", True)]
    monkeypatch.setattr("pipeline.storage.warehouse", lambda: _FakeWarehouseCtx(rows))
    result = coverage_check(
        product_id="p",
        baseline_prompts={"relevance": {"system": "s", "template": "t"}},
        proposed_prompts={"relevance": {"system": "s", "template": "t"}},
    )
    assert result.n_items == 4
    assert result.baseline_pass_rate == 0.75
    # Same prompts → no drop
    assert result.blocked is False


def test_coverage_check_blocks_on_dramatic_shrinkage(monkeypatch):
    """Wiping most of the relevance prompt is estimated to drop pass rate
    below the threshold → block."""
    rows = [("i", "t", "b", True)] * 10
    monkeypatch.setattr("pipeline.storage.warehouse", lambda: _FakeWarehouseCtx(rows))

    baseline = {"relevance": {"system": "a" * 500, "template": "b" * 500}}
    proposed = {"relevance": {"system": "", "template": "b"}}  # 999 chars → 1 char
    result = coverage_check(
        product_id="p", baseline_prompts=baseline, proposed_prompts=proposed,
    )
    assert result.blocked is True
    assert result.reason


def test_coverage_check_allows_trivial_edit(monkeypatch):
    """A tiny prompt tweak shouldn't spuriously block."""
    rows = [("i", "t", "b", True)] * 5
    monkeypatch.setattr("pipeline.storage.warehouse", lambda: _FakeWarehouseCtx(rows))

    baseline = {"relevance": {"system": "You are strict.", "template": "Is this relevant?"}}
    proposed = {"relevance": {"system": "You are strict.",
                              "template": "Is this relevant to the product?"}}
    result = coverage_check(
        product_id="p", baseline_prompts=baseline, proposed_prompts=proposed,
    )
    assert result.blocked is False


# ---------------------------------------------------------------------------
# suggestion_cache_key
# ---------------------------------------------------------------------------


def test_cache_key_stable_across_snippet_order():
    a = suggestion_cache_key(current_prompts={"x": 1}, recent_snippet_ids=["a", "b", "c"])
    b = suggestion_cache_key(current_prompts={"x": 1}, recent_snippet_ids=["c", "b", "a"])
    assert a == b


def test_cache_key_changes_on_prompt_edit():
    a = suggestion_cache_key(current_prompts={"x": 1}, recent_snippet_ids=["a"])
    b = suggestion_cache_key(current_prompts={"x": 2}, recent_snippet_ids=["a"])
    assert a != b


def test_cache_key_changes_on_new_snippet():
    a = suggestion_cache_key(current_prompts={"x": 1}, recent_snippet_ids=["a"])
    b = suggestion_cache_key(current_prompts={"x": 1}, recent_snippet_ids=["a", "b"])
    assert a != b
