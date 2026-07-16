"""Tests for pipeline/token_usage.py (POST_V1_PLAN §4.11, ADR-0005)."""

from __future__ import annotations

import pytest

from pipeline import token_usage
from pipeline.token_usage import (
    TokenContext,
    estimate_cost_usd,
    get_context,
    record_usage,
    set_context,
)


def test_default_context_is_none():
    assert get_context() is None


def test_set_context_pushes_and_restores():
    with set_context(TokenContext(run_id="r1", stage="classify")):
        ctx = get_context()
        assert ctx is not None
        assert ctx.run_id == "r1"
        assert ctx.stage == "classify"
    # After exit, context restored
    assert get_context() is None


def test_nested_contexts_merge():
    """Inner context overrides matching fields; parent fills in blanks."""
    with set_context(TokenContext(run_id="r1", stage="classify", product_id="p1")):
        with set_context(TokenContext(item_id="hn:1234")):
            ctx = get_context()
            # Inner supplied item_id; parent's run_id / stage / product_id inherited
            assert ctx.run_id == "r1"
            assert ctx.stage == "classify"
            assert ctx.product_id == "p1"
            assert ctx.item_id == "hn:1234"
        # After inner exits, parent restored
        assert get_context().item_id == ""


def test_nested_child_can_override_parent():
    with set_context(TokenContext(run_id="r1", stage="relevance")):
        with set_context(TokenContext(stage="classify")):
            ctx = get_context()
            assert ctx.run_id == "r1"       # inherited
            assert ctx.stage == "classify"  # overridden


def test_record_usage_without_product_id_is_noop():
    """No product_id → no warehouse write. Ensures we don't create warehouses
    for assistant-LLM calls at setup time."""
    # Just verify it doesn't raise
    record_usage(
        endpoint="test",
        model="test-model",
        prompt_tokens=10,
        completion_tokens=5,
    )


def test_estimate_cost_returns_none_for_unknown_model():
    assert estimate_cost_usd("unknown-model-xyz", 1000, 500) is None


def test_estimate_cost_for_known_model():
    """Test against a value in config/model_pricing.yaml."""
    # haiku-4-5: input $1.00/M, output $5.00/M
    # 1000 in + 500 out = 0.001 + 0.0025 = 0.0035
    cost = estimate_cost_usd("claude-haiku-4-5-20251001", 1000, 500)
    assert cost is not None
    assert 0.003 <= cost <= 0.004


def test_estimate_cost_with_cache_reads():
    """Cache reads are cheaper than fresh input."""
    # haiku-4-5 cache_read: $0.10/M; input: $1.00/M
    # 1000 total prompt, 500 cached → 500 fresh + 500 cached + 100 output
    # = 500 * 0.000001 + 500 * 0.0000001 + 100 * 0.000005
    # = 0.0005 + 0.00005 + 0.0005 = 0.00105
    fresh_cost = estimate_cost_usd("claude-haiku-4-5-20251001", 1000, 100)
    cached_cost = estimate_cost_usd("claude-haiku-4-5-20251001", 1000, 100, cached_input_tokens=500)
    assert cached_cost is not None
    assert fresh_cost is not None
    assert cached_cost < fresh_cost, "cache hit should reduce cost"
